"""HTTP API and management dashboard.

Authentication: per-tenant API keys (stored hashed), sent as
``Authorization: Bearer <key>`` or, for the dashboard, an HttpOnly
SameSite=Strict cookie set by the login form. Roles: viewer < operator < owner.
"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import approvals, budget, crm, db, documents, workflows
from .agents import lifecycle
from .bootstrap import hash_key
from .models import (Activity, Agent, ApiKey, ApprovalRequest, AuditLog, Contact, Deal, Department, Document,
                     OutboundMessage, Task, Tenant, WebSession)
from .policy import PermissionDenied

ROLE_RANK = {"viewer": 0, "operator": 1, "owner": 2}
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "web"))
templates.env.filters["money"] = lambda v: f"${Decimal(str(v or 0)):,.2f}"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="White Waters AI Business Operating System", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------- errors

@app.exception_handler(crm.CRMValidationError)
@app.exception_handler(documents.DocumentError)
@app.exception_handler(lifecycle.ProvisioningError)
@app.exception_handler(approvals.ApprovalError)
async def _bad_request(_req: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(PermissionDenied)
async def _forbidden(_req: Request, exc: Exception):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


# ---------------------------------------------------------------- auth

class Principal(BaseModel):
    tenant_id: str
    role: str
    key_name: str


def _lookup_key(raw: str | None) -> Principal | None:
    if not raw:
        return None
    with db.system_session() as s:
        key = s.scalars(select(ApiKey).where(ApiKey.key_hash == hash_key(raw), ApiKey.active.is_(True))).first()
        if key is None:
            return None
        return Principal(tenant_id=key.tenant_id, role=key.role, key_name=key.name)


def principal(request: Request) -> Principal:
    raw = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
    p = _lookup_key(raw)
    if p is None:
        p = _web_principal(request)
        # Cookie-authenticated writes must come from our own pages (defence in depth beyond SameSite).
        if p is not None and request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
            raise HTTPException(status_code=403, detail="cross-site request refused")
    if p is None:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return p


def require(role: str):
    def dep(p: Principal = Depends(principal)) -> Principal:
        if ROLE_RANK.get(p.role, -1) < ROLE_RANK[role]:  # unknown roles fail closed
            raise HTTPException(status_code=403, detail=f"{role} role required")
        return p
    return dep


def session_for(p: Principal) -> Iterator[Session]:
    with db.tenant_session(p.tenant_id) as s:
        yield s


def viewer_session(p: Principal = Depends(require("viewer"))) -> Iterator[Session]:
    yield from session_for(p)


def operator_session(p: Principal = Depends(require("operator"))) -> Iterator[Session]:
    yield from session_for(p)


def owner_session(p: Principal = Depends(require("owner"))) -> Iterator[Session]:
    yield from session_for(p)


def _404(obj: Any, what: str) -> Any:
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return obj


def _row(obj: Any, *fields: str) -> dict:
    out = {}
    for f in fields:
        v = getattr(obj, f)
        out[f] = str(v) if isinstance(v, Decimal) else (v.isoformat() if hasattr(v, "isoformat") else v)
    return out


# ---------------------------------------------------------------- health

@app.get("/health")
def health() -> dict:
    """Public liveness probe: reveals nothing about tenants or workload."""
    with db.system_session() as s:
        s.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/health/details")
def health_details(request: Request) -> dict:
    """Platform operator view (cross-tenant counts). Requires the X-Admin-Token header."""
    from .config import get_settings
    from .schedule import last_qa

    token = get_settings().admin_token
    supplied = request.headers.get("x-admin-token", "")
    if not token or not secrets.compare_digest(token, supplied):
        raise HTTPException(status_code=404, detail="not found")
    with db.system_session() as s:
        queued = s.execute(select(func.count(Task.id)).where(Task.status == "queued")).scalar_one()
        dead = s.execute(select(func.count(Task.id)).where(Task.status == "dead")).scalar_one()
        unknown_mail = s.execute(select(func.count(OutboundMessage.id))
                                 .where(OutboundMessage.status == "unknown")).scalar_one()
    return {"status": "ok", "queued_tasks": queued, "dead_tasks": dead, "emails_unknown_outcome": unknown_mail,
            "last_qa_scan": last_qa()}


# ---------------------------------------------------------------- CRM

class ContactIn(BaseModel):
    email: str
    first_name: str
    last_name: str = ""
    phone: str = ""
    company: str | None = None
    source: str = "api"
    notes: str = ""


@app.get("/api/contacts")
def list_contacts(s: Session = Depends(viewer_session)) -> list[dict]:
    return [_row(c, "id", "first_name", "last_name", "email", "company_id", "lifecycle", "source", "created_at")
            for c in s.scalars(select(Contact).order_by(Contact.created_at.desc()))]


@app.post("/api/contacts", status_code=201)
def create_contact(body: ContactIn, s: Session = Depends(operator_session)) -> dict:
    c, created = crm.upsert_contact(s, actor="api", **body.model_dump())
    return {"id": c.id, "created": created}


class DealIn(BaseModel):
    contact_id: str
    title: str
    value: str = "0"
    currency: str = "USD"


@app.get("/api/deals")
def list_deals(s: Session = Depends(viewer_session)) -> list[dict]:
    return [_row(d, "id", "title", "contact_id", "stage", "value", "currency", "updated_at")
            for d in s.scalars(select(Deal).order_by(Deal.updated_at.desc()))]


@app.post("/api/deals", status_code=201)
def create_deal(body: DealIn, s: Session = Depends(operator_session)) -> dict:
    d = crm.create_deal(s, actor="api", **body.model_dump())
    return {"id": d.id, "stage": d.stage}


class StageIn(BaseModel):
    stage: str


@app.post("/api/deals/{deal_id}/stage")
def move_deal(deal_id: str, body: StageIn, s: Session = Depends(operator_session)) -> dict:
    _404(s.get(Deal, deal_id), "deal")
    d = crm.move_deal(s, deal_id, body.stage, actor="api")
    return {"id": d.id, "stage": d.stage}


@app.get("/api/deals/{deal_id}/activities")
def deal_activities(deal_id: str, s: Session = Depends(viewer_session)) -> list[dict]:
    _404(s.get(Deal, deal_id), "deal")
    return [_row(a, "id", "kind", "summary", "actor", "created_at")
            for a in s.scalars(select(Activity).where(Activity.deal_id == deal_id).order_by(Activity.created_at))]


class LeadIn(BaseModel):
    email: str
    first_name: str
    last_name: str = ""
    company: str | None = None
    need: str = ""
    estimated_value: str = "0"
    currency: str = "USD"
    source: str = "web"
    idempotency_key: str | None = None


@app.post("/api/leads", status_code=202)
def submit_lead(body: LeadIn, s: Session = Depends(operator_session)) -> dict:
    data = body.model_dump(exclude={"idempotency_key"})
    task = workflows.start_lead_workflow(s, data, idempotency_key=body.idempotency_key)
    return {"task_id": task.id, "workflow_id": task.workflow_id, "status": task.status}


# ---------------------------------------------------------------- documents

class DocumentIn(BaseModel):
    kind: str
    title: str
    data: dict[str, Any]
    deal_id: str | None = None


@app.post("/api/documents", status_code=201)
def create_document(body: DocumentIn, s: Session = Depends(operator_session)) -> dict:
    doc = documents.generate(s, actor="api", **body.model_dump())
    return {"id": doc.id, "kind": doc.kind}


@app.get("/api/documents")
def list_documents(s: Session = Depends(viewer_session)) -> list[dict]:
    return [_row(d, "id", "kind", "title", "deal_id", "created_by", "created_at")
            for d in s.scalars(select(Document).order_by(Document.created_at.desc()))]


@app.get("/api/documents/{doc_id}", response_class=HTMLResponse)
def get_document(doc_id: str, format: str = "html", s: Session = Depends(viewer_session)):
    doc = _404(s.get(Document, doc_id), "document")
    if format == "md":
        return HTMLResponse(doc.markdown, media_type="text/markdown")
    return HTMLResponse(doc.html, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@app.get("/api/messages")
def list_messages(s: Session = Depends(viewer_session)) -> list[dict]:
    return [_row(m, "id", "to_address", "subject", "status", "attempts", "sent_at", "created_by", "created_at")
            for m in s.scalars(select(OutboundMessage).order_by(OutboundMessage.created_at.desc()))]


class ResolveIn(BaseModel):
    delivered: bool


@app.post("/api/messages/{message_id}/resolve")
def resolve_message(message_id: str, body: ResolveIn, p: Principal = Depends(require("owner")),
                    s: Session = Depends(owner_session)) -> dict:
    """Reconcile an email whose delivery outcome is unknown (or that failed): confirm it was
    delivered, or re-queue it once (same Message-ID). Owner only: a resend speaks for the firm."""
    from . import comms

    _404(s.get(OutboundMessage, message_id), "message")
    try:
        m = comms.resolve(s, message_id, delivered=body.delivered, actor=f"owner:{p.key_name}")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"id": m.id, "status": m.status}


# ---------------------------------------------------------------- agents

AGENT_FIELDS = ("id", "lineage_id", "version", "name", "role", "department_id", "model", "tools", "status",
                "status_reason", "monthly_budget_usd", "can_provision_agents", "consecutive_failures",
                "created_by", "created_at")


@app.get("/api/agents")
def list_agents(include_retired: bool = False, s: Session = Depends(viewer_session)) -> list[dict]:
    q = select(Agent).order_by(Agent.name, Agent.version)
    if not include_retired:
        q = q.where(Agent.status != "retired")
    out = []
    for a in s.scalars(q):
        row = _row(a, *AGENT_FIELDS)
        row["spent_this_month"] = f"{budget.spent_this_month(s, a.id):.4f}"
        out.append(row)
    return out


class AgentIn(BaseModel):
    name: str
    role: str
    department_code: str
    instructions: str
    tools: list[str]
    model: str | None = None
    monthly_budget_usd: str = "10"
    can_provision: bool = False


@app.post("/api/agents", status_code=201)
def provision_agent(body: AgentIn, s: Session = Depends(owner_session)) -> dict:
    tenant = lifecycle.tenant_of(s)
    a = lifecycle.provision(s, tenant, creator=None, **body.model_dump())
    return _row(a, *AGENT_FIELDS)


class ReasonIn(BaseModel):
    reason: str = "suspended by owner"


@app.post("/api/agents/{agent_id}/suspend")
def suspend_agent(agent_id: str, body: ReasonIn, s: Session = Depends(owner_session)) -> dict:
    a = _404(s.get(Agent, agent_id), "agent")
    lifecycle.suspend(s, a, body.reason, lifecycle.OWNER)
    return {"id": a.id, "status": a.status}


@app.post("/api/agents/{agent_id}/reactivate")
def reactivate_agent(agent_id: str, s: Session = Depends(owner_session)):
    a = _404(s.get(Agent, agent_id), "agent")
    ok, detail = lifecycle.reactivate(s, lifecycle.tenant_of(s), a, lifecycle.OWNER)
    body = {"id": a.id, "status": a.status, "detail": detail}
    # A failed re-test still commits (agent stays suspended; the test's spend stays on the ledger).
    return body if ok else JSONResponse(status_code=409, content=body)


class ReviseIn(BaseModel):
    instructions: str | None = None
    tools: list[str] | None = None
    model: str | None = None
    monthly_budget_usd: str | None = None


@app.post("/api/agents/{agent_id}/revise")
def revise_agent(agent_id: str, body: ReviseIn, s: Session = Depends(owner_session)) -> dict:
    a = _404(s.get(Agent, agent_id), "agent")
    new = lifecycle.revise(s, lifecycle.tenant_of(s), a, actor_agent=None, **body.model_dump())
    return _row(new, *AGENT_FIELDS)


# ---------------------------------------------------------------- tasks

class TaskIn(BaseModel):
    agent_id: str
    instruction: str = Field(min_length=3)
    idempotency_key: str | None = None


@app.post("/api/tasks", status_code=202)
def assign_task(body: TaskIn, p: Principal = Depends(require("operator")),
                s: Session = Depends(operator_session)) -> dict:
    a = _404(s.get(Agent, body.agent_id), "agent")
    if a.status != "active":
        raise HTTPException(status_code=409, detail=f"agent is {a.status}")
    if a.can_provision_agents and p.role != "owner":
        # Provisioning-capable agents act with owner-level reach; only the owner may task them.
        raise HTTPException(status_code=403, detail="owner role required to assign work to this agent")
    key = f"api:{body.idempotency_key}" if body.idempotency_key else f"api:{secrets.token_hex(8)}"
    task, created = workflows.enqueue(s, kind="agent_run", idempotency_key=key, agent_id=a.id,
                                      payload={"instruction": body.instruction, "delegated_by": "owner"})
    return {"task_id": task.id, "created": created, "status": task.status}


@app.get("/api/tasks")
def list_tasks(status: str | None = None, limit: int = 100, s: Session = Depends(viewer_session)) -> list[dict]:
    q = select(Task).order_by(Task.created_at.desc()).limit(max(1, min(limit, 500)))
    if status:
        q = q.where(Task.status == status)
    return [_row(t, "id", "kind", "status", "attempts", "agent_id", "workflow_id", "last_error", "result",
                 "created_at", "updated_at") for t in s.scalars(q)]


@app.post("/api/worker/drain")
def drain(p: Principal = Depends(require("owner"))) -> dict:
    """Process this tenant's due tasks now. Production runs a dedicated worker; this exists for
    small single-process deployments."""
    return {"processed": workflows.drain(max_tasks=200, tenant_id=p.tenant_id)}


# ---------------------------------------------------------------- approvals, budget, audit

@app.get("/api/approvals")
def list_approvals(status: str = "pending", s: Session = Depends(viewer_session)) -> list[dict]:
    return [_row(r, "id", "category", "action", "params", "requested_by", "reason", "status", "decided_by",
                 "decided_at", "created_at")
            for r in s.scalars(select(ApprovalRequest).where(ApprovalRequest.status == status)
                               .order_by(ApprovalRequest.created_at))]


class DecisionIn(BaseModel):
    approve: bool
    note: str = ""


@app.post("/api/approvals/{approval_id}/decision")
def decide(approval_id: str, body: DecisionIn, p: Principal = Depends(require("owner")),
           s: Session = Depends(owner_session)) -> dict:
    r = approvals.decide(s, approval_id, approve=body.approve, actor=f"owner:{p.key_name}", note=body.note)
    return {"id": r.id, "status": r.status}


@app.get("/api/budget")
def budget_status(s: Session = Depends(viewer_session)) -> dict:
    tenant = lifecycle.tenant_of(s)
    return {"monthly_budget_usd": str(tenant.monthly_budget_usd),
            "spent_this_month_usd": f"{budget.spent_this_month(s):.4f}",
            "allocated_to_agents_usd": str(budget.allocated_to_agents(s))}


@app.get("/api/audit")
def audit_log(limit: int = 200, s: Session = Depends(owner_session)) -> list[dict]:
    return [_row(a, "id", "actor", "action", "target", "detail", "created_at")
            for a in s.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(max(1, min(limit, 1000))))]


# ---------------------------------------------------------------- dashboard

def _approval_preview(s: Session, r: ApprovalRequest) -> dict | None:
    """What the owner needs to see to decide, e.g. the actual email for a send request."""
    if r.action == "comms.send_email":
        m = s.get(OutboundMessage, (r.params or {}).get("message_id"))
        if m is not None:
            return {"to": m.to_address, "subject": m.subject, "body": m.body}
    return None


def dashboard_data(s: Session) -> dict:
    tenant = lifecycle.tenant_of(s)
    depts = {d.id: d for d in s.scalars(select(Department))}
    agents = []
    for a in s.scalars(select(Agent).where(Agent.status != "retired").order_by(Agent.name)):
        agents.append({"a": a, "dept": depts.get(a.department_id).name if a.department_id in depts else "",
                       "spent": budget.spent_this_month(s, a.id)})
    task_counts = dict(s.execute(select(Task.status, func.count(Task.id)).group_by(Task.status)).all())
    spent = budget.spent_this_month(s)
    return {
        "tenant": tenant,
        "spent": spent,
        "budget_pct": float(spent / tenant.monthly_budget_usd * 100) if tenant.monthly_budget_usd else 0.0,
        "allocated": budget.allocated_to_agents(s),
        "agents": agents,
        "pipeline": crm.pipeline_summary(s),
        "approvals": [{"r": r, "preview": _approval_preview(s, r)} for r in approvals.pending(s)],
        "tasks": list(s.scalars(select(Task).order_by(Task.updated_at.desc()).limit(15))),
        "task_counts": task_counts,
        "contacts": s.execute(select(func.count(Contact.id))).scalar_one(),
        "documents": list(s.scalars(select(Document).order_by(Document.created_at.desc()).limit(8))),
        "attention": list(s.scalars(select(Agent).where(Agent.status == "suspended").order_by(Agent.name))),
        "unknown_mail": list(s.scalars(select(OutboundMessage).where(OutboundMessage.status.in_(["unknown", "failed"])))),
    }


SESSION_HOURS = 8


def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts (login CSRF etc.). Browsers send Origin on POST.

    Behind a TLS-terminating proxy the app sees http while the browser says https, so compare to
    WWOS_PUBLIC_ORIGIN when configured, otherwise compare hosts only."""
    from urllib.parse import urlsplit

    from .config import get_settings

    origin = request.headers.get("origin")
    if origin is None:
        return True  # non-browser clients
    origin = origin.rstrip("/")
    public = get_settings().public_origin
    if public:
        return origin == public
    return urlsplit(origin).netloc == request.headers.get("host", request.url.netloc)


