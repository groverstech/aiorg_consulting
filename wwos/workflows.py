"""Durable task queue, worker and business workflows.

Reliability properties:

* **Idempotent enqueue** - every task carries a (tenant, idempotency_key) unique
  key; re-enqueueing returns the existing task instead of duplicating work.
* **Atomic steps** - each task runs in a single transaction; a crash mid-step
  leaves no partial business state, and the step is retried.
* **Leases** - a claimed task holds a lease; if the worker dies the lease
  expires and another worker reclaims it.
* **Bounded retries with backoff**, then dead-lettering with the error kept.
* **Spend survives failure** - model spend from a failed run is re-recorded
  after rollback so budgets cannot be bypassed by failing.
* **Self-healing agents** - repeated failures suspend an agent and hand its
  recovery to the Technology agent automatically.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import crm, documents
from .agents.runtime import RunTrace, execute_tool, find_agent, persist_spend, run_agent_prompt
from .budget import BudgetExceeded
from .config import get_settings
from .db import system_session, tenant_session
from .llm import PermanentLLMError
from .models import Agent, ApprovalRequest, Contact, Deal, Task, Tenant, utcnow
from .policy import PermissionDenied, audit
from .tools import ToolContext

log = logging.getLogger("wwos.worker")

NON_RETRYABLE = (PermanentLLMError, PermissionDenied, BudgetExceeded, crm.CRMValidationError,
                 documents.DocumentError)


def enqueue(s: Session, *, kind: str, idempotency_key: str, payload: dict[str, Any] | None = None,
            agent_id: str | None = None, workflow_id: str | None = None,
            delay_seconds: int = 0) -> tuple[Task, bool]:
    existing = s.scalars(select(Task).where(Task.idempotency_key == idempotency_key)).first()
    if existing:
        return existing, False
    task = Task(kind=kind, idempotency_key=idempotency_key, payload=payload or {}, agent_id=agent_id,
                workflow_id=workflow_id, run_after=utcnow() + timedelta(seconds=delay_seconds))
    try:
        with s.begin_nested():
            s.add(task)
            s.flush()
    except IntegrityError:  # concurrent enqueue of the same key
        return s.scalars(select(Task).where(Task.idempotency_key == idempotency_key)).one(), False
    return task, True


# ---------------------------------------------------------------- handlers

HANDLERS: dict[str, Callable[[Session, Tenant, Task, RunTrace], dict]] = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


@handler("agent_run")
def _agent_run(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    agent = s.get(Agent, task.agent_id) if task.agent_id else None
    if agent is None:
        raise PermissionDenied("task has no valid agent")
    text = run_agent_prompt(s, tenant, agent, task.payload["instruction"], task_id=task.id, trace=trace)
    return {"summary": text, "tool_calls": trace.tool_calls, "approvals": trace.approvals}


@handler("approval.execute")
def _approval_execute(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    from .tools import TOOLS

    req = s.get(ApprovalRequest, task.payload["approval_id"])
    if req is None or req.status not in ("approved",):
        return {"skipped": True, "reason": "approval not in approved state"}
    ctx = ToolContext(session=s, tenant=tenant, agent=None, task_id=task.id)
    with s.begin_nested():
        result = TOOLS[req.action].handler(ctx, **req.params)
    req.status = "executed"
    req.result = result
    audit(s, req.decided_by or "owner", "approval.executed", req.id, action=req.action)
    return result


# ---- Lead -> qualification -> proposal -> outreach workflow


def _qualification_score(p: dict) -> tuple[int, list[str]]:
    score, reasons = 0, []
    if p.get("company"):
        score += 25
        reasons.append("company identified")
    if len((p.get("need") or "").split()) >= 6:
        score += 35
        reasons.append("clear business need stated")
    try:
        value = Decimal(str(p.get("estimated_value") or 0))
    except Exception:  # noqa: BLE001
        value = Decimal(0)
    if value >= Decimal(str(p.get("min_deal_value", 2000))):
        score += 40
        reasons.append("budget meets minimum engagement size")
    return score, reasons


@handler("workflow.lead_intake")
def _wf_intake(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    p = task.payload
    sales = find_agent(s, role="sales")
    contact, created = crm.upsert_contact(s, email=p["email"], first_name=p["first_name"],
                                          last_name=p.get("last_name", ""), company=p.get("company"),
                                          source=p.get("source", "web"), notes=p.get("need", ""),
                                          actor="workflow")
    deal = crm.create_deal(s, contact_id=contact.id, title=p.get("title") or f"Engagement - {p.get('company') or contact.email}",
                           value=p.get("estimated_value") or 0, currency=p.get("currency", "USD"),
                           owner_agent_id=sales.id if sales else None, actor="workflow")
    enqueue(s, kind="workflow.qualify", idempotency_key=f"wf:{task.workflow_id}:qualify",
            payload={**p, "contact_id": contact.id, "deal_id": deal.id}, workflow_id=task.workflow_id)
    return {"contact_id": contact.id, "deal_id": deal.id, "new_contact": created}


@handler("workflow.qualify")
def _wf_qualify(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    p = task.payload
    score, reasons = _qualification_score(p)
    qualified = score >= 60
    crm.move_deal(s, p["deal_id"], "qualified" if qualified else "lost", actor="workflow")
    contact = s.get(Contact, p["contact_id"])
    contact.lifecycle = "qualified" if qualified else "disqualified"
    note = f"Qualification score {score}/100: {', '.join(reasons) or 'insufficient information'}."
    sales = find_agent(s, role="sales")
    if sales is not None:
        # The sales agent adds its own assessment; failure here must not block the deterministic decision.
        try:
            with s.begin_nested():
                assessment = run_agent_prompt(
                    s, tenant, sales,
                    f"Write a two-sentence qualification note for this lead. Decision already made: "
                    f"{'QUALIFIED' if qualified else 'NOT QUALIFIED'} ({note}). Need: {p.get('need', '')!r}. "
                    f"Company: {p.get('company', '')!r}.",
                    max_tokens=600, allow_tools=False, task_id=task.id, trace=trace)
            if assessment:
                note += f"\nSales agent: {assessment}"
        except Exception as e:  # noqa: BLE001 - the assessment is advisory only
            note += f"\n(Sales agent assessment unavailable: {type(e).__name__}: {e})"
    crm.log_activity(s, kind="note", summary=note, contact_id=contact.id, deal_id=p["deal_id"], actor="workflow")
    if qualified:
        enqueue(s, kind="workflow.proposal", idempotency_key=f"wf:{task.workflow_id}:proposal",
                payload=p, workflow_id=task.workflow_id)
    return {"qualified": qualified, "score": score}


@handler("workflow.proposal")
def _wf_proposal(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    p = task.payload
    deal = s.get(Deal, p["deal_id"])
    scope = p.get("scope_items") or [
        "Discovery workshops and current-state assessment",
        "Recommendations report with prioritised roadmap",
        "Implementation support and progress reviews",
    ]
    doc = documents.generate(s, kind="proposal", title=f"Proposal - {p.get('company') or p['email']}", deal_id=deal.id,
                             data={"client_name": p.get("company") or f"{p['first_name']} {p.get('last_name', '')}".strip(),
                                   "objective": p.get("need") or "To be confirmed in discovery.",
                                   "scope_items": scope, "fee": str(deal.value), "currency": deal.currency,
                                   "valid_days": 30}, actor="workflow")
    crm.move_deal(s, deal.id, "proposal", actor="workflow")
    crm.log_activity(s, kind="document", summary=f"Proposal generated ({doc.id})", deal_id=deal.id,
                     contact_id=deal.contact_id, actor="workflow")
    enqueue(s, kind="workflow.outreach", idempotency_key=f"wf:{task.workflow_id}:outreach",
            payload={**p, "document_id": doc.id}, workflow_id=task.workflow_id)
    return {"document_id": doc.id}


@handler("workflow.outreach")
def _wf_outreach(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    from .models import Document

    p = task.payload
    sales = find_agent(s, role="sales")
    if sales is None:
        raise PermissionDenied("no active sales agent to send outreach")
    doc = s.get(Document, p["document_id"])
    ctx = ToolContext(session=s, tenant=tenant, agent=sales, task_id=task.id)
    draft, err = execute_tool(ctx, "comms.draft_email", {
        "idempotency_key": f"wf:{task.workflow_id}:proposal-email",
        "to": p["email"],
        "subject": f"Your proposal from {documents.COMPANY['name']}",
        "body": f"Dear {p['first_name']},\n\nThank you for speaking with us. Please find our proposal below.\n\n"
                f"{doc.markdown}\n\nKind regards,\n{documents.COMPANY['name']}",
        "contact_id": p["contact_id"],
    }, trace)
    if err:
        raise RuntimeError(f"draft failed: {draft}")
    sent, err = execute_tool(ctx, "comms.send_email", {"message_id": draft["message_id"]}, trace)
    if err:
        raise RuntimeError(f"send failed: {sent}")
    if sent.get("status") == "pending_owner_approval":
        from .models import OutboundMessage

        msg = s.get(OutboundMessage, draft["message_id"])
        msg.status = "pending_approval"
    return {"message_id": draft["message_id"], "send": sent}


def start_lead_workflow(s: Session, lead: dict[str, Any], idempotency_key: str | None = None) -> Task:
    crm._norm_email(lead.get("email", ""))  # validate early so bad input fails at the API
    if not lead.get("first_name"):
        raise crm.CRMValidationError("first_name is required")
    key = idempotency_key or f"lead:{lead['email'].strip().lower()}:{lead.get('title') or lead.get('company') or ''}"
    from .models import new_id

    existing = s.scalars(select(Task).where(Task.idempotency_key == key)).first()
    if existing:
        return existing
    task, _ = enqueue(s, kind="workflow.lead_intake", idempotency_key=key, payload=lead, workflow_id=new_id())
    return task


# ---------------------------------------------------------------- worker


def claim_next(now=None) -> tuple[str, str] | None:
    """Atomically claim one due task across tenants. Returns (task_id, tenant_id)."""
    cfg = get_settings()
    now = now or utcnow()
    with system_session() as s:
        candidates = s.execute(
            select(Task.id, Task.tenant_id, Task.status).where(
                or_(
                    (Task.status == "queued") & (Task.run_after <= now),
                    (Task.status == "running") & (Task.lease_until < now),
                )
            ).order_by(Task.run_after).limit(10)
        ).all()
        for task_id, tenant_id, status in candidates:
            res = s.execute(
                update(Task).where(Task.id == task_id, Task.status == status)
                .values(status="running", lease_until=now + timedelta(seconds=cfg.task_lease_seconds),
                        attempts=Task.attempts + 1)
            )
            if res.rowcount == 1:
                s.commit()
                return task_id, tenant_id
    return None


def process(task_id: str, tenant_id: str) -> str:
    """Run one claimed task. Returns its final status."""
    cfg = get_settings()
    trace = RunTrace()
    error: Exception | None = None
    try:
        with tenant_session(tenant_id) as s:
            task = s.get(Task, task_id)
            tenant = s.get(Tenant, tenant_id)
            fn = HANDLERS.get(task.kind)
            if fn is None:
                raise PermissionDenied(f"no handler for task kind {task.kind}")
            result = fn(s, tenant, task, trace)
            persist_spend(s, trace)
            task.status = "succeeded"
            task.result = result
            task.last_error = ""
            task.lease_until = None
            if task.agent_id:
                agent = s.get(Agent, task.agent_id)
                if agent:
                    agent.consecutive_failures = 0
        return "succeeded"
    except Exception as e:  # noqa: BLE001
        error = e
    # Failure path: the step's transaction was rolled back. Record what must survive.
    trace.persisted = 0  # the failed transaction rolled back every ledger write
    with tenant_session(tenant_id) as s:
        persist_spend(s, trace)
        task = s.get(Task, task_id)
        task.last_error = f"{type(error).__name__}: {error}"[:4000]
        task.lease_until = None
        retryable = not isinstance(error, NON_RETRYABLE)
        if retryable and task.attempts < cfg.task_max_attempts:
            task.status = "queued"
            task.run_after = utcnow() + timedelta(seconds=min(2 ** task.attempts * 5, 600))
        else:
            task.status = "dead"
        audit(s, "worker", "task.failed", task.id, kind=task.kind, error=task.last_error, final=task.status)
        if task.agent_id:
            _record_agent_failure(s, task.agent_id, task.last_error)
        return task.status


def _record_agent_failure(s: Session, agent_id: str, error: str) -> None:
    cfg = get_settings()
    agent = s.get(Agent, agent_id)
    if agent is None or agent.status != "active":
        return
    if "BudgetExceeded" in error:
        return  # budget exhaustion is not a malfunction
    agent.consecutive_failures += 1
    if agent.consecutive_failures >= cfg.agent_failure_suspend_threshold:
        agent.status = "suspended"
        agent.status_reason = f"auto-suspended after {agent.consecutive_failures} consecutive failures: {error[:300]}"
        audit(s, "system", "agent.auto_suspended", agent.id, reason=agent.status_reason)
        tech = find_agent(s, role="technology")
        if tech is not None and tech.id != agent.id:
            enqueue(s, kind="agent_run", agent_id=tech.id,
                    idempotency_key=f"recover:{agent.id}:{agent.updated_at.isoformat() if agent.updated_at else ''}",
                    payload={"instruction": (
                        f"Agent {agent.name} (id {agent.id}) was auto-suspended: {agent.status_reason}. "
                        "Diagnose the likely cause from its instructions and recent errors, then either "
                        "replace it with agents.replace or publish an improved version with agents.revise."
                    )})


def run_worker(poll_seconds: float = 2.0, max_tasks: int | None = None, stop_when_idle: bool = False,
               scheduled: bool = False) -> int:
    from . import schedule

    done = 0
    last_tick = 0.0
    while max_tasks is None or done < max_tasks:
        if scheduled and time.monotonic() - last_tick >= 60:
            try:
                schedule.tick()
            except Exception:  # noqa: BLE001 - scheduling must never stop task processing
                log.exception("schedule tick failed")
            last_tick = time.monotonic()
        claimed = claim_next()
        if claimed is None:
            if stop_when_idle:
                break
            time.sleep(poll_seconds)
            continue
        status = process(*claimed)
        log.info("task %s -> %s", claimed[0], status)
        done += 1
    return done


def drain(max_tasks: int = 500) -> int:
    """Process every due task now (used by tests, CLI and QA)."""
    return run_worker(max_tasks=max_tasks, stop_when_idle=True)
