# Test report

_Run on 2026-11-22 against commit `0bf49bf` (branch `claude/peaceful-ride-6fjetj`). Re-run with the
commands at the end. CI runs the same suite on every push (see "CI" below)._

## Result

**175 passed, 0 failed, 0 skipped** (SQLite + PostgreSQL 16 + headless Chromium). Without
`WWOS_TEST_PG_URL` the 12 PostgreSQL tests are skipped (163 run).

The two race tests (`test_memory_store_thread_race_respects_budget`, PostgreSQL
`test_concurrent_reservations_never_exceed_org_budget`) were also run 15 consecutive times: 0 failures.

## What is real and what is simulated

| Real (the actual system) | Simulated (test doubles) |
|---|---|
| Runtime, permission/approval/budget gates, worker, queue, migrations, CRM, documents, API, dashboard, outbox, PostgreSQL, SQLite, SMTP client code, browser | **The language model** (`OfflineProvider`, `ScriptedProvider`, `SimulatedOrganisation`): it decides which tools to call; quality of live Claude decisions is **not** tested. **The mail server** (`tests/fake_smtp.py`, local only). **The Anthropic HTTP API** (mock transport in `test_anthropic_provider.py`). |

No test can reach the internet: an autouse fixture blocks every non-loopback connection
(`tests/conftest.py`, verified by `test_tests_cannot_reach_the_internet`). No AI charges were
incurred and no real person was contacted.

## Coverage by requirement

| Area (mandate §5) | Tests | Key evidence |
|---|---|---|
| Unit & integration | `tests/unit/*` (148) | all services, gates, validation |
| PostgreSQL | `tests/postgres/*` (12) | concurrency, migrations round-trip, isolation, e-mail, full CEO scenario |
| API & browser | `test_end_to_end.py` (10), `tests/browser/*` (5) | auth, roles, dashboard login, approve-in-browser, light/dark, mobile width, cross-site write refused |
| Multi-tenant isolation | `test_isolation.py`, `test_cross_tenant_api_access_is_impossible`, `test_foreign_ids_rejected_in_tool_args`, `test_isolation_on_postgres` | reads, writes, bulk updates, API IDs, tool-argument IDs |
| Agent permissions & privilege escalation | `test_agents_budget_policy.py`, `test_review_regressions.py` (hidden-kwarg injection ×7, type checks, reserved roles, owner-only revision, no tool use when tools not offered, delegation authority) | |
| Dynamic agent creation & replacement | `test_ceo_agent_dynamically_creates_and_delegates_to_a_specialist`, `test_revision_versioning_and_safe_rollout`, `test_malfunctioning_agent_is_suspended_and_recovery_assigned`, `test_failed_acceptance_test_retires_agent` | |
| Concurrent budget enforcement | `test_pg_budget_concurrency.py` (24-thread race, per-agent race, 4 workers × 30 tasks, crash reconciliation, rollback), `test_budget_guard.py`, lineage limit after revision | A mutation test (lock removed) let 15 of 24 reservations through on a $10 budget, proving the test detects overspend |
| E-mail idempotency & failure recovery | `test_email_reliability.py` (14) + PostgreSQL variants | each SMTP failure phase, ambiguous outcomes never resent, owner reconciliation, 4 concurrent dispatchers deliver 20 messages exactly once, worker crash mid-send |
| Migrations & backup restoration | `test_migrations.py`, `test_pg_migrations_isolation.py`, `test_backup_and_restore_round_trip` | fresh → head, upgrade of a real previous-release database (fixture) with every row preserved and a verified pre-migration backup, adoption refusal, destructive gates, PostgreSQL downgrade/upgrade with data |
| End-to-end lead to client | `test_full_business_cycle_lead_to_sent_proposal`, `scripts/smoke.py` | lead → qualify → proposal → approval → sent → won → invoice |
| Autonomous organisation (mandate §6) | `test_ceo_runs_an_objective_end_to_end_without_human_intervention` (SQLite + PostgreSQL) | 10 steps below |
| Confidentiality | `test_partner_confidentiality.py` (21) | obfuscation attempts, forged briefs, NDA withdrawal after approval, sub-domains, previews |
| Production configuration | `test_production_config.py`, `test_deploy_support.py` | unsafe settings refused, Cloud SQL socket URL, structured logs, worker health |
| Regression & security | `test_review_regressions.py` (47) | one test per confirmed finding from four review rounds |

### Autonomous organisation scenario (simulated model)

After the owner submits one objective, only the worker runs. Verified:
1. The CEO receives the objective.
2. It reviews the workforce and partners, and breaks the objective into 4 department tasks.
3. It identifies the missing partner-coordination capability.
4. It creates "Partner Coordinator", which passes its acceptance test, holds a subset of the CEO's tools, has no provisioning authority, and is capped at $5.
5. It assigns the partner task to that specialist.
6. The Sales, Marketing, Finance and partner work runs in parallel through the queue.
7. QA reviews every task, rejects Finance once, then passes it.
8. Failed work is retried: Marketing's simulated model outage gives 2 failed attempts, then the task is reassigned to Operations, which completes it.
9. The CRM is updated: a call is logged, the deal moves to negotiation, and the outcome is logged.
10. An executive report is produced, with the task table, the new agent and AI spend.

The partner brief contains no client identifiers, and the QA invariant scan passes afterwards.
**This demonstrates the orchestration mechanics, not live-model judgement.**

## Other verification

| Check | Result |
|---|---|
| `wwos qa` invariant scan (9 checks) after smoke on PostgreSQL | 9/9 pass |
| Schema drift (types + indexes) SQLite and PostgreSQL | none |
| Clean install of the built wheel in an empty venv, migrations + app import | pass |
| Terraform syntax (python-hcl2, local) | pass; `terraform validate` runs in CI |
| `pip-audit` (independent reviewer run + CI job) | no known vulnerabilities |
| Live server + real Chromium screenshots (light/dark/mobile), 0 console errors | pass |

## CI

`.github/workflows/ci.yml` (on every push) has three jobs:
- **test**: the whole suite with PostgreSQL and Chromium, then migrations to head with a drift check, the smoke test, and the QA scan.
- **audit**: `pip-audit`.
- **infrastructure**: `terraform fmt` and `validate`, then builds the image and runs migrations and the drift check inside it.

CI runs on this branch:
- Run 2 failed: `alembic` was undeclared. Security review round 3 reported this independently; fixed.
- Run 3 failed: the drift check found no models on the CLI path, and the budget settle race. Both fixed in `0bf49bf`.

The latest run's result is recorded in `docs/PROJECT_STATE.md`.

## How to run

```bash
.venv/bin/python -m pytest -q
```
```bash
WWOS_TEST_PG_URL=postgresql+psycopg://wwos@localhost:5432/postgres .venv/bin/python -m pytest -q
```
```bash
WWOS_DATABASE_URL=postgresql+psycopg://... .venv/bin/python scripts/smoke.py && .venv/bin/wwos qa
```