_login_attempts: dict[str, list[float]] = {}
LOGIN_LIMIT_PER_MINUTE = 10


def _login_rate_limited(client: str) -> bool:
    import time

    now = time.monotonic()
    recent = [t for t in _login_attempts.get(client, []) if now - t < 60]
    recent.append(now)
    _login_attempts[client] = recent
    return len(recent) > LOGIN_LIMIT_PER_MINUTE


def _web_principal(request: Request) -> Principal | None:
    from datetime import datetime, timezone

    raw = request.cookies.get("wwos_session")
    if not raw:
        return None
    with db.system_session() as s:
        ws = s.scalars(select(WebSession).where(WebSession.token_hash == hash_key(raw))).first()
        if ws is None:
            return None
        expires = ws.expires_at if ws.expires_at.tzinfo else ws.expires_at.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return None
        key = s.get(ApiKey, ws.api_key_id)
        if key is None or not key.active:
            return None
        return Principal(tenant_id=key.tenant_id, role=key.role, key_name=key.name)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/dashboard")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login(request: Request, api_key: str = Form(...)):
    from datetime import timedelta

    from .config import get_settings
    from .models import utcnow

    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-site request refused")
    if _login_rate_limited(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="too many login attempts; wait a minute")
    from sqlalchemy import delete

    with db.system_session() as s:
        s.execute(delete(WebSession).where(WebSession.expires_at < utcnow()))  # prune
        key = s.scalars(select(ApiKey).where(ApiKey.key_hash == hash_key(api_key), ApiKey.active.is_(True))).first()
        if key is None:
            return RedirectResponse("/login?error=Invalid+key", status_code=303)
        token = secrets.token_urlsafe(32)
        s.add(WebSession(tenant_id=key.tenant_id, token_hash=hash_key(token), api_key_id=key.id,
                         expires_at=utcnow() + timedelta(hours=SESSION_HOURS)))
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie("wwos_session", token, httponly=True, samesite="strict", secure=get_settings().cookie_secure,
                    max_age=SESSION_HOURS * 3600)
    return resp


