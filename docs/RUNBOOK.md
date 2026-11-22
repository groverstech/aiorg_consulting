# Runbook

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
wwos create-tenant "White Waters Consulting Group" --budget 500   # prints the owner API key once
wwos serve            # http://127.0.0.1:8000/dashboard (sign in with the key)
wwos worker           # separate terminal: processes tasks continuously
pytest -q             # unit + acceptance tests
wwos qa               # independent invariant scan of the live database
```

With no configuration, the system runs on SQLite (`var/wwos.db`), the offline model provider
(no spend) and the local email outbox (nothing leaves the machine).

## Production (Docker Compose, single host)

1. `cp deploy/env.example deploy/.env`, then set `POSTGRES_PASSWORD`, `ANTHROPIC_API_KEY`,
   `WWOS_LLM_PROVIDER=anthropic`, the budget ceilings and SMTP settings.
2. `docker compose -f deploy/docker-compose.yml up -d --build`
3. `docker compose -f deploy/docker-compose.yml exec api wwos create-tenant "White Waters Consulting Group" --budget 500`
4. Put a TLS-terminating reverse proxy (Caddy, nginx or a cloud load balancer) in front of
   port 8000. **Set the dashboard cookie to `secure` behind TLS**: it is `secure=False` today
   so that it works on plain-HTTP localhost.
5. Check: `curl https://<host>/health` shows `"status":"ok"`; then `wwos qa` passes inside the api container.

Any container platform works (Fly.io, Render, Cloud Run, ECS, a VM). Run one `api` service and
one `worker` service against managed PostgreSQL; the worker is safe to scale horizontally
(lease-based claiming), but see ADR-007 about concurrent spend checks.

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `WWOS_DATABASE_URL` | `sqlite:///./var/wwos.db` | SQLAlchemy URL; Postgres: `postgresql+psycopg://user:pass@host/db` |
| `WWOS_LLM_PROVIDER` | `offline` | `anthropic` to call Claude |
| `ANTHROPIC_API_KEY` | (none) | Claude API key, read by the SDK |
| `WWOS_DEFAULT_MODEL` | `claude-opus-5-5` | Model for new agents (approved list in `wwos/budget.py`) |
| `WWOS_ORG_MONTHLY_BUDGET_USD` | `500` | Default budget for new tenants |
| `WWOS_MAX_AGENT_BUDGET_USD` | `100` | Per-agent monthly ceiling |
| `WWOS_MAX_AGENTS_PER_TENANT` | `40` | Live agent headcount ceiling |
| `WWOS_AGENT_FAILURE_SUSPEND_THRESHOLD` | `3` | Consecutive failures before auto-suspension |
| `WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL` | `true` | Owner policy gate on client email (ADR-006) |
| `WWOS_EMAIL_TRANSPORT` | `outbox` | `smtp` for real delivery (`WWOS_SMTP_*`) |
| `WWOS_TASK_MAX_ATTEMPTS` | `4` | Retries before dead-lettering |
| `WWOS_TASK_LEASE_SECONDS` | `300` | Worker lease length |

## Routine operations

| Situation | Action |
|---|---|
| Pending owner decisions | Dashboard → *Owner approvals*, or `GET /api/approvals` + `POST /api/approvals/{id}/decision` |
| Dead-lettered task | `GET /api/tasks?status=dead`; read `last_error`; fix the cause; re-submit with a new idempotency key |
| Agent auto-suspended | The Technology Director automatically receives a repair task. Owner override: `POST /api/agents/{id}/reactivate` or `/revise` |
| Budget exhausted | Agents stop calling models (tasks fail with `BudgetExceeded`, no retries). Raise `monthly_budget_usd` via an approved `budget.request_increase` |
| New API key | `wwos create-key <tenant_id> --name alice --role operator` |

## Backup and recovery

* **SQLite**: `wwos backup --dest backups/` makes an online copy and verifies it (integrity check
  plus row counts vs source). Restore: `wwos restore backups/<file>.db --yes` (stop the worker first).
* **PostgreSQL**: use the provider's automated snapshots plus a nightly logical dump:
  `pg_dump -Fc "$DATABASE_URL" > wwos-$(date +%F).dump`; restore with
  `pg_restore --clean --if-exists -d "$DATABASE_URL" wwos-<date>.dump`, then run `wwos qa`.
* The backup → modify → restore round trip is covered by the acceptance test
  `test_backup_and_restore_round_trip`.

## Monitoring

* `GET /health`: database reachability, queued and dead task counts (wire to an uptime monitor;
  alert when `dead_tasks > 0`).
* Logs: structured stdout from the worker (`task <id> -> <status>`).
* `wwos qa` on a schedule (e.g. hourly cron in the worker container); a non-zero exit means an
  invariant was violated.
* Audit trail: `GET /api/audit` (owner): every tool call, denial, approval, provisioning and failure.
