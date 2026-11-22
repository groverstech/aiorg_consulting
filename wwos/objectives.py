"""CEO objectives: autonomous decomposition, delegation, QA, retry/reassignment and reporting.

Lifecycle of an objective (each step is a durable, idempotent queue task):

1. ``objective.plan``   - the CEO agent reviews the workforce, provisions any missing specialist
                          (agents.provision: inherited permissions, budget-contained) and records
                          department tasks with ``objectives.add_task``.
2. ``objective.run``    - each task runs as its assigned agent, within that agent's own tools and
                          budget. Errors are caught and handled by the coordinator (step 4).
3. ``objective.qa``     - the QA Auditor reviews the result and records a verdict with
                          ``objectives.qa_verdict``. No verdict counts as a failure.
4. coordinator          - failed or rejected work is retried by the same agent (with the QA notes)
                          up to MAX_ATTEMPTS, then reassigned once (another active agent in the same
                          department, else the Operations Manager), then marked failed.
5. ``objective.report`` - the CEO writes an executive summary; the system adds the factual task
                          table, new agents and AI spend, stores an ``executive_report`` document
                          and logs the outcome on the linked CRM deal.

Coordination rules are deterministic code; judgement (planning, the work itself, QA verdicts,
narrative) comes from the agents. With the offline/simulated provider this demonstrates the
mechanics only, not live-model quality.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import crm, documents
from .agents.runtime import RunTrace, find_agent, run_agent_prompt
from .models import (Agent, CostEntry, Deal, Department, Objective, ObjectiveTask, Task, Tenant, utcnow)
from .policy import audit

MAX_ATTEMPTS = 2


class ObjectiveError(ValueError):
    pass


# ---------------------------------------------------------------- services used by API and tools

def create(s: Session, *, title: str, description: str, deal_id: str | None = None, actor: str = "owner") -> Objective:
    from .workflows import enqueue

    if not title.strip() or len(description.strip()) < 20:
        raise ObjectiveError("objective needs a title and a description of at least 20 characters")
    if deal_id is not None and s.get(Deal, deal_id) is None:
        raise ObjectiveError("deal not found")
    obj = Objective(title=title.strip(), description=description.strip(), deal_id=deal_id, created_by=actor)
    s.add(obj)
    s.flush()
    enqueue(s, kind="objective.plan", idempotency_key=f"obj:{obj.id}:plan", workflow_id=obj.id,
            payload={"objective_id": obj.id})
    audit(s, actor, "objective.created", obj.id, title=obj.title)
    return obj


def department_agent(s: Session, code: str, exclude: set[str] | None = None) -> Agent | None:
    dept = s.scalars(select(Department).where(Department.code == code)).first()
    if dept is None:
        return None
    q = select(Agent).where(Agent.department_id == dept.id, Agent.status == "active").order_by(
        Agent.can_provision_agents, Agent.created_at)
    for a in s.scalars(q):
        if not exclude or a.id not in exclude:
            return a
    return None


def add_task(s: Session, *, objective_id: str, department_code: str, title: str, instruction: str,
             agent_id: str | None = None, actor: str) -> ObjectiveTask:
    obj = s.get(Objective, objective_id)
    if obj is None:
        raise ObjectiveError("objective not found")
    if obj.status != "planning":
        raise ObjectiveError(f"objective is {obj.status}; tasks can only be added while planning")
    if s.scalars(select(Department).where(Department.code == department_code)).first() is None:
        raise ObjectiveError(f"unknown department {department_code!r}")
    if agent_id is not None:
        a = s.get(Agent, agent_id)
        if a is None or a.status != "active":
            raise ObjectiveError("assigned agent not found or not active")
        if a.can_provision_agents:
            raise ObjectiveError("tasks cannot be assigned to provisioning-capable agents")
    if len(instruction.strip()) < 10:
        raise ObjectiveError("instruction too short")
    count = s.execute(select(func.count(ObjectiveTask.id)).where(ObjectiveTask.objective_id == obj.id)).scalar_one()
    if count >= 12:
        raise ObjectiveError("an objective may have at most 12 tasks")
    t = ObjectiveTask(objective_id=obj.id, department_code=department_code, title=title.strip()[:200],
                      instruction=instruction.strip(), agent_id=agent_id)
    s.add(t)
    s.flush()
    audit(s, actor, "objective.task_added", t.id, objective_id=obj.id, department=department_code)
    return t


def record_verdict(s: Session, *, objective_task_id: str, verdict: str, notes: str, actor: str) -> ObjectiveTask:
    t = s.get(ObjectiveTask, objective_task_id)
    if t is None:
        raise ObjectiveError("objective task not found")
    if t.status != "awaiting_qa":
        raise ObjectiveError(f"task is {t.status}; only work awaiting QA can be reviewed")
    if verdict not in ("pass", "fail"):
        raise ObjectiveError("verdict must be 'pass' or 'fail'")
    t.qa_verdict, t.qa_notes = verdict, notes.strip()[:2000]
    audit(s, actor, "objective.qa_verdict", t.id, verdict=verdict)
    return t


def summary(s: Session, objective_id: str) -> dict:
    obj = s.get(Objective, objective_id)
    if obj is None:
        raise ObjectiveError("objective not found")
    tasks = s.scalars(select(ObjectiveTask).where(ObjectiveTask.objective_id == obj.id)
                      .order_by(ObjectiveTask.created_at)).all()
    return {"id": obj.id, "title": obj.title, "status": obj.status, "report_document_id": obj.report_document_id,
            "failure_reason": obj.failure_reason,
            "tasks": [{"id": t.id, "department": t.department_code, "title": t.title, "status": t.status,
                       "agent_id": t.agent_id, "attempts": t.attempts, "reassigned": t.reassigned,
                       "qa_verdict": t.qa_verdict, "qa_notes": t.qa_notes, "history": t.history} for t in tasks]}


# ---------------------------------------------------------------- workflow steps

def _enqueue_run(s: Session, t: ObjectiveTask) -> None:
    from .workflows import enqueue

    enqueue(s, kind="objective.run", workflow_id=t.objective_id, payload={"objective_task_id": t.id},
            idempotency_key=f"obj:{t.id}:run:{t.agent_id}:{t.attempts + 1}")


def _log(t: ObjectiveTask, event: str, **detail) -> None:
    t.history = list(t.history or []) + [{"at": utcnow().isoformat(), "event": event, **detail}]


def plan(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    obj = s.get(Objective, task.payload["objective_id"])
    if obj.status != "planning":
        return {"skipped": obj.status}
    ceo = find_agent(s, role="ceo")
    if ceo is None:
        raise ObjectiveError("no active CEO agent")
    depts = ", ".join(f"{d.code} ({d.name})" for d in s.scalars(select(Department).order_by(Department.code)))
    prompt = (
        f"OBJECTIVE {obj.id}: {obj.title}\n\n{obj.description}\n\n"
        + (f"Related CRM deal: {obj.deal_id}\n" if obj.deal_id else "")
        + f"Departments: {depts}.\n\n"
        "Plan this objective. 1) Review the workforce with agents.list. 2) If a capability the plan needs is "
        "missing, create a specialist with agents.provision (you can only grant tools you hold; keep its "
        "budget small). 3) Record every department task with objectives.add_task (objective_id as above; "
        "set agent_id to assign a specific agent, e.g. a specialist you just created). Do not do the work "
        "yourself. Finish with a one-paragraph plan summary."
    )
    text = run_agent_prompt(s, tenant, ceo, prompt, task_id=task.id, trace=trace)
    tasks = s.scalars(select(ObjectiveTask).where(ObjectiveTask.objective_id == obj.id)).all()
    if not tasks:
        obj.status, obj.failure_reason = "failed", "the CEO produced no tasks"
        _enqueue_report(s, obj)
        return {"tasks": 0}
    for t in tasks:
        if t.agent_id is None:
            a = department_agent(s, t.department_code)
            if a is None or a.can_provision_agents:
                a = find_agent(s, role="operations")
            t.agent_id = a.id if a else None
        if t.agent_id is None:
            t.status = "failed"
            _log(t, "unassignable")
            continue
        _log(t, "assigned", agent_id=t.agent_id)
        _enqueue_run(s, t)
    obj.status = "in_progress"
    _check_complete(s, obj)
    return {"tasks": len(tasks), "plan": text[:2000]}


def run(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    t = s.get(ObjectiveTask, task.payload["objective_task_id"])
    obj = s.get(Objective, t.objective_id)
    if t.status in ("passed", "failed") or obj.status != "in_progress":
        return {"skipped": t.status}
    agent = s.get(Agent, t.agent_id) if t.agent_id else None
    t.attempts += 1
    t.status = "running"
    if agent is None or agent.status != "active":
        _handle_failure(s, t, f"assigned agent unavailable ({agent.status if agent else 'missing'})")
        return {"status": t.status}
    prompt = (f"Task from the CEO for objective '{obj.title}' (objective task {t.id}).\n\n{t.instruction}\n"
              + (f"\nRelated CRM deal: {obj.deal_id}\n" if obj.deal_id else "")
              + (f"\nQA feedback on the previous attempt: {t.qa_notes}\n" if t.qa_verdict == "fail" else "")
              + "\nDo the work with your tools, then summarise exactly what you did and the results.")
    try:
        with s.begin_nested():
            result = run_agent_prompt(s, tenant, agent, prompt, task_id=task.id, trace=trace)
    except Exception as e:  # noqa: BLE001 - the coordinator decides what happens next
        _handle_failure(s, t, f"{type(e).__name__}: {e}"[:500])
        return {"status": t.status}
    t.result_summary, t.status, t.qa_verdict = result, "awaiting_qa", None
    _log(t, "completed", agent_id=agent.id, attempt=t.attempts)
    from .workflows import enqueue

    enqueue(s, kind="objective.qa", workflow_id=obj.id, payload={"objective_task_id": t.id},
            idempotency_key=f"obj:{t.id}:qa:{agent.id}:{t.attempts}")
    return {"status": t.status}


def qa(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    t = s.get(ObjectiveTask, task.payload["objective_task_id"])
    if t.status != "awaiting_qa":
        return {"skipped": t.status}
    auditor = find_agent(s, role="qa")
    if not t.result_summary.strip():
        t.qa_verdict, t.qa_notes = "fail", "no result was reported"
    elif auditor is None:
        t.qa_verdict, t.qa_notes = "fail", "no active QA agent to review the work"
    else:
        prompt = (f"QA review of objective task {t.id} ({t.department_code}: {t.title}).\n\n"
                  f"Instruction given:\n{t.instruction}\n\nReported result:\n{t.result_summary}\n\n"
                  "Check the result against the instruction (use your read-only tools where useful). Then call "
                  f"objectives.qa_verdict with objective_task_id {t.id}, verdict 'pass' or 'fail', and specific notes.")
        try:
            with s.begin_nested():
                run_agent_prompt(s, tenant, auditor, prompt, task_id=task.id, trace=trace)
        except Exception as e:  # noqa: BLE001
            t.qa_verdict, t.qa_notes = "fail", f"QA review could not be completed: {type(e).__name__}"
        if t.qa_verdict is None:
            t.qa_verdict, t.qa_notes = "fail", "QA agent did not record a verdict"
    _log(t, "qa", verdict=t.qa_verdict, notes=t.qa_notes[:300])
    if t.qa_verdict == "pass":
        t.status = "passed"
        _check_complete(s, s.get(Objective, t.objective_id))
    else:
        _handle_failure(s, t, f"QA: {t.qa_notes}")
    return {"verdict": t.qa_verdict}


def _handle_failure(s: Session, t: ObjectiveTask, reason: str) -> None:
    _log(t, "failed_attempt", reason=reason, agent_id=t.agent_id, attempt=t.attempts)
    if t.attempts < MAX_ATTEMPTS:
        t.status = "pending"
        _enqueue_run(s, t)
        return
    if not t.reassigned:
        alt = department_agent(s, t.department_code, exclude={t.agent_id} if t.agent_id else None)
        if alt is None or alt.can_provision_agents:
            ops = find_agent(s, role="operations")
            alt = ops if ops is not None and ops.id != t.agent_id else None
        if alt is not None:
            _log(t, "reassigned", from_agent=t.agent_id, to_agent=alt.id)
            t.agent_id, t.reassigned, t.attempts, t.status = alt.id, True, 0, "pending"
            _enqueue_run(s, t)
            return
    t.status = "failed"
    _log(t, "gave_up")
    _check_complete(s, s.get(Objective, t.objective_id))


def _check_complete(s: Session, obj: Objective) -> None:
    if obj.status != "in_progress":
        return
    statuses = [st for (st,) in s.execute(select(ObjectiveTask.status).where(ObjectiveTask.objective_id == obj.id))]
    if all(st == "passed" for st in statuses):
        obj.status = "reporting"
        _enqueue_report(s, obj)
    elif all(st in ("passed", "failed") for st in statuses):
        obj.status = "failed"
        obj.failure_reason = f"{statuses.count('failed')} task(s) could not be completed"
        _enqueue_report(s, obj)


def _enqueue_report(s: Session, obj: Objective) -> None:
    from .workflows import enqueue

    enqueue(s, kind="objective.report", workflow_id=obj.id, payload={"objective_id": obj.id},
            idempotency_key=f"obj:{obj.id}:report")


def report(s: Session, tenant: Tenant, task: Task, trace: RunTrace) -> dict:
    obj = s.get(Objective, task.payload["objective_id"])
    if obj.report_document_id:
        return {"document_id": obj.report_document_id}
    tasks = s.scalars(select(ObjectiveTask).where(ObjectiveTask.objective_id == obj.id)
                      .order_by(ObjectiveTask.created_at)).all()
    names = {a.id: a.name for a in s.scalars(select(Agent))}
    rows = [{"department": t.department_code, "title": t.title, "agent": names.get(t.agent_id, "unassigned"),
             "attempts": t.attempts, "reassigned": t.reassigned,
             "qa": t.qa_verdict or t.status} for t in tasks]
    new_agents = [f"{a.name} (v{a.version}, {a.status})" for a in s.scalars(
        select(Agent).where(Agent.created_by == "CEO", Agent.created_at >= obj.created_at))]
    queue_ids = select(Task.id).where(Task.workflow_id == obj.id)
    spend = Decimal(str(s.execute(select(func.coalesce(func.sum(CostEntry.cost_usd), 0))
                                  .where(CostEntry.task_id.in_(queue_ids))).scalar_one()))
    final = "completed" if obj.status == "reporting" else "failed"
    ceo = find_agent(s, role="ceo")
    narrative = ""
    if ceo is not None:
        facts = "\n".join(f"- {r['department']}: {r['title']} by {r['agent']} -> {r['qa']}" for r in rows)
        try:
            with s.begin_nested():
                narrative = run_agent_prompt(
                    s, tenant, ceo,
                    f"Write a short executive summary for the owner on objective '{obj.title}' (outcome: {final}). "
                    f"Facts:\n{facts}\nState results, risks and recommended next steps. Do not invent facts.",
                    allow_tools=False, task_id=task.id, trace=trace, max_tokens=1500)
        except Exception as e:  # noqa: BLE001 - the factual report is produced regardless
            narrative = f"(CEO narrative unavailable: {type(e).__name__})"
    summary_text = narrative or f"Objective {final}. {obj.failure_reason}".strip()
    doc = documents.generate(s, kind="executive_report", title=f"Executive report - {obj.title}", deal_id=obj.deal_id,
                             data={"objective_title": obj.title, "status": final, "summary": summary_text,
                                   "tasks": rows, "new_agents": new_agents, "spend": f"{spend:.4f}"},
                             actor="CEO")
    obj.report_document_id, obj.status = doc.id, final
    if obj.deal_id:
        crm.log_activity(s, kind="document", deal_id=obj.deal_id,
                         summary=f"Objective '{obj.title}' {final}; executive report {doc.id}", actor="CEO")
    audit(s, "CEO", "objective.reported", obj.id, status=final, document_id=doc.id)
    return {"document_id": doc.id, "status": final}
