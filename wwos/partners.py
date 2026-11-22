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


CATEGORIES = ("ca_firm", "cs_firm", "ca_cs_firm", "international_provider", "other")
AGENT_STAGES = ("identified", "contacted", "in_discussion", "nda_requested", "declined")


def register(s: Session, *, name: str, region: str, contact_email: str, services: str = "",
             category: str = "other", actor: str) -> Partner:
    """Record a PROSPECTIVE partner. Nothing here confirms a relationship."""
    if region not in REGIONS:
        raise PartnerError(f"region must be one of {REGIONS}")
    if category not in CATEGORIES:
        raise PartnerError(f"category must be one of {CATEGORIES}")
    if category in ("ca_firm", "cs_firm", "ca_cs_firm") and region != "india":
        raise PartnerError("CA/CS firm categories apply to Indian partners")
    email = _norm_email(contact_email)
    name = (name or "").strip()
    if not name:
        raise PartnerError("partner name is required")
    existing = s.scalars(select(Partner).where(func.lower(Partner.name) == name.lower())).first()
    if existing:
        return existing
    domain = email.rsplit("@", 1)[1]
    overlap = partners_for_address(s, email)
    if overlap:  # one domain, one partner: avoids a prospect shadowing a confirmed or inactive partner
        raise PartnerError(f"the domain {domain} is already registered to partner {overlap[0].name}")
    p = Partner(name=name, region=region, services=services, contact_email=email, category=category,
                email_domain=domain, created_by=actor)
    s.add(p)
    s.flush()
    audit(s, actor, "partner.registered", p.id, name=name, region=region, category=category)
    return p


def update_stage(s: Session, partner_id: str, *, stage: str, note: str = "", actor: str) -> Partner:
    """Agents move prospects through the acquisition pipeline; they can never confirm a partner."""
    p = s.get(Partner, partner_id)
    if p is None:
        raise PartnerError("partner not found")
    if stage not in AGENT_STAGES:
        raise PartnerError(f"stage must be one of {AGENT_STAGES}; confirmation is reserved for the owner")
    if p.status == "confirmed":
        raise PartnerError("partner is already confirmed; only the owner can change a confirmed partner")
    p.pipeline_stage, p.stage_note = stage, note[:2000]
    audit(s, actor, "partner.stage", p.id, stage=stage)
    return p


def attest_nda(s: Session, partner_id: str, *, on_file: bool, actor: str) -> Partner:
    """Owner-only (enforced by the API): record that a signed NDA is (or is no longer) on file.
    An NDA alone does not confirm a partner."""
    p = s.get(Partner, partner_id)
    if p is None:
        raise PartnerError("partner not found")
    p.nda_status = "on_file" if on_file else "none"
    p.nda_attested_by = actor if on_file else None
    if not on_file and p.status == "confirmed":
        p.status = "prospect"  # no NDA, no work sharing
        p.agreement_confirmed_by = None
    audit(s, actor, "partner.nda_attested", p.id, on_file=on_file)
    return p


def confirm(s: Session, partner_id: str, *, agreement_on_file: bool, credentials_verified: bool,
            actor: str) -> Partner:
    """Owner-only: confirm a partnership (requires NDA on file and an agreement the owner has signed)."""
    p = s.get(Partner, partner_id)
    if p is None:
        raise PartnerError("partner not found")
    if p.nda_status != "on_file":
        raise PartnerError("attest the NDA first; a partner cannot be confirmed without one")
    if not agreement_on_file:
        raise PartnerError("a partner is confirmed only when a partnership agreement is on file")
    p.status, p.pipeline_stage, p.agreement_confirmed_by = "confirmed", "confirmed", actor
    if credentials_verified:
        p.credentials_verified_by = actor
    audit(s, actor, "partner.confirmed", p.id, credentials_verified=credentials_verified)
    return p


def deactivate(s: Session, partner_id: str, *, actor: str) -> Partner:
    p = s.get(Partner, partner_id)
    if p is None:
        raise PartnerError("partner not found")
    p.status = "inactive"
    audit(s, actor, "partner.deactivated", p.id)
    return p


def require_confirmed(partner: Partner) -> None:
    if partner.status != "confirmed":
        raise PartnerError(f"{partner.name} is a prospective partner, not confirmed by the owner; no work may be shared")
    if partner.nda_status != "on_file":
        raise PartnerError(f"no NDA on file for {partner.name}")


def _domain_matches(domain: str, partner_domain: str) -> bool:
    return domain == partner_domain or domain.endswith("." + partner_domain)


def partners_for_address(s: Session, address: str) -> list[Partner]:
    """Every partner an address could belong to, including sub-domains (ops@mail.partner.in -> partner.in)."""
    domain = address.strip().lower().rsplit("@", 1)[-1]
    return [p for p in s.scalars(select(Partner).order_by(Partner.created_at))
            if _domain_matches(domain, p.email_domain) or _domain_matches(p.email_domain, domain)]


def partner_for_address(s: Session, address: str) -> Partner | None:
    """The STRICTEST matching partner: inactive beats confirmed beats prospect, so an ambiguous
    address can never be downgraded to a weaker rule."""
    matches = partners_for_address(s, address)
    rank = {"inactive": 0, "confirmed": 1, "prospect": 2}
    return min(matches, key=lambda p: rank.get(p.status, 0)) if matches else None


def client_leaks_in(s: Session, text: str) -> list[str]:
    """Identifiers of any open deal's client found in ``text`` (for messages to partner domains)."""
    from .models import Deal as _Deal

    found: list[str] = []
    for deal in s.scalars(select(_Deal).where(_Deal.stage.notin_(["lost"])).order_by(_Deal.created_at.desc()).limit(500)):
        if leaks(s, deal, text):
            found.append(code_name(deal.id))
    return found


