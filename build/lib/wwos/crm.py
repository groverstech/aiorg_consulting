"""CRM service: contacts, companies, deals and activity history.

All functions take a tenant-scoped session, so cross-tenant access is impossible
by construction. Validation lives here (not in the API) so agents, workflows and
HTTP callers all get the same rules.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Activity, Company, Contact, Deal

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

STAGES = ["lead", "qualified", "proposal", "negotiation", "won", "lost"]
# Allowed forward/back transitions. won/lost are terminal except reopening lost -> qualified.
TRANSITIONS = {
    "lead": {"qualified", "lost"},
    "qualified": {"proposal", "lost", "lead"},
    "proposal": {"negotiation", "won", "lost", "qualified"},
    "negotiation": {"won", "lost", "proposal"},
    "won": set(),
    "lost": {"qualified"},
}


class CRMValidationError(ValueError):
    pass


def _norm_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise CRMValidationError(f"invalid email address: {email!r}")
    return email


def get_or_create_company(s: Session, name: str, **fields) -> Company:
    name = (name or "").strip()
    if not name:
        raise CRMValidationError("company name is required")
    existing = s.scalars(select(Company).where(func.lower(Company.name) == name.lower())).first()
    if existing:
        return existing
    company = Company(name=name, **{k: v for k, v in fields.items() if v is not None})
    s.add(company)
    s.flush()
    return company


def upsert_contact(s: Session, *, email: str, first_name: str, last_name: str = "", phone: str = "",
                   company: str | None = None, source: str = "manual", notes: str = "",
                   actor: str = "system") -> tuple[Contact, bool]:
    """Create a contact, or return the existing one with the same email. Returns (contact, created)."""
    email = _norm_email(email)
    if not (first_name or "").strip():
        raise CRMValidationError("first_name is required")
    existing = s.scalars(select(Contact).where(Contact.email == email)).first()
    company_id = get_or_create_company(s, company).id if company else None
    if existing:
        if company_id and not existing.company_id:
            existing.company_id = company_id
        if phone and not existing.phone:
            existing.phone = phone
        return existing, False
    contact = Contact(email=email, first_name=first_name.strip(), last_name=last_name.strip(),
                      phone=phone, company_id=company_id, source=source, notes=notes)
    s.add(contact)
    s.flush()
    log_activity(s, kind="note", summary=f"Contact created from {source}", contact_id=contact.id, actor=actor)
    return contact, True


def create_deal(s: Session, *, contact_id: str, title: str, value: str | float | Decimal = 0,
                currency: str = "USD", owner_agent_id: str | None = None, actor: str = "system") -> Deal:
    contact = s.get(Contact, contact_id)
    if contact is None:
        raise CRMValidationError("contact not found")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as e:
        raise CRMValidationError(f"invalid deal value: {value!r}") from e
    if amount < 0:
        raise CRMValidationError("deal value cannot be negative")
    if not (title or "").strip():
        raise CRMValidationError("deal title is required")
    if not re.fullmatch(r"[A-Z]{3}", currency or ""):
        raise CRMValidationError("currency must be a 3-letter ISO code")
    deal = Deal(title=title.strip(), contact_id=contact.id, company_id=contact.company_id,
                value=amount, currency=currency, owner_agent_id=owner_agent_id)
    s.add(deal)
    s.flush()
    log_activity(s, kind="stage_change", summary=f"Deal opened at stage lead ({currency} {amount})",
                 contact_id=contact.id, deal_id=deal.id, actor=actor)
    return deal


def move_deal(s: Session, deal_id: str, stage: str, actor: str = "system") -> Deal:
    deal = s.get(Deal, deal_id)
    if deal is None:
        raise CRMValidationError("deal not found")
    if stage not in STAGES:
        raise CRMValidationError(f"unknown stage {stage!r}")
    if stage == deal.stage:
        return deal
    if stage not in TRANSITIONS[deal.stage]:
        raise CRMValidationError(f"cannot move deal from {deal.stage} to {stage}")
    previous = deal.stage
    deal.stage = stage
    if stage == "won":
        contact = s.get(Contact, deal.contact_id)
        if contact:
            contact.lifecycle = "customer"
    log_activity(s, kind="stage_change", summary=f"Stage {previous} -> {stage}", contact_id=deal.contact_id,
                 deal_id=deal.id, actor=actor)
    return deal


def log_activity(s: Session, *, kind: str, summary: str, contact_id: str | None = None,
                 deal_id: str | None = None, actor: str = "system") -> Activity:
    # Referenced ids are looked up through the (tenant-scoped) session so a foreign id is "not found".
    if contact_id is not None and s.get(Contact, contact_id) is None:
        raise CRMValidationError("contact not found")
    if deal_id is not None and s.get(Deal, deal_id) is None:
        raise CRMValidationError("deal not found")
    act = Activity(kind=kind, summary=summary, contact_id=contact_id, deal_id=deal_id, actor=actor)
    s.add(act)
    return act


def pipeline_summary(s: Session) -> dict[str, dict[str, str | int]]:
    rows = s.execute(select(Deal.stage, func.count(Deal.id), func.coalesce(func.sum(Deal.value), 0))
                     .group_by(Deal.stage)).all()
    out = {stage: {"count": 0, "value": "0.00"} for stage in STAGES}
    for stage, count, total in rows:
        out[stage] = {"count": int(count), "value": f"{Decimal(str(total)):.2f}"}
    return out
