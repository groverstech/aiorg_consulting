# Decision records

Each record: context → decision → consequences. New decisions are appended; superseded ones stay
with a note.

### ADR-001: Build from the autonomy amendment; master mandate not found
**Context.** The owner's instructions reference a "Master Implementation Mandate" that this
amendment supersedes in part. It was not in the repository (empty at start), Google Drive or Gmail.
**Decision.** Derive scope from the amendment itself: departments, CEO and Technology agents with
dynamic agent creation, CRM, document generation, communications, budget enforcement, data
isolation, workflow reliability, management dashboard, QA, deployment readiness.
**Consequences.** Anything specific to the master mandate (named technology stack, budget
figures, department list, integrations, branding) may differ. Recorded as owner action OA-1.

### ADR-002: Python 3.12 / FastAPI / SQLAlchemy 2 / PostgreSQL (SQLite for dev)
Mature, typed, easy to operate on one small server or a managed platform. SQLite needs no setup
for development and tests; PostgreSQL is the production target and is verified by the smoke test
and the QA scan.

### ADR-003: Claude via the official Anthropic SDK; deterministic offline provider
Default model `claude-opus-5-5` (per-agent override; approved list in `budget.MODEL_PRICES`).
Requests use `output_config.effort=medium` and server-side refusal fallbacks. The `offline`
provider makes the system fully runnable and testable with zero spend and no credentials.

### ADR-004: Self-hosted agent loop rather than Managed Agents
Agents act on our own database through our own permission, approval and budget gates on every
tool call. Keeping the loop in-process makes those gates impossible to bypass and keeps tenant
data in our database. Revisit if agents need sandboxes (code execution, file work).

### ADR-005: Tenant isolation enforced in the ORM layer
`with_loader_criteria` on every ORM statement in a tenant session plus a `before_flush` guard,
instead of per-query discipline. Raw SQL is restricted to `system_session` code (health, QA,
backup) that never serves tenant requests.

### ADR-006: External email requires owner approval by default
Outbound mail to clients speaks for the firm, which makes it a business-policy decision (mandate §9).
`WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL=true` until the owner sets it to false. Internal work
proceeds regardless; only the send waits.

### ADR-007: Budget pre-check uses a worst-case estimate
Before each call: estimated input (chars ÷ 3) + `max_tokens` of output at list price, plus
unpersisted spend of the current run. Output can never exceed `max_tokens`; the input estimate
is conservative. Residual risk: tokenizer variance on unusual text, and *concurrent* runs of the
same agent can each pass the check before either records spend. With one worker this cannot
happen; with several, overshoot is bounded by (workers × one call). Accepted and documented.

### ADR-008: Deterministic qualification, LLM-assisted notes
Lead qualification decisions use a transparent score (company, stated need, budget vs minimum)
so the pipeline is reproducible and auditable; the Sales agent adds an advisory assessment. An
LLM failure can never block or flip a decision.

### ADR-009: Approved owner actions without integrations are "authorised", not "executed"
No banking, e-signature or regulator integration exists. Approving `finance.transfer_funds`,
`legal.sign_agreement` or `compliance.submit_filing` records the authorisation; the owner (or a
future integration) performs the act. The system never claims a payment was made.

### ADR-010: Agent changes ship as tested versions
`revise` creates version N+1 under the same lineage, runs the acceptance test, and only then
retires version N. A failing revision is retired immediately and the working version keeps running.

### ADR-011: Outbox delivery is at-most-once, with "unknown" for humans
Business steps only *queue* email (`comms.request_send`); `comms.dispatch` sends after commit and
records `queued → sending → sent` in separate committed transactions. If a worker dies between
the SMTP hand-off and recording `sent`, the message becomes `unknown` after 10 minutes and is
shown on the dashboard. It is never re-sent automatically: a duplicate client email is worse
than one that needs checking.

### ADR-012: Strict tool-argument validation is the security boundary for model input
The model controls tool arguments. `tools.validate_args` rejects unknown keys and wrong types
before the approval gate and before any handler runs, and handlers take explicit parameters.
Origin: the independent review proved that `**kwargs` passthrough let an agent set internal
parameters (privilege escalation).

### ADR-013: Claim tokens make task commits conditional
Each claim stamps a fresh `claim_token`; results commit through a conditional UPDATE on that
token, so a worker whose lease expired can never commit after another worker reclaimed the
task. Leases default to 15 minutes to cover long multi-turn agent runs.

### ADR-014: SQLite transactions are managed explicitly
pysqlite defers `BEGIN` until the first write, which turns an early SAVEPOINT release into a
commit. The engine disables driver autocommit handling and emits `BEGIN` itself (SQLAlchemy's
documented recipe), so outer rollbacks undo nested work on SQLite exactly as on PostgreSQL.

### ADR-015: Authority does not flow through delegation
Only the owner (or another provisioning-capable agent) can assign work to a provisioning-capable
agent, via API or `tasks.delegate`. Agents cannot delegate to themselves, and delegation chains
are limited to depth 3. This prevents an operator or a department agent from borrowing the
CEO's provisioning authority.

### ADR-016: Schema created with `create_all`; migrations needed before the first schema change in production
There is no production database yet, so tables are created from the models. Before the first
schema change after go-live, introduce Alembic with a baseline revision of the deployed schema.