@app.post("/logout")
def logout(request: Request):
    from sqlalchemy import delete

    raw = request.cookies.get("wwos_session")
    if raw:  # always revoke server-side, whoever triggered the logout
        with db.system_session() as s:
            s.execute(delete(WebSession).where(WebSession.token_hash == hash_key(raw)))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("wwos_session")
    return resp


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    p = _web_principal(request)
    if p is None:
        return RedirectResponse("/login")
    with db.tenant_session(p.tenant_id) as s:
        ctx = dashboard_data(s)
        ctx["role"] = p.role
        return templates.TemplateResponse(request, "dashboard.html", ctx)


def _dashboard_owner(request: Request) -> Principal:
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-site request refused")
    p = _web_principal(request)
    if p is None:
        raise HTTPException(status_code=401, detail="sign in required")
    if p.role != "owner":
        raise HTTPException(status_code=403, detail="owner role required")
    return p


@app.post("/dashboard/approvals/{approval_id}")
def dashboard_decide(approval_id: str, request: Request, decision: str = Form(...)):
    p = _dashboard_owner(request)
    with db.tenant_session(p.tenant_id) as s:
        approvals.decide(s, approval_id, approve=decision == "approve", actor=f"owner:{p.key_name}")
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/dashboard/agents/{agent_id}/reactivate")
def dashboard_reactivate(agent_id: str, request: Request):
    p = _dashboard_owner(request)
    with db.tenant_session(p.tenant_id) as s:
        a = _404(s.get(Agent, agent_id), "agent")
        lifecycle.reactivate(s, lifecycle.tenant_of(s), a, f"owner:{p.key_name}")
    return RedirectResponse("/dashboard", status_code=303)