def code_name(deal_id: str) -> str:
    return "Client-" + hashlib.sha256(deal_id.encode()).hexdigest()[:6].upper()


LEGAL_SUFFIXES = {"ltd", "limited", "inc", "llc", "llp", "plc", "pvt", "private", "corp", "corporation", "co",
                  "company", "gmbh", "sa", "ag", "bv", "the"}
MIN_FINGERPRINT = 4


# Look-alike characters folded to Latin before matching (Cyrillic/Greek homoglyphs, digit swaps).
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "к": "k", "м": "m", "т": "t",
    "н": "h", "в": "b", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "α": "a", "ο": "o", "ρ": "p",
    "ν": "v", "τ": "t", "ι": "i", "κ": "k", "ε": "e", "Α": "a", "Β": "b", "Ε": "e", "Κ": "k", "Μ": "m",
    "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Χ": "x", "Υ": "y",
    "А": "a", "В": "b", "Е": "e", "К": "k", "М": "m", "Н": "h", "О": "o", "Р": "p", "С": "c", "Т": "t",
    "Х": "x", "У": "y", "І": "i", "Ј": "j", "Ѕ": "s",
})
_DIGIT_SWAPS = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
# Generic mailbox names: never treated as identifying (avoids refusing ordinary words like "information").
GENERIC_LOCALS = {"info", "contact", "sales", "admin", "hello", "office", "support", "enquiries", "inquiries",
                  "team", "mail", "ops", "accounts", "billing", "hr", "jobs", "noreply", "no-reply", "help",
                  "finance", "marketing", "careers", "service", "services"}


def _fold(text: str) -> str:
    """Unicode compatibility form, homoglyphs folded to Latin, accents dropped, lower case."""
    import unicodedata

    text = unicodedata.normalize("NFKC", text).translate(_CONFUSABLES)
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return text.lower()


def _norm(text: str) -> str:
    """Letters and digits only, after folding: defeats spacing, punctuation, case and homoglyph tricks."""
    return re.sub(r"[^a-z0-9]", "", _fold(text))


def _norm_letters(text: str) -> str:
    """Like _norm but with digit-for-letter swaps undone ("N0rthw1nd" -> "northwlnd")."""
    return re.sub(r"[^a-z0-9]", "", _fold(text).translate(_DIGIT_SWAPS))


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
    # Names shorter than 3 characters are not replaced literally (they would mangle ordinary words);
    # the fingerprint check still covers them as whole words.
    return sorted({i.strip() for i in ids if i and len(i.strip()) >= 3}, key=len, reverse=True)


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
        fps |= {_norm(c.email), _domain_label(domain), _norm(f"{c.first_name}{c.last_name}"), _norm(c.last_name)}
        if local.lower() not in GENERIC_LOCALS:
            fps.add(_norm(local))
        # each part of a multi-part surname, matched as a whole word ("Ms A. Smith" for "Smith Jones")
        fps |= {_norm(part) for part in re.split(r"[\s-]+", c.last_name or "") if len(part) >= 4}
        digits = re.sub(r"\D", "", c.phone or "")
        if len(digits) >= 7:
            fps.add(digits[-7:])
    if company:
        core = [w for w in re.split(r"[\s.,()/&-]+", company.name.lower()) if w and w not in LEGAL_SUFFIXES]
        fps.add(_norm(" ".join(core)))
        if core and len(core[0]) >= 3:
            fps.add(_norm(core[0]))  # the distinctive part, e.g. "northwind" of Northwind Logistics, "acme"
        if company.website:
            fps.add(_domain_label(re.sub(r"^https?://", "", company.website).split("/")[0]))
    return {fp for fp in fps if len(fp) >= 3 and fp != "client" and fp not in GENERIC_LOCALS}


def anonymise(s: Session, deal: Deal, text: str) -> str:
    name = code_name(deal.id)
    for ident in _identifiers(s, deal):
        # Whole-word replacement: "Li" never turns "Client" into "CClient-..."; "Smith" leaves "smithing".
        pattern = (r"(?<![\w])" if ident[0].isalnum() else "") + re.escape(ident) + (r"(?![\w])" if ident[-1].isalnum() else "")
        text = re.sub(pattern, name, text, flags=re.IGNORECASE)
    return text


def leaks(s: Session, deal: Deal, text: str) -> list[str]:
    """Fingerprints of the client still present in ``text`` (after normalisation)."""
    text = text.replace(code_name(deal.id), " ")
    norm, swapped = _norm(text), _norm_letters(text)
    words = set(re.findall(r"[a-z0-9]+", _fold(text))) | set(re.findall(r"[a-z]+", _fold(text).translate(_DIGIT_SWAPS)))
    digits = re.sub(r"\D", "", text)
    found = []
    for fp in fingerprints(s, deal):
        if fp.isdigit():
            hit = fp in digits
        elif len(fp) < MIN_FINGERPRINT + 1:  # short names: whole words only ("Acme", "Tata", "IBM")
            hit = fp in words
        else:
            hit = fp in norm or fp.translate(_DIGIT_SWAPS) in swapped
        if hit:
            found.append(fp)
    return sorted(found)


def prepare_brief(s: Session, *, partner_id: str, deal_id: str, scope: str, actor: str) -> Document:
    from .documents import generate

    partner = s.get(Partner, partner_id)
    if partner is None:
        raise PartnerError("partner not found")
    require_confirmed(partner)
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
    require_confirmed(partner)
    deal = s.get(Deal, doc.deal_id) if doc.deal_id else None
    if deal is None:
        raise PartnerError("brief has no deal")
    if leaks(s, deal, doc.markdown) or leaks(s, deal, doc.html):
        raise PartnerError("brief identifies the client; refused")
