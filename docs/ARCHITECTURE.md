# Architecture

White Waters AI Business Operating System (`wwos`): a multi-tenant Python service in which
Claude-powered agents, organised into nine departments, run the day-to-day operations of a
consulting firm inside hard limits set by the owner.

```
 owner ──► Dashboard / REST API (FastAPI, wwos/api.py)
           auth: hashed API keys, server-side sessions, roles, Origin checks, security headers
              │ enqueue (idempotent)                         │ decide (with content preview)
              ▼                                              ▼
   Durable queue (tasks) ── claim tokens · leases · retry/backoff · dead-letter ── approvals.py
              │
              ▼ worker (wwos/workflows.py) ─ also: outbox dispatch · schedules · budget reconciliation · QA scan
   ┌──────────┼───────────────────────────────┬──────────────────────────────────┐
   lead workflow                       objective orchestration              agent_run
   intake→qualify→proposal→outreach    plan→run→QA→retry/reassign→report   (delegated / scheduled work)
              │                               │                                  │
              └──────────────► agent runtime (agents/runtime.py) ◄───────────────┘
                               reserve budget (budget_guard) → model (llm.py) → incur
                               tool call → schema validation → permission → owner gate → handler
                                             │
                tools.py ─► crm · documents · comms(outbox) · partners · objectives · lifecycle
                                             │
                db.py: tenant-scoped sessions (ORM filter + write guard) ─ PostgreSQL 16 (prod) / SQLite (dev)
                migrate.py + migrations/: Alembic with backup and destructive gates
```

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Settings from `WWOS_*`; production-mode safety checks; Cloud SQL socket URL assembly |
| `db.py` | Engine, tenant isolation, explicit SQLite transactions, budget hooks on commit/rollback |
| `migrate.py`, `migrations/` | Alembic: upgrade with backups, adoption of pre-migration databases, drift check, gates |
| `models.py` | Tenants, keys, sessions, departments, versioned agents, ledger, reservations, tasks, approvals, audit, CRM, documents, outbox, partners, objectives |
| `policy.py` | Tool permissions, owner-authority categories, partner disclosure detection, audit |
| `budget.py` | Model prices, cost calculation, reporting |
| `budget_guard.py` | Atomic reservations (row lock on PostgreSQL), lineage and org limits, exactly-once ledger, reconciliation |
| `llm.py` | `AnthropicProvider` (Claude API, refusal fallbacks, error classes), `OfflineProvider` |
| `agents/runtime.py` | Tool loop with budget reservation per call and all gates |
| `agents/lifecycle.py` | Provision, acceptance test, versions, suspend, reactivate, replace; containment rules |
| `tools.py` | Agent tool catalogue and strict argument validation |
| `workflows.py` | Queue, worker, retries, leases, self-healing, lead workflow, health endpoint |
| `objectives.py` | CEO objective orchestration |
| `partners.py` | Partner registry, NDA attestation, anonymised briefs, leak detection |
| `comms.py` | Outbox, phase-classified SMTP, dispatch, reconciliation |
| `crm.py`, `documents.py` | CRM rules; Jinja2 documents (autoescaped), reserved kinds |
| `approvals.py` | Owner decisions; approved actions execute as idempotent tasks |
| `schedule.py` | Daily CEO/operations reviews, hourly QA scan, exactly once per period |
| `qa.py` | Independent read-only invariant scanner (9 checks) |
| `backup.py` | Verified SQLite backup/restore |
| `bootstrap.py` | Tenant creation: 9 departments, 9 founding agents (76 % of budget), owner key |
| `logging_setup.py` | JSON logs with Cloud Logging severities in production |

## Departments and agents

| Department | Founding agent | Provisions agents | Notable tools |
|---|---|---|---|
| Executive Office | CEO | yes | agent management, delegation, objectives, partners, budget increase *request* |
| Technology | Technology Director | yes | agent management (repairs and revises failed agents) |
| Sales & Business Development | Sales Lead | no | CRM, proposals, e-mail, partner list |
| Consulting Delivery | Engagement Manager | no | SOWs, status reports, CRM notes |
| Finance | Finance Controller | no | invoices, transfer *request*, budget status |
| Marketing | Marketing Manager | no | contacts, e-mail drafts |
| Legal & Compliance | Legal & Compliance Officer | no | agreements/filings (owner-gated) |
| Operations & People | Operations Manager | no | pipeline, budget, delegation, objective status; fallback assignee |
| Quality Assurance | QA Auditor | no | read-only checks, objective QA verdicts |

Specialists (e.g. Partner Coordinator) are created by the CEO or Technology Director on demand. They
hold only tools their creator holds, never provisioning authority, and never a founding role name.

## Key flows

**Model call.** `budget_guard.reserve` (atomic admission against lineage + organisation limits) →
provider → `incur` (actual cost) → the reservation is tracked on the session → after commit/rollback,
`spill` writes the ledger row and settles the reservation in one transaction.

**Tool call.** `validate_args` (schema, types, amounts) → `check_tool_permission` →
`approval_category` (owner gates, partner detection) → handler in a savepoint → audit.

**Objective.** See `wwos/objectives.py`: CEO plan (may provision specialists) → tasks run as assigned
agents → QA verdict → retry (2) → reassign (department peer, else Operations) → executive report → CRM.

**E-mail.** `draft_email` → `request_send` (approval-gated by policy; partner mail always) →
`dispatch`: queued → sending → sent | queued (definite 4xx, backoff) | failed (5xx) | unknown
(ambiguous) → owner `resolve`.

## Safety properties (enforced in code)

1. Tenant isolation on every ORM statement; foreign IDs are "not found".
2. Model output is untrusted: strict argument validation; no tool runs unless offered.
3. Privilege containment for agent-created agents; reserved roles and document kinds.
4. Owner authority for money, signatures, filings, budget, irreversible actions, partner disclosure,
   client e-mail (policy), with content previews.
5. Atomic budget reservations; ledger exactly once; no spend lost to rollbacks.
6. Idempotency keys (namespaced for agents and API callers), claim tokens, leases, dead-lettering.
7. At-most-once e-mail with reconciliation; stable Message-IDs.
8. Production mode refuses unsafe configuration and unmigrated schemas.
9. Independent QA: invariant scanner, acceptance suite, separate security reviews.

## Deployment shape

See `DEPLOYMENT_GUIDE.md`: Cloud Run (API, worker, migration job), Cloud SQL PostgreSQL 16 with PITR,
Secret Manager, Artifact Registry, Cloud Monitoring; Terraform in `deploy/gcp`; CI/CD in
`.github/workflows`.
