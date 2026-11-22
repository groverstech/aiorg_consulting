"""Tools available to agents.

Every tool is a plain function ``handler(ctx, **args) -> dict`` plus a JSON
schema. The runtime enforces permissions and owner-approval gates *before* a
handler runs (see ``policy.py``); handlers themselves only contain business
logic, so the same code path serves agents, workflows and approved actions.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import budget, comms, crm, documents
from .models import Agent, Contact, Deal, Tenant


@dataclass
class ToolContext:
    session: Session
    tenant: Tenant
    agent: Agent | None  # None when the owner executes an approved action
    task_id: str | None = None

    @property
    def actor(self) -> str:
        return self.agent.name if self.agent else "owner"


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[..., dict]


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None):
    def deco(fn: Callable[..., dict]) -> Callable[..., dict]:
        TOOLS[name] = Tool(name, description, {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }, fn)
        return fn
    return deco


def api_name(name: str) -> str:
    """Claude tool names may not contain dots."""
    return name.replace(".", "__")


def from_api_name(name: str) -> str:
    return name.replace("__", ".")


def anthropic_tool_defs(names: list[str]) -> list[dict]:
    return [{"name": api_name(n), "description": TOOLS[n].description, "input_schema": TOOLS[n].schema}
            for n in names if n in TOOLS]


S = {"type": "string"}
NUM = {"type": ["number", "string"]}
STRLIST = {"type": "array", "items": {"type": "string"}}

# ---------------------------------------------------------------- CRM


@tool("crm.upsert_contact", "Create a CRM contact or return the existing one with that email.",
      {"email": S, "first_name": S, "last_name": S, "phone": S, "company": S, "source": S, "notes": S},
      ["email", "first_name"])
def _upsert_contact(ctx: ToolContext, **a) -> dict:
    c, created = crm.upsert_contact(ctx.session, actor=ctx.actor, **a)
    return {"contact_id": c.id, "created": created, "lifecycle": c.lifecycle}


@tool("crm.find_contact", "Look up a contact by email.", {"email": S}, ["email"])
def _find_contact(ctx: ToolContext, email: str) -> dict:
    c = ctx.session.scalars(select(Contact).where(Contact.email == email.strip().lower())).first()
    if not c:
        return {"found": False}
    return {"found": True, "contact_id": c.id, "name": f"{c.first_name} {c.last_name}".strip(),
            "lifecycle": c.lifecycle}


@tool("crm.create_deal", "Open a deal for a contact.",
      {"contact_id": S, "title": S, "value": NUM, "currency": S}, ["contact_id", "title"])
def _create_deal(ctx: ToolContext, **a) -> dict:
    d = crm.create_deal(ctx.session, actor=ctx.actor,
                        owner_agent_id=ctx.agent.id if ctx.agent else None, **a)
    return {"deal_id": d.id, "stage": d.stage}


@tool("crm.move_deal", "Move a deal to a new pipeline stage (lead, qualified, proposal, negotiation, won, lost).",
      {"deal_id": S, "stage": S}, ["deal_id", "stage"])
def _move_deal(ctx: ToolContext, deal_id: str, stage: str) -> dict:
    d = crm.move_deal(ctx.session, deal_id, stage, actor=ctx.actor)
    return {"deal_id": d.id, "stage": d.stage}


@tool("crm.log_activity", "Record a note, call or meeting against a contact or deal.",
      {"kind": S, "summary": S, "contact_id": S, "deal_id": S}, ["kind", "summary"])
def _log_activity(ctx: ToolContext, **a) -> dict:
    act = crm.log_activity(ctx.session, actor=ctx.actor, **a)
    ctx.session.flush()
    return {"activity_id": act.id}


@tool("crm.pipeline_summary", "Count and value of deals per stage.", {})
def _pipeline(ctx: ToolContext) -> dict:
    return crm.pipeline_summary(ctx.session)

# ---------------------------------------------------------------- Documents & comms


@tool("docs.generate", "Generate a business document (proposal, sow, invoice, status_report).",
      {"kind": S, "title": S, "data": {"type": "object"}, "deal_id": S}, ["kind", "title", "data"])
def _docs_generate(ctx: ToolContext, kind: str, title: str, data: dict, deal_id: str | None = None) -> dict:
    doc = documents.generate(ctx.session, kind=kind, title=title, data=data, deal_id=deal_id, actor=ctx.actor)
    return {"document_id": doc.id, "kind": doc.kind}


@tool("comms.draft_email", "Save an email draft to the outbox (idempotent by key).",
      {"idempotency_key": S, "to": S, "subject": S, "body": S, "contact_id": S},
      ["idempotency_key", "to", "subject", "body"])
def _draft_email(ctx: ToolContext, **a) -> dict:
    msg, created = comms.draft_email(ctx.session, actor=ctx.actor, **a)
    return {"message_id": msg.id, "status": msg.status, "created": created}


@tool("comms.send_email", "Send a drafted outbound email. May require owner approval by policy.",
      {"message_id": S}, ["message_id"])
def _send_email(ctx: ToolContext, message_id: str) -> dict:
    msg = comms.deliver(ctx.session, message_id)
    return {"message_id": msg.id, "status": msg.status}

# ---------------------------------------------------------------- Finance / legal (owner-gated)


@tool("finance.create_invoice", "Generate an invoice document for a client.",
      {"client_name": S, "invoice_number": S, "line_items": {"type": "array", "items": {"type": "object"}},
       "currency": S, "due_days": {"type": "integer"}, "tax_rate": NUM, "deal_id": S},
      ["client_name", "invoice_number", "line_items", "currency", "due_days"])
def _create_invoice(ctx: ToolContext, deal_id: str | None = None, **data) -> dict:
    doc = documents.generate(ctx.session, kind="invoice", title=f"Invoice {data['invoice_number']}",
                             data=data, deal_id=deal_id, actor=ctx.actor)
    return {"document_id": doc.id, "total": doc.data.get("total")}


@tool("finance.transfer_funds", "Request a financial transfer. ALWAYS requires owner approval.",
      {"amount": NUM, "currency": S, "payee": S, "reference": S}, ["amount", "currency", "payee"])
def _transfer_funds(ctx: ToolContext, **a) -> dict:
    # No banking integration is connected; approval authorises the owner to execute it.
    return {"status": "authorised_for_manual_execution", "detail": a,
            "note": "No payment rail is integrated. The owner executes the transfer in their bank."}


@tool("legal.sign_agreement", "Request signature of a binding agreement. ALWAYS requires owner approval.",
      {"document_id": S, "counterparty": S}, ["document_id", "counterparty"])
def _sign_agreement(ctx: ToolContext, document_id: str, counterparty: str) -> dict:
    return {"status": "signature_authorised", "document_id": document_id, "counterparty": counterparty,
            "note": "Owner approved; signature is applied by the owner or their e-signature provider."}


@tool("compliance.submit_filing", "Request a regulatory filing. ALWAYS requires owner approval.",
      {"filing": S, "authority": S, "summary": S}, ["filing", "authority"])
def _submit_filing(ctx: ToolContext, **a) -> dict:
    return {"status": "authorised_for_submission", "detail": a}


@tool("budget.request_increase", "Request an increase to the organisation's monthly AI budget (owner approval).",
      {"new_monthly_budget_usd": NUM, "justification": S}, ["new_monthly_budget_usd", "justification"])
def _budget_increase(ctx: ToolContext, new_monthly_budget_usd, justification: str) -> dict:
    new = Decimal(str(new_monthly_budget_usd))
    old = ctx.tenant.monthly_budget_usd
    ctx.tenant.monthly_budget_usd = new
    return {"previous": str(old), "new": str(new)}


@tool("budget.status", "Current month spend vs budget for the organisation and this agent.", {})
def _budget_status(ctx: ToolContext) -> dict:
    out = {"org_budget": str(ctx.tenant.monthly_budget_usd),
           "org_spent": f"{budget.spent_this_month(ctx.session):.4f}",
           "allocated_to_agents": str(budget.allocated_to_agents(ctx.session))}
    if ctx.agent:
        out.update(agent_budget=str(ctx.agent.monthly_budget_usd),
                   agent_spent=f"{budget.spent_this_month(ctx.session, ctx.agent.id):.4f}")
    return out

# ---------------------------------------------------------------- Agent management


@tool("agents.list", "List agents with their status, department and budget.", {})
def _agents_list(ctx: ToolContext) -> dict:
    rows = ctx.session.scalars(select(Agent).where(Agent.status != "retired").order_by(Agent.name)).all()
    return {"agents": [{"id": a.id, "name": a.name, "role": a.role, "status": a.status, "version": a.version,
                        "budget": str(a.monthly_budget_usd), "tools": a.tools} for a in rows]}


@tool("agents.provision", "Create, test and activate a new specialist agent within budget and your own permissions.",
      {"name": S, "role": S, "department_code": S, "instructions": S, "tools": STRLIST, "model": S,
       "monthly_budget_usd": NUM}, ["name", "role", "department_code", "instructions", "tools"])
def _agents_provision(ctx: ToolContext, **a) -> dict:
    from .agents import lifecycle

    agent = lifecycle.provision(ctx.session, ctx.tenant, creator=ctx.agent, **a)
    return {"agent_id": agent.id, "status": agent.status, "reason": agent.status_reason}


@tool("agents.suspend", "Suspend a malfunctioning agent.", {"agent_id": S, "reason": S}, ["agent_id", "reason"])
def _agents_suspend(ctx: ToolContext, agent_id: str, reason: str) -> dict:
    from .agents import lifecycle

    target = ctx.session.get(Agent, agent_id)
    if target is None:
        raise ValueError("agent not found")
    lifecycle.suspend(ctx.session, target, reason, ctx.actor)
    return {"agent_id": agent_id, "status": target.status}


@tool("agents.revise", "Publish a new tested version of an agent with improved instructions, tools or model.",
      {"agent_id": S, "instructions": S, "tools": STRLIST, "model": S, "monthly_budget_usd": NUM}, ["agent_id"])
def _agents_revise(ctx: ToolContext, agent_id: str, **a) -> dict:
    from .agents import lifecycle

    target = ctx.session.get(Agent, agent_id)
    if target is None:
        raise ValueError("agent not found")
    new = lifecycle.revise(ctx.session, ctx.tenant, target, actor_agent=ctx.agent, **a)
    return {"agent_id": new.id, "version": new.version, "status": new.status, "reason": new.status_reason}


@tool("agents.replace", "Replace a suspended agent with a freshly tested instance.", {"agent_id": S}, ["agent_id"])
def _agents_replace(ctx: ToolContext, agent_id: str) -> dict:
    from .agents import lifecycle

    target = ctx.session.get(Agent, agent_id)
    if target is None:
        raise ValueError("agent not found")
    new = lifecycle.replace_failed(ctx.session, ctx.tenant, target, ctx.agent)
    return {"agent_id": new.id, "status": new.status}


@tool("tasks.delegate", "Assign work to another agent; it runs asynchronously.",
      {"agent_id": S, "instruction": S, "idempotency_key": S}, ["agent_id", "instruction"])
def _delegate(ctx: ToolContext, agent_id: str, instruction: str, idempotency_key: str | None = None) -> dict:
    from . import workflows

    target = ctx.session.get(Agent, agent_id)
    if target is None or target.status != "active":
        raise ValueError("target agent not found or not active")
    digest = hashlib.sha256(instruction.encode()).hexdigest()[:16]
    key = idempotency_key or f"delegate:{ctx.task_id}:{agent_id}:{digest}"
    task, created = workflows.enqueue(ctx.session, kind="agent_run", idempotency_key=key,
                                      payload={"instruction": instruction, "delegated_by": ctx.actor},
                                      agent_id=agent_id)
    return {"task_id": task.id, "created": created}


@tool("deals.get", "Fetch a deal by id.", {"deal_id": S}, ["deal_id"])
def _deal_get(ctx: ToolContext, deal_id: str) -> dict:
    d = ctx.session.get(Deal, deal_id)
    if d is None:
        return {"found": False}
    return {"found": True, "title": d.title, "stage": d.stage, "value": str(d.value), "currency": d.currency}
