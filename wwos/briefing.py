"""Rules engine: the daily operating briefing and stalled-deal follow-ups.

Runs every day for every tenant with or without AI (it never calls a model, so it costs nothing).
In zero-spend mode it replaces the CEO's and Operations' AI reviews.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import budget, crm, documents
from .models import (Activity, ApprovalRequest, AuditLog, Contact, Deal, Document, OutboundMessage, Partner, Task,
                     Tenant, utcnow)

STALL_DAYS = 7
PARTNER_STALL_DAYS = 14


def _last_activity(s: Session, deal_id: str):
    return s.execute(select(func.max(Activity.created_at)).where(Activity.deal_id == deal_id)).scalar_one()


def _aware(dt):
    from datetime import timezone

    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def stalled_deals(s: Session, now) -> list[Deal]:
    cutoff = now - timedelta(days=STALL_DAYS)
    out = []
    for d in s.scalars(select(Deal).where(Deal.stage.in_(["proposal", "negotiation"])).order_by(Deal.created_at)):
        last = _aware(_last_activity(s, d.id) or d.updated_at)
        if last is None or last < cutoff:
            out.append(d)
    return out


def _follow_up(s: Session, tenant: Tenant, deal: Deal, now, trace) -> str | None:
    """Draft a follow-up for a stalled proposal (once per deal per ISO week) and request sending,
    which goes to owner approval under the default e-mail policy."""
    from .agents.runtime import execute_tool, find_agent
    from .tools import ToolContext

    sales = find_agent(s, role="sales")
    contact = s.get(Contact, deal.contact_id)
    if sales is None or contact is None:
        return None
    year, week, _ = now.isocalendar()
    ctx = ToolContext(session=s, tenant=tenant, agent=sales)
    draft, err = execute_tool(ctx, "comms.draft_email", {
        "idempotency_key": f"followup:{deal.id}:{year}-W{week}",
        "to": contact.email,
        "subject": f"Following up: {deal.title}",
        "body": (f"Dear {contact.first_name},\n\nI hope you are well. I wanted to follow up on our proposal for "
                 f"\"{deal.title}\" and see whether you have any questions or would like to discuss next steps.\n\n"
                 f"Kind regards,\n{documents.COMPANY['name']}"),
        "contact_id": contact.id,
    }, trace)
    if err or not draft.get("created"):
        return None
    execute_tool(ctx, "comms.send_email", {"message_id": draft["message_id"]}, trace)
    msg = s.get(OutboundMessage, draft["message_id"])
    if msg is not None and msg.status == "draft":
        msg.status = "pending_approval"
    crm.log_activity(s, kind="email", summary="Follow-up drafted by the rules engine", deal_id=deal.id,
                     contact_id=contact.id, actor="rules")
    return draft["message_id"]


def run(s: Session, tenant: Tenant, task: Task, trace) -> dict:
    from .llm import ai_enabled
    from .schedule import last_qa

    now = utcnow()
    day = task.payload.get("date") or now.strftime("%Y-%m-%d")
    approvals = dict(s.execute(select(ApprovalRequest.category, func.count(ApprovalRequest.id))
                               .where(ApprovalRequest.status == "pending").group_by(ApprovalRequest.category)).all())
    pipeline = crm.pipeline_summary(s)
    stalled = stalled_deals(s, now)
    followups = [m for m in (_follow_up(s, tenant, d, now, trace) for d in stalled if d.stage == "proposal") if m]
    new_leads = s.execute(select(func.count(Contact.id)).where(Contact.created_at >= now - timedelta(days=1))).scalar_one()
    partner_stages = dict(s.execute(select(Partner.pipeline_stage, func.count(Partner.id))
                                    .group_by(Partner.pipeline_stage)).all())
    stale_partners = []
    for p in s.scalars(select(Partner).where(Partner.status == "prospect",
                                              Partner.pipeline_stage.in_(["contacted", "in_discussion", "nda_requested"]))):
        last = _aware(s.execute(select(func.max(AuditLog.created_at)).where(AuditLog.target == p.id)).scalar_one())
        if last is None or last < now - timedelta(days=PARTNER_STALL_DAYS):
            stale_partners.append(f"{p.name} ({p.pipeline_stage})")
    since = now - timedelta(days=1)
    ops = {
        "dead_tasks": s.execute(select(func.count(Task.id)).where(Task.status == "dead", Task.updated_at >= since)).scalar_one(),
        "skipped_tasks": s.execute(select(func.count(Task.id)).where(Task.status == "skipped", Task.updated_at >= since)).scalar_one(),
        "emails_to_send_by_hand": s.execute(select(func.count(OutboundMessage.id)).where(OutboundMessage.status == "manual")).scalar_one(),
        "emails_unknown_or_failed": s.execute(select(func.count(OutboundMessage.id))
                                              .where(OutboundMessage.status.in_(["unknown", "failed"]))).scalar_one(),
    }
    qa = last_qa()
    data = {
        "day": day,
        "ai": "on" if ai_enabled() else "off (zero-spend mode)",
        "ai_spend": f"{budget.spent_this_month(s):.2f}",
        "approvals": [{"category": k, "count": v} for k, v in sorted(approvals.items())],
        "pipeline": [{"stage": k, "count": v["count"], "value": v["value"]} for k, v in pipeline.items()],
        "stalled": [{"title": d.title, "stage": d.stage, "value": str(d.value)} for d in stalled],
        "followups": len(followups),
        "new_leads": new_leads,
        "partner_stages": [{"stage": k, "count": v} for k, v in sorted(partner_stages.items())],
        "stale_partners": stale_partners,
        "ops": ops,
        "qa": qa.get("summary") or "no scan yet",
    }
    doc = documents.generate(s, kind="briefing", title=f"Daily briefing {day}", data=data, actor="rules", internal=True)
    return {"document_id": doc.id, "stalled": len(stalled), "followups": len(followups)}
