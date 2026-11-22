"""Partner coordination (Indian partner firms and international service providers) with
confidentiality controls.

Rules enforced here and in policy/comms:

1. Partners only receive **anonymised** work briefs: the client's company name, contact names,
   e-mail addresses and phone numbers are replaced by a stable code name (``Client-XXXXXX``),
   including occurrences inside free text supplied by agents.
2. A brief can only be prepared for a partner whose NDA the **owner** has attested as on file.
3. Every message to a partner's e-mail domain is an owner-approval action
   (category ``confidential_disclosure``), regardless of the general e-mail policy, and is refused
   outright if the partner has no NDA on file.
4. Partner records never leave the tenant (tenant isolation applies as everywhere else).
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .crm import _norm_email
from .models import Company, Contact, Deal, Document, Partner
from .policy import audit

REGIONS = ("india", "international")


class PartnerError(ValueError):
    pass


def register(s: Session, *, name: str, region: str, contact_email: str, services: str = "",
             actor: str) -> Partner:
    if region not in REGIONS:
        raise PartnerError(f"region must be one of {REGIONS}")
    email = _norm_email(contact_email)
    name = (name or "").strip()
    if not name:
        raise PartnerError("partner name is required")
    existing = s.scalars(select(Partner).where(func.lower(Partner.name) == name.lower())).first()
    if existing:
        return existing
    p = Partner(name=name, region=region, services=services, contact_email=email,
                email_domain=email.rsplit("@", 1)[1], created_by=actor)
    s.add(p)
    s.flush()
    audit(s, actor, "partner.registered", p.id, name=name, region=region)
    return p


def attest_nda(s: Session, partner_id: str, *, on_file: bool, actor: str) -> Partner:
    """Owner-only (enforced by the API): record that a signed NDA is (or is no longer) on file."""
    p = s.get(Partner, partner_id)
    if p is None:
        raise PartnerError("partner not found")
    p.nda_status = "on_file" if on_file else "none"
    p.nda_attested_by = actor if on_file else None
    if on_file and p.status == "prospect":
        p.status = "active"
    audit(s, actor, "partner.nda_attested", p.id, on_file=on_file)
    return p


def _domain_matches(domain: str, partner_domain: str) -> bool:
    return domain == partner_domain or domain.endswith("." + partner_domain)


def partner_for_address(s: Session, address: str) -> Partner | None:
    """The partner an address belongs to, including sub-domains (ops@mail.partner.in -> partner.in)."""
    domain = address.strip().lower().rsplit("@", 1)[-1]
    for p in s.scalars(select(Partner)):
        if _domain_matches(domain, p.email_domain):
            return p
    return None


def code_name(deal_id: str) -> str:
    return "Client-" + hashlib.sha256(deal_id.encode()).hexdigest()[:6].upper()


LEGAL_SUFFIXES = {"ltd", "limited", "inc", "llc", "llp", "plc", "pvt", "private", "corp", "corporation", "co",
                  "company", "gmbh", "sa", "ag", "bv", "the"}
MIN_FINGERPRINT = 4


def _norm(text: str) -> str:
    """Lower-case letters and digits only: defeats spacing, punctuation and case tricks."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _identifiers(s: Session, deal: Deal) -> list[str]:
    """Literal identifiers of the client (for replacement): the deal's contact, every contact at the
    same company, the company, its website, and the deal title."""
    contacts = [s.get(Contact, deal.contact_id)]
    company = s.get(Company, deal.company_id) if deal.company_id else None
    if company:
        contacts += list(s.scalars(select(Contact).where(Contact.company_id == company.id)))
    ids: list[str] = [deal.title]
    for c in {c.id: c for c in contacts if c}.values():
        ids += [c.email, f"{c.first_name} {c.last_name}".strip(), c.first_name, c.last_name, c.phone,
                c.email.rsplit("@", 1)[1]]
    if company:
        ids += [company.name, company.website]
    return sorted({i.strip() for i in ids if i and len(i.strip()) >= 2}, key=len, reverse=True)


def _domain_label(domain: str) -> str:
    """Registrable label of a domain: northwind.io -> northwind, shop.acme.co.uk -> acme."""
    labels = [x for x in domain.lower().strip(".").split(".") if x]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in {"co", "com", "org", "net", "ac", "gov", "ltd"}:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")


