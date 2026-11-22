# Decision log

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

### ADR-007: Budget pre-check uses a worst-case estimate (superseded by ADR-017)
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

### ADR-016: Schema created with `create_all` (superseded by ADR-019)
There is no production database yet, so tables are created from the models. Before the first
schema change after go-live, introduce Alembic with a baseline revision of the deployed schema.

### ADR-017: Atomic budget reservations (supersedes ADR-007)
Every model call reserves its worst-case cost first. The admission check (committed ledger spend +
all active reservations + this estimate ≤ limit, for the agent lineage and the organisation) and the
insert of the reservation are one atomic step: on PostgreSQL a separate committed transaction holding
`SELECT … FOR UPDATE` on the tenant's `budget_locks` row; on SQLite (dev only) a process lock. After the
call the reservation carries the actual cost; the ledger row is written exactly once (unique
`reservation_id`) when the business transaction commits **or** rolls back, so neither savepoints nor
failures can lose spend. Crashed workers' reservations expire and are reconciled. Concurrent overspend
is eliminated (tested with 24 threads and 4 workers on PostgreSQL; a mutation test removing the lock
fails). Residual: provider tokenisation can exceed the char/3 input estimate on unusual text.

### ADR-018: E-mail delivery classified by SMTP phase (refines ADR-011)
Failures before the message body is transmitted are definite non-deliveries (4xx → retry with
backoff, 5xx → failed). After transmission only an explicit reply is definitive; a timeout or drop
becomes `unknown`, is never retried automatically, and is reconciled by the owner (confirm delivered,
or re-queue once). Each outbox row has a stable RFC Message-ID so recipients can de-duplicate a manual
resend. Exactly-once is impossible over SMTP; this is at-most-once with human reconciliation.

### ADR-019: Alembic migrations with safeguards (supersedes ADR-016)
Baseline 0001 equals the schema of the release before migrations (verified by adopting a database
created by that release's code). `init_db` migrates in development and only *verifies* in production;
production migrations run as a separate job after a backup. PostgreSQL upgrades require
`--backup-confirmed`; revisions flagged `destructive` and all downgrades require
`--allow-destructive`. Strict drift detection (types and indexes) runs in CI.

### ADR-020: Explicit production mode
`WWOS_ENV=production` refuses SQLite, plain-text SMTP, insecure cookies, a non-https public origin and
short admin tokens, and refuses to start on an unmigrated schema.

### ADR-021: Partner confidentiality by construction
Partners (Indian firms, international providers) receive only anonymised briefs generated by
`partners.prepare_brief` (reserved document kind). Identity removal is literal replacement plus a
normalised-fingerprint leak check that refuses rather than guesses. NDA status is an owner
attestation. Every message to a partner domain (sub-domains included) is owner-approved, and sending
re-verifies provenance, NDA and leaks. Partner coordination is done by specialists the CEO
provisions (no founding partner agent), keeping the nine departments the owner specified.

### ADR-022: Claude Agent SDK not used (owner-visible deviation)
The hardening mandate lists the Claude Agent SDK. Its value is a coding/filesystem harness with
built-in shell and file tools; business agents here must have *only* tools that pass this system's
permission, owner-approval and budget gates, act on a tenant-scoped database, and be testable
offline. The Anthropic API SDK with an in-process loop gives that control (ADR-004). Revisit if
agents need sandboxed code execution; the owner can overrule.

### ADR-023: Objectives — deterministic coordination, model judgement
The CEO decides the plan and specialists; agents do the work; QA decides pass/fail. Assignment,
retry (2 attempts), reassignment (same department, else Operations), QA gating, reporting and
spend accounting are deterministic code so the process is reliable and auditable regardless of
model behaviour. A QA run without a verdict counts as a failure.

### ADR-024: SQLite is development-only; write paths take the lock up front
SQLite cannot share reservations across processes and deadlocks on read→write lock upgrades.
Production refuses it. Short write paths (ledger, outbox state, task claims) use `BEGIN IMMEDIATE`.

### ADR-025: GCP target architecture
Cloud Run for the API (request-based billing, scale to zero) and the worker (instance-based, one
always-on instance), a Cloud Run job for migrations, Cloud SQL PostgreSQL 16 Enterprise with PITR,
Secret Manager for every credential (the DB URL is assembled at runtime so no password appears in
Terraform variables or env), a single least-privilege runtime service account, uptime and log-based
alerts. Chosen for lowest operating effort at small scale. Terraform describes; the owner applies.

### ADR-026: Reserved names
Document kinds `partner_brief` and `executive_report` can only be produced by their owning workflow,
and the nine founding role names can only be used by the owner, so agents can neither forge
confidential artefacts nor hijack role-based routing (security review round 3).
