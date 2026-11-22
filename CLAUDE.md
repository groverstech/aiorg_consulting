# CLAUDE.md: working on this repository

White Waters Consulting Group AI Business Operating System (`wwos`). Read in this order before
changing anything: `docs/PROJECT_STATE.md` (where things stand, what is next),
`MASTER_IMPLEMENTATION_MANDATE.md`, `docs/ARCHITECTURE.md`, `docs/DECISION_LOG.md`,
`docs/SECURITY.md`, `docs/OPERATIONS_MANUAL.md`.

## Operating mandate (summary)
The owner's mandate requires autonomous execution: make routine engineering decisions yourself,
record them in `docs/DECISION_LOG.md`, test, fix and continue without asking for approval between
phases. Escalate to the owner **only** for: new spend or budget increases, missing credentials,
binding legal commitments, financial transfers, regulatory filings, material business-model or
policy changes, and irreversible production actions. Never fabricate success; record blockers in
`docs/PROJECT_STATE.md` under "Owner actions".

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # all tests (must stay green)
.venv/bin/wwos qa                      # invariant scan of the configured database
WWOS_DATABASE_URL=... .venv/bin/python scripts/smoke.py   # end-to-end smoke on any DB
WWOS_TEST_PG_URL=postgresql+psycopg://user@host/postgres .venv/bin/python -m pytest -q  # + PostgreSQL tests
.venv/bin/wwos db status | upgrade | check                 # migrations
```

## Rules that must not regress
- Tenant isolation lives in `wwos/db.py`; tenant data is accessed only through `tenant_session`.
  `system_session` is for platform code (health, QA, backup, worker claiming, key lookup).
- Agents act only through `agents/runtime.execute_tool`, which applies permission → owner-approval → handler.
- New owner-reserved actions go in `policy.APPROVAL_REQUIRED_TOOLS`.
- Model calls must go through `agents/runtime.run_agent_prompt` (budget_guard reservation per call);
  never write `CostEntry` directly: the ledger is written by `budget_guard` when the session ends.
- Every schema change needs an Alembic revision (`wwos/migrations/versions`); mark data-losing
  revisions `destructive = True`; CI checks drift.
- Untrusted text (CRM fields, documents, tool results, errors) must never decide a privileged action.
- Tests must not reach the network (enforced by `tests/conftest.py`); never make real AI calls or send real e-mail.
- Every new side effect that can be retried needs an idempotency key.
- HTML templates must render with autoescape on (`documents._env(True)`); keep the XSS tests.
- Add tests with every change; acceptance tests in `tests/acceptance` use public surfaces only.
- Do not put model identifiers in commit messages.
