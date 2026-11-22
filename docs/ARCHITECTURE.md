# Architecture

White Waters AI Business Operating System (`wwos`): a multi-tenant Python service in which
Claude-powered agents, organised into departments, run the day-to-day operations of a
consulting firm, inside hard limits set by the owner.

```
            ┌──────────── HTTP API + dashboard (FastAPI, wwos/api.py) ───────────┐
 owner ───► │ auth (hashed API keys, roles) · CRM · leads · docs · agents · approvals │
            └───────────────┬─────────────────────────────────────┬──────────────┘
                            │ enqueue (idempotent)                │ decide
                            ▼                                     ▼
   ┌──────── durable task queue (tasks table) ────────┐   approvals.py ──► approval.execute task
   │ claim w/ lease · retry+backoff · dead-letter     │
   └───────────────┬──────────────────────────────────┘
                   ▼ worker (wwos/workflows.py, `wwos worker`)
   workflow steps ─┬─ lead_intake → qualify → proposal → outreach
                   └─ agent_run ──► agent runtime (agents/runtime.py)
                                     │ budget pre-check → LLM provider (llm.py)
                                     │ tool call → permission gate → owner gate → handler
                                     ▼
                     tools.py ──► crm.py · documents.py · comms.py (outbox) · agents/lifecycle.py
                                     │
                     SQLAlchemy (db.py): tenant-scoped sessions ── SQLite (dev) / PostgreSQL (prod)
```

## Layers

| Module | Responsibility |
|---|---|
| `config.py` | All settings from `WWOS_*` env vars; owner-set ceilings live here. |
| `db.py` | Engine, sessions, **automatic tenant isolation** (`with_loader_criteria` on every ORM query + `before_flush` write guard). |
| `models.py` | Tenants, API keys, departments, versioned agents, cost ledger, tasks, approvals, audit log, CRM, documents, outbox. |
| `policy.py` | Tool permission checks; owner-authority categories (mandate §9); audit helper. |
| `budget.py` | Model price table, cost calculation, agent + org ceilings, allocation accounting. |
| `llm.py` | `AnthropicProvider` (Claude API, refusal fallbacks, transient/permanent error split) and deterministic `OfflineProvider`. |
| `agents/runtime.py` | Tool-use loop: budget gate → model → per-tool permission/approval gates → savepointed handler. |
| `agents/lifecycle.py` | Provision, acceptance-test, activate, revise (versioned), suspend, reactivate, replace. |
| `tools.py` | The agent tool catalogue (CRM, docs, comms, finance, legal, budget, agent management, delegation). |
| `workflows.py` | Queue, worker, retry/lease/dead-letter, self-healing, lead→proposal workflow. |
| `approvals.py` | Owner approval queue; approved actions execute as idempotent tasks. |
| `crm.py`, `documents.py`, `comms.py` | Business services with validation; Jinja2 documents; email outbox (`request_send` queues, `dispatch` delivers at-most-once). |
| `schedule.py` | Recurring jobs (daily CEO review, daily operations check, hourly QA scan), exactly once per period. |
| `qa.py` | Independent read-only invariant scanner (`wwos qa`). |
| `backup.py` | Verified online backup / restore (SQLite); Postgres via `pg_dump` (runbook). |
| `bootstrap.py` | Tenant creation: 9 departments, 9 founding agents, owner API key. |

## Departments and founding agents

| Department | Agent | Can provision agents | Notable tools |
|---|---|---|---|
| Executive | CEO | yes | agent management, delegation, budget increase *request* |
| Technology | Technology Director | yes | agent management (repairs/revises failed agents) |
| Sales | Sales Lead | no | full CRM, proposals, email draft/send |
| Delivery | Engagement Manager | no | SOW / status reports, CRM notes |
| Finance | Finance Controller | no | invoices, transfer *request*, budget status |
| Marketing | Marketing Manager | no | contacts, email drafts |
| Legal & Compliance | Legal & Compliance Officer | no | agreements / filings (owner-gated) |
| Operations | Operations Manager | no | pipeline, budget, delegation |
| QA | QA Auditor | no | read-only checks |

Founding agents take 76% of the tenant budget (scaled), leaving 24% for dynamically created agents.

## Safety model (enforced in code, not prompts)

1. **Tenant isolation**: every tenant-owned row carries `tenant_id`; tenant sessions filter
   every read/update/delete and refuse foreign writes.
2. **Tool permissions**: an agent can only call tools on its own record; suspended/retired agents cannot act.
3. **Privilege containment**: an agent can only grant tools it holds (or keep an agent's existing
   tools when revising); only the owner can create, revise or suspend provisioning-capable agents.
4. **Owner authority**: transfers, signatures, filings, budget increases, irreversible purges and
   (by policy) external email never execute directly; they become `ApprovalRequest`s.
5. **Budget**: worst-case pre-check per model call against agent and org ceilings; immutable
   ledger; spend from failed runs is re-recorded after rollback; total allocation ≤ org budget.
6. **Reliability**: idempotency keys on tasks and outbound messages (agent-supplied keys are
   namespaced); atomic workflow steps; leases with claim tokens; bounded exponential retry;
   dead-letter; email sent only after commit; auto-suspension and automatic repair task.
8. **Model input is untrusted**: tool arguments are validated strictly against each tool's schema;
   referenced IDs are resolved through the tenant session; delegation cannot carry authority
   upward and is depth-limited.
7. **Independent QA**: `wwos qa` audits invariants straight from the tables; the acceptance
   suite exercises only public surfaces; agent versions must pass acceptance tests to go live.