def fingerprints(s: Session, deal: Deal) -> set[str]:
    """Normalised fingerprints used to DETECT identity leaks that literal replacement misses (spacing,
    punctuation, abbreviations, phone formats, "dot com" spellings). Built per identifier type so
    that generic business words in a company name or deal title do not become forbidden."""
    fps: set[str] = {_norm(deal.title)}
    contacts = [s.get(Contact, deal.contact_id)]
    company = s.get(Company, deal.company_id) if deal.company_id else None
    if company:
        contacts += list(s.scalars(select(Contact).where(Contact.company_id == company.id)))
    for c in {c.id: c for c in contacts if c}.values():
        local, domain = c.email.rsplit("@", 1)
        fps |= {_norm(c.email), _norm(local), _domain_label(domain),
                _norm(f"{c.first_name}{c.last_name}"), _norm(c.last_name)}
        digits = re.sub(r"\D", "", c.phone or "")
        if len(digits) >= 7:
            fps.add(digits[-7:])
    if company:
        core = [w for w in re.split(r"[\s.,()/&-]+", company.name.lower()) if w and w not in LEGAL_SUFFIXES]
        fps.add(_norm(" ".join(core)))
        if core and len(core[0]) >= 5:
            fps.add(_norm(core[0]))  # the distinctive part, e.g. "northwind" of Northwind Logistics
        if company.website:
            fps.add(_domain_label(re.sub(r"^https?://", "", company.website).split("/")[0]))
    return {fp for fp in fps if len(fp) >= MIN_FINGERPRINT and fp != "client"}


def anonymise(s: Session, deal: Deal, text: str) -> str:
    name = code_name(deal.id)
    for ident in _identifiers(s, deal):
        text = re.sub(re.escape(ident), name, text, flags=re.IGNORECASE)
    return text


def leaks(s: Session, deal: Deal, text: str) -> list[str]:
    """Fingerprints of the client still present in ``text`` (after normalisation)."""
    norm = _norm(text.replace(code_name(deal.id), ""))
    digits = re.sub(r"\D", "", text)
    return sorted(fp for fp in fingerprints(s, deal) if fp in norm or (fp.isdigit() and fp in digits))


def prepare_brief(s: Session, *, partner_id: str, deal_id: str, scope: str, actor: str) -> Document:
    from .documents import generate

    partner = s.get(Partner, partner_id)
    if partner is None:
        raise PartnerError("partner not found")
    if partner.nda_status != "on_file":
        raise PartnerError(f"no NDA on file for {partner.name}; the owner must attest one before sharing work")
    deal = s.get(Deal, deal_id)
    if deal is None:
        raise PartnerError("deal not found")
    clean_scope = anonymise(s, deal, scope)
    if leaks(s, deal, clean_scope):
        # Obfuscated identity (spacing, abbreviations, phone formats...) survived replacement: refuse
        # rather than guess. The agent must describe the work without identifying the client.
        raise PartnerError("scope still identifies the client after anonymisation; rewrite it without names, "
                           "contact details or company references")
    data = {"partner_name": partner.name, "partner_id": partner.id, "client_code": code_name(deal.id),
            "scope": clean_scope, "region": partner.region}
    doc = generate(s, kind="partner_brief", title=f"Partner brief for {partner.name} ({data['client_code']})",
                   data=data, deal_id=deal.id, actor=actor, internal=True)
    if leaks(s, deal, doc.markdown) or leaks(s, deal, doc.html):
        raise PartnerError("brief still contains client-identifying information; refused")
    audit(s, actor, "partner.brief_prepared", doc.id, partner_id=partner.id, deal_id=deal.id)
    return doc


def verify_brief_for_sending(s: Session, partner: Partner, doc: Document) -> None:
    """Re-checked at send time (after owner approval too): provenance, NDA and leaks."""
    if doc.kind != "partner_brief" or doc.data.get("partner_id") != partner.id:
        raise PartnerError("not a system-generated brief for this partner")
    if partner.nda_status != "on_file":
        raise PartnerError(f"no NDA on file for {partner.name}")
    deal = s.get(Deal, doc.deal_id) if doc.deal_id else None
    if deal is None:
        raise PartnerError("brief has no deal")
    if leaks(s, deal, doc.markdown) or leaks(s, deal, doc.html):
        raise PartnerError("brief identifies the client; refused")
