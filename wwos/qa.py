"""Independent QA function: invariant checks over a live database.

This module deliberately does NOT reuse the service code it audits. It reads
raw tables through a system session and checks the business invariants
directly, so a bug in a service cannot hide itself from QA. It is read-only.

Run with ``wwos qa`` (exit code 1 on any failure). The automated pytest suite in
``tests/acceptance`` is the other half of the QA function.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from .budget import ALLOWED_MODELS, month_start
from .db import system_session
from .models import (Activity, Agent, ApprovalRequest, Contact, CostEntry, Deal, Document, OutboundMessage, Task,
                     Tenant)
from .policy import OWNER_AUTHORITY_CATEGORIES, PROVISIONING_TOOLS


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(self, name: str, problems: list[str]) -> None:
        self.checks.append(Check(name, not problems, "; ".join(problems[:10])))

    def render(self) -> str:
        lines = [f"[{'PASS' if c.passed else 'FAIL'}] {c.name}" + (f" - {c.detail}" if c.detail else "")
                 for c in self.checks]
        lines.append(f"\n{sum(c.passed for c in self.checks)}/{len(self.checks)} checks passed")
        return "\n".join(lines)


def _cross_tenant(s, child, fk_col, parent) -> list[str]:
    P = aliased(parent)
    rows = s.execute(select(child.id).join(P, fk_col == P.id).where(child.tenant_id != P.tenant_id)).all()
    return [f"{child.__name__} {r[0]} references another tenant's {parent.__name__}" for r in rows]


def run() -> Report:
    from .tools import TOOLS

    rep = Report()
    with system_session() as s:
        # 1. Data isolation: no row references another tenant's row.
        problems = []
        problems += _cross_tenant(s, Deal, Deal.contact_id, Contact)
        problems += _cross_tenant(s, Activity, Activity.deal_id, Deal)
        problems += _cross_tenant(s, Activity, Activity.contact_id, Contact)
        problems += _cross_tenant(s, Document, Document.deal_id, Deal)
        problems += _cross_tenant(s, CostEntry, CostEntry.agent_id, Agent)
        problems += _cross_tenant(s, OutboundMessage, OutboundMessage.contact_id, Contact)
        rep.add("Data isolation: no cross-tenant references", problems)

        # 2. Agent permissions.
        problems = []
        for a in s.scalars(select(Agent).where(Agent.status != "retired")):
            unknown = set(a.tools or []) - set(TOOLS)
            if unknown:
                problems.append(f"{a.name} has unknown tools {sorted(unknown)}")
            if set(a.tools or []) & PROVISIONING_TOOLS and not a.can_provision_agents:
                problems.append(f"{a.name} holds management tools without provisioning authority")
            if a.can_provision_agents and a.created_by != "owner":
                problems.append(f"{a.name} has provisioning authority but was created by {a.created_by}")
            if a.model not in ALLOWED_MODELS:
                problems.append(f"{a.name} uses unapproved model {a.model}")
        rep.add("Agent permissions: privilege containment holds", problems)

        # 3. Budget enforcement.
        problems = []
        for t in s.scalars(select(Tenant)):
            spent = Decimal(str(s.execute(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(
                CostEntry.tenant_id == t.id, CostEntry.created_at >= month_start())).scalar_one()))
            if spent > t.monthly_budget_usd:
                problems.append(f"{t.name} spent ${spent} over budget ${t.monthly_budget_usd}")
            allocated = Decimal(str(s.execute(select(func.coalesce(func.sum(Agent.monthly_budget_usd), 0)).where(
                Agent.tenant_id == t.id, Agent.status != "retired")).scalar_one()))
            if allocated > t.monthly_budget_usd:
                problems.append(f"{t.name} allocated ${allocated} over budget ${t.monthly_budget_usd}")
            lineages: dict[str, list[Agent]] = {}
            for a in s.scalars(select(Agent).where(Agent.tenant_id == t.id)):
                lineages.setdefault(a.lineage_id, []).append(a)
            for versions in lineages.values():
                cap = max(v.monthly_budget_usd for v in versions)
                l_spent = Decimal(str(s.execute(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(
                    CostEntry.agent_id.in_([v.id for v in versions]),
                    CostEntry.created_at >= month_start())).scalar_one()))
                if l_spent > cap:
                    problems.append(f"agent lineage {versions[0].name} spent ${l_spent} over ${cap}")
            for a in s.scalars(select(Agent).where(Agent.tenant_id == t.id)):
                a_spent = Decimal(str(s.execute(select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(
                    CostEntry.agent_id == a.id, CostEntry.created_at >= month_start())).scalar_one()))
                if a_spent > a.monthly_budget_usd:
                    problems.append(f"agent {a.name} v{a.version} spent ${a_spent} over ${a.monthly_budget_usd}")
        rep.add("Budget: no tenant or agent over its ceiling", problems)

        # 4. Owner authority: owner-gated actions only executed after approval.
        problems = []
        for r in s.scalars(select(ApprovalRequest)):
            if r.category not in OWNER_AUTHORITY_CATEGORIES:
                problems.append(f"approval {r.id} has unknown category {r.category}")
            if r.status in ("approved", "executed", "rejected") and not r.decided_by:
                problems.append(f"approval {r.id} is {r.status} without a recorded decision-maker")
        rep.add("Owner authority: every decision is attributable", problems)

        # 5. Communications: nothing pending approval was sent; sent mail has a timestamp.
        problems = []
        for m in s.scalars(select(OutboundMessage).where(OutboundMessage.status == "sent")):
            if m.sent_at is None:
                problems.append(f"message {m.id} marked sent without sent_at")
            gate = s.scalars(select(ApprovalRequest).where(
                ApprovalRequest.tenant_id == m.tenant_id, ApprovalRequest.action == "comms.send_email")).all()
            gated = [g for g in gate if (g.params or {}).get("message_id") == m.id]
            if gated and not any(g.status in ("approved", "executed") for g in gated):
                problems.append(f"message {m.id} was sent although its approval is not granted")
        rep.add("Communications: approval gates respected", problems)

        # 6. CRM accuracy.
        problems = []
        valid = {"lead", "qualified", "proposal", "negotiation", "won", "lost"}
        for d in s.scalars(select(Deal)):
            if d.stage not in valid:
                problems.append(f"deal {d.id} has invalid stage {d.stage}")
            if d.value is not None and d.value < 0:
                problems.append(f"deal {d.id} has negative value")
        dupes = s.execute(select(Contact.tenant_id, Contact.email, func.count()).group_by(
            Contact.tenant_id, Contact.email).having(func.count() > 1)).all()
        problems += [f"duplicate contact {e}" for _, e, _ in dupes]
        rep.add("CRM: valid stages, values and unique contacts", problems)

        # 7. Documents: invoice totals reconcile.
        problems = []
        for d in s.scalars(select(Document).where(Document.kind == "invoice")):
            items = d.data.get("line_items", [])
            sub = sum(Decimal(str(i.get("quantity", 1))) * Decimal(str(i["unit_price"])) for i in items)
            tax = (sub * Decimal(str(d.data.get("tax_rate", 0))) / 100).quantize(Decimal("0.01"))
            if "total" in d.data and Decimal(d.data["total"]) != (sub + tax).quantize(Decimal("0.01")):
                problems.append(f"invoice {d.id} total {d.data['total']} != {sub + tax}")
        rep.add("Documents: invoice arithmetic reconciles", problems)

        # 8. Confidentiality: partner briefs never identify the client; partner mail is always approved.
        from .models import Partner

        problems = []
        for d in s.scalars(select(Document).where(Document.kind == "partner_brief")):
            deal = s.get(Deal, d.deal_id) if d.deal_id else None
            contact = s.get(Contact, deal.contact_id) if deal else None
            idents = [contact.email, contact.first_name if len(contact.first_name) > 2 else None] if contact else []
            if deal and deal.company_id:
                from .models import Company

                comp = s.get(Company, deal.company_id)
                idents.append(comp.name if comp else None)
            text = (d.markdown + d.html).lower()
            leaked = [i for i in idents if i and i.lower() in text]
            if leaked:
                problems.append(f"partner brief {d.id} contains client identifiers")
        partner_domains = {(p.tenant_id, p.email_domain): p for p in s.scalars(select(Partner))}
        for m in s.scalars(select(OutboundMessage).where(OutboundMessage.status.in_(["queued", "sending", "sent"]))):
            p = partner_domains.get((m.tenant_id, m.to_address.rsplit("@", 1)[-1]))
            if p is None:
                continue
            if p.nda_status != "on_file":
                problems.append(f"message {m.id} to partner {p.name} without an NDA on file")
            approved = [a for a in s.scalars(select(ApprovalRequest).where(
                ApprovalRequest.tenant_id == m.tenant_id, ApprovalRequest.category == "confidential_disclosure",
                ApprovalRequest.status.in_(["approved", "executed"])))
                if (a.params or {}).get("message_id") == m.id or (a.result or {}).get("message_id") == m.id]
            if not approved:
                problems.append(f"message {m.id} to partner {p.name} without an approved disclosure")
        rep.add("Confidentiality: partner briefs anonymised, partner mail approved", problems)

        # 9. Workflow reliability.
        problems = []
        dead = s.execute(select(Task.id, Task.kind, Task.last_error).where(Task.status == "dead")).all()
        problems += [f"dead task {k} {i}: {e[:120]}" for i, k, e in dead]
        dup = s.execute(select(Task.tenant_id, Task.idempotency_key, func.count()).group_by(
            Task.tenant_id, Task.idempotency_key).having(func.count() > 1)).all()
        problems += [f"duplicate task key {k}" for _, k, _ in dup]
        rep.add("Workflows: no dead-lettered or duplicated tasks", problems)
    return rep
