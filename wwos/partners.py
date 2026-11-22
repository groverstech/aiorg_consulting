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


def partner_for_address(s: Session, address: str) -> Partner | None:
    domain = address.strip().lower().rsplit("@", 1)[-1]
    return s.scalars(select(Partner).where(Partner.email_domain == domain)).first()


def code_name(deal_id: str) -> str:
    return "Client-" + hashlib.sha256(deal_id.encode()).hexdigest()[:6].upper()


def _identifiers(s: Session, deal: Deal) -> list[str]:
    ids: list[str] = []
    contact = s.get(Contact, deal.contact_id)
    if contact:
        ids += [contact.email, f"{contact.first_name} {contact.last_name}".strip(), contact.first_name,
                contact.last_name, contact.phone, contact.email.rsplit("@", 1)[1]]
    company = s.get(Company, deal.company_id) if deal.company_id else None
    if company:
        ids += [company.name, company.website]
    # Longest first so "Acme Ltd" is replaced before "Acme".
    return sorted({i for i in ids if i and len(i.strip()) >= 2}, key=len, reverse=True)


def anonymise(s: Session, deal: Deal, text: str) -> str:
    name = code_name(deal.id)
    for ident in _identifiers(s, deal):
        text = re.sub(re.escape(ident), name, text, flags=re.IGNORECASE)
    return text


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
    data = {"partner_name": partner.name, "client_code": code_name(deal.id), "scope": anonymise(s, deal, scope),
            "region": partner.region}
    doc = generate(s, kind="partner_brief", title=f"Partner brief for {partner.name} ({data['client_code']})",
                   data=data, deal_id=deal.id, actor=actor)
    leaks = [i for i in _identifiers(s, deal) if i.lower() in doc.markdown.lower() or i.lower() in doc.html.lower()]
    if leaks:  # defence in depth: never store a brief that still identifies the client
        raise PartnerError("brief still contains client-identifying information; refused")
    audit(s, actor, "partner.brief_prepared", doc.id, partner_id=partner.id, deal_id=deal.id)
    return doc
