"""Tenant bootstrap: departments, the founding agent workforce and API keys."""
from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal

from sqlalchemy import select

from .agents import lifecycle
from .config import get_settings
from .db import system_session, tenant_session
from .models import ApiKey, Department, Tenant
from .policy import audit

DEPARTMENTS = [
    ("executive", "Executive Office", "Sets priorities, allocates resources and coordinates all departments."),
    ("technology", "Technology", "Builds, tests, improves and maintains the agent workforce and systems."),
    ("sales", "Sales & Business Development", "Converts leads into qualified opportunities and signed work."),
    ("delivery", "Consulting Delivery", "Delivers client engagements and reports progress."),
    ("finance", "Finance", "Invoicing, cost control and financial reporting."),
    ("marketing", "Marketing", "Generates and nurtures demand."),
    ("legal", "Legal & Compliance", "Prepares agreements and filings for owner signature."),
    ("operations", "Operations & People", "Keeps workflows running, monitors service health and coordinates "
     "people operations (contractor and staff onboarding, scheduling)."),
    ("qa", "Quality Assurance", "Independently verifies the work of every other department."),
]

FOUNDING_REFERENCE_BUDGET = Decimal("500")

CRM_ALL = ["crm.upsert_contact", "crm.find_contact", "crm.create_deal", "crm.move_deal", "crm.log_activity",
           "crm.pipeline_summary", "deals.get"]
MGMT = ["agents.list", "agents.provision", "agents.suspend", "agents.revise", "agents.replace", "tasks.delegate"]

FOUNDING_AGENTS = [
    dict(name="CEO", role="ceo", department_code="executive", can_provision=True, monthly_budget_usd="60",
         tools=MGMT + ["budget.status", "budget.request_increase", "crm.pipeline_summary", "deals.get",
                       "objectives.add_task", "objectives.status", "partners.list", "partners.register",
                       "partners.prepare_brief", "partners.send_brief"],
         instructions=(
             "You run White Waters Consulting Group day to day on behalf of the owner. Review the pipeline, "
             "budget and agent workforce, decide priorities, delegate work to department agents with "
             "tasks.delegate, and create specialist agents with agents.provision when a recurring need is not "
             "covered. Keep total spend inside the approved budget; request increases only with a business case.")),
    dict(name="Technology Director", role="technology", department_code="technology", can_provision=True,
         monthly_budget_usd="60", tools=MGMT + ["budget.status"],
         instructions=(
             "You own the health of the agent workforce. When an agent is suspended or failing, diagnose the "
             "cause, then replace it or publish an improved version (agents.revise) that must pass automated "
             "acceptance tests. Choose the least expensive approved model that does the job reliably.")),
    dict(name="Sales Lead", role="sales", department_code="sales", monthly_budget_usd="60",
         tools=CRM_ALL + ["docs.generate", "comms.draft_email", "comms.send_email", "partners.list"],
         instructions=(
             "You qualify inbound leads, maintain accurate CRM records, prepare proposals and correspond with "
             "prospects professionally. Never promise prices, dates or terms that are not in the CRM or an "
             "approved document.")),
    dict(name="Engagement Manager", role="delivery", department_code="delivery", monthly_budget_usd="50",
         tools=["docs.generate", "crm.log_activity", "crm.find_contact", "deals.get"],
         instructions=(
             "You run client engagements once a deal is won: plan milestones, prepare statements of work and "
             "status reports, and log progress against the deal in the CRM.")),
    dict(name="Finance Controller", role="finance", department_code="finance", monthly_budget_usd="30",
         tools=["finance.create_invoice", "finance.transfer_funds", "budget.status", "deals.get",
                "crm.pipeline_summary"],
         instructions=(
             "You issue invoices for won work, track AI operating spend against budget and report on revenue "
             "pipeline. Payments out always go to the owner for approval.")),
    dict(name="Marketing Manager", role="marketing", department_code="marketing", monthly_budget_usd="40",
         tools=["crm.upsert_contact", "crm.find_contact", "crm.log_activity", "comms.draft_email"],
         instructions=(
             "You generate and nurture demand: capture new contacts accurately, draft campaign emails for "
             "review and record marketing touchpoints in the CRM.")),
    dict(name="Legal & Compliance Officer", role="legal", department_code="legal", monthly_budget_usd="20",
         tools=["docs.generate", "legal.sign_agreement", "compliance.submit_filing"],
         instructions=(
             "You prepare statements of work and agreements and route anything binding or regulatory to the "
             "owner for signature or submission. You never sign or file on your own authority.")),
    dict(name="Operations Manager", role="operations", department_code="operations", monthly_budget_usd="30",
         tools=["crm.pipeline_summary", "budget.status", "agents.list", "tasks.delegate", "objectives.status"],
         instructions=(
             "You monitor workflow throughput, stuck tasks and service health, and delegate follow-ups to the "
             "responsible department agent.")),
    dict(name="QA Auditor", role="qa", department_code="qa", monthly_budget_usd="30",
         tools=["agents.list", "budget.status", "crm.pipeline_summary", "deals.get", "objectives.qa_verdict",
                "objectives.status"],
         instructions=(
             "You independently verify other departments' work: CRM accuracy, document correctness, budget "
             "compliance and agent behaviour. Report defects with evidence; you do not fix them yourself.")),
]


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_api_key(tenant_id: str, name: str, role: str = "owner") -> str:
    raw = "wwos_" + secrets.token_urlsafe(32)
    with tenant_session(tenant_id) as s:
        s.add(ApiKey(name=name, key_hash=hash_key(raw), role=role))
        audit(s, "owner", "api_key.created", name, role=role)
    return raw


def create_tenant(name: str, monthly_budget_usd: Decimal | str | None = None,
                  with_agents: bool = True) -> tuple[str, str]:
    """Create a tenant with departments and founding agents. Returns (tenant_id, owner_api_key)."""
    budget = Decimal(str(monthly_budget_usd)) if monthly_budget_usd is not None else get_settings().org_monthly_budget_usd
    with system_session() as s:
        if s.scalars(select(Tenant).where(Tenant.name == name)).first():
            raise ValueError(f"tenant {name!r} already exists")
        tenant = Tenant(name=name, monthly_budget_usd=budget)
        s.add(tenant)
        s.flush()
        tenant_id = tenant.id
    with tenant_session(tenant_id) as s:
        for code, dname, mission in DEPARTMENTS:
            s.add(Department(code=code, name=dname, mission=mission))
        s.flush()
        if with_agents:
            tenant = s.get(Tenant, tenant_id)
            for spec in FOUNDING_AGENTS:
                # Reference budgets assume a $500 organisation; scale so founding agents always take
                # 76% of the budget and leave 24% headroom for dynamically created agents.
                share = Decimal(spec["monthly_budget_usd"]) / FOUNDING_REFERENCE_BUDGET
                scaled = (share * budget).quantize(Decimal("0.01"))
                agent = lifecycle.provision(s, tenant, **{**spec, "monthly_budget_usd": min(
                    scaled, get_settings().max_agent_budget_usd)})
                if agent.status != "active":
                    raise RuntimeError(f"founding agent {agent.name} failed acceptance: {agent.status_reason}")
        audit(s, "owner", "tenant.created", tenant_id, name=name, budget=str(budget))
    key = create_api_key(tenant_id, "owner-bootstrap")
    return tenant_id, key
