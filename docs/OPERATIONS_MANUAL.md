# Operations manual

For deployment and rollback see `DEPLOYMENT_GUIDE.md`. This manual covers running the system day to
day, monitoring, recovery and incident response.

## 1. Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/wwos create-tenant "White Waters Consulting Group" --budget 500   # prints the owner key once
.venv/bin/wwos serve            # http://127.0.0.1:8000/dashboard
.venv/bin/wwos worker           # second terminal: tasks, e-mail outbox, schedules, QA scans
.venv/bin/python -m pytest -q
```
Defaults: SQLite (`var/wwos.db`, single process only), offline model (no spend), local outbox (no e-mail leaves).

## 2. Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `WWOS_ENV` | `development` | `production` enforces safe settings and refuses an unmigrated schema |
| `WWOS_DATABASE_URL` | `sqlite:///./var/wwos.db` | SQLAlchemy URL (PostgreSQL in production) |
| `WWOS_DATABASE_HOST_SOCKET`, `WWOS_DB_USER`, `WWOS_DB_NAME`, `WWOS_DB_PASSWORD` | — | Cloud SQL socket form, used when `WWOS_DATABASE_URL` is unset |
| `WWOS_LLM_PROVIDER` | `offline` | `anthropic` to call Claude |
| `ANTHROPIC_API_KEY` | — | Claude API key (Secret Manager in GCP) |
| `WWOS_DEFAULT_MODEL` | `claude-opus-5-5` | Model for new agents (approved list in `wwos/budget.py`) |
| `WWOS_AGENT_RUNTIME` | `messages` | `agent_sdk` runs agents on the Claude Agent SDK (needs the `agent-sdk` extra; see `AGENT_SDK_EVALUATION.md`) |
| `WWOS_ANTHROPIC_UPSTREAM` | `https://api.anthropic.com` | Where the Agent SDK budget proxy forwards model calls |
| `WWOS_ORG_MONTHLY_BUDGET_USD` | `500` | Default monthly AI budget for new tenants |
| `WWOS_MAX_AGENT_BUDGET_USD` | `100` | Per-agent monthly ceiling |
| `WWOS_MAX_AGENTS_PER_TENANT` | `40` | Live agent headcount ceiling |
| `WWOS_AGENT_FAILURE_SUSPEND_THRESHOLD` | `3` | Consecutive failures before auto-suspension |
| `WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL` | `true` | Owner policy gate on client e-mail (partner mail is always gated) |
| `WWOS_EMAIL_TRANSPORT` | `outbox` | `smtp` for real delivery |
| `WWOS_SMTP_HOST/PORT/USER/PASSWORD/FROM` | — / 587 | SMTP account |
| `WWOS_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` (port 465) or `none` (refused in production) |
| `WWOS_SMTP_TIMEOUT_SECONDS` | `30` | Per-connection timeout |
| `WWOS_TASK_MAX_ATTEMPTS` | `4` | Retries before dead-lettering |
| `WWOS_TASK_LEASE_SECONDS` | `900` | Worker lease length |
| `WWOS_COOKIE_SECURE` | `false` | `true` in production |
| `WWOS_PUBLIC_ORIGIN` | — | `https://` URL of the dashboard (required in production) |
| `WWOS_ADMIN_TOKEN` | — | Enables `/health/details` (≥ 32 chars) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse proxies whose forwarded headers uvicorn trusts |

## 3. Daily operation (owner)

| Task | Where |
|---|---|
| Decide approvals (e-mails, partner disclosures, payments, signatures, filings, budget) | Dashboard → *Owner approvals* (the exact content is shown), or `GET /api/approvals` + `POST /api/approvals/{id}/decision` |
| Give the CEO an objective | `POST /api/objectives {title, description, deal_id?}`; follow on the dashboard; read the executive report |
| Partners | Agents register prospects and move them through acquisition stages. Owner: `POST /api/partners` (with `category`), `POST /api/partners/{id}/nda {"on_file": true}` only when a signed NDA is on file, `POST /api/partners/{id}/confirm {"agreement_on_file": true, "credentials_verified": …}` only when an agreement is signed (and credentials checked), `/deactivate` |
| Won deals | Moving a deal to *won* drafts the SOW, requests your signature and asks the Engagement Manager for a milestone plan. Set dates, milestones and payment terms yourself |
| Review agents | Dashboard → *Agent workforce*; `POST /api/agents/{id}/suspend`, `/reactivate`, `/revise` |
| Read the audit trail | `GET /api/audit` |
| Issue API keys | `wwos create-key <tenant_id> --name <who> --role operator|viewer|owner` |

The CEO runs a daily operating review, and Operations a daily health check, automatically. The QA
scan runs hourly.

## 4. Monitoring

| Signal | Source | Alert (GCP) |
|---|---|---|
| Service up | `GET /health` (public, minimal) | uptime check |
| Queue depth, dead tasks, unknown e-mail outcomes, last QA scan | `GET /health/details` with `X-Admin-Token` | — |
| Dead-lettered task | log `task <id> -> dead` (ERROR) | `wwos_task_dead` |
| QA invariant violation | log `QA scan:` (ERROR) | `wwos_qa_failed` |
| E-mail outcome unknown | log `delivery outcome unknown` (ERROR) | `wwos_email_unknown` |
| Budget refusals | log `BudgetExceeded` | `wwos_budget_refused` |
| Worker stalled | worker `/healthz` returns 503 after 10 min without a loop | Cloud Run restarts it |

## 5. Runbooks

| Situation | Action |
|---|---|
| **Dead-lettered task** | `GET /api/tasks?status=dead` → read `last_error` → fix the cause → resubmit (new idempotency key). Objective tasks are retried/reassigned automatically before this happens. |
| **Agent auto-suspended** | Non-provisioning agents: the Technology Director receives a repair task automatically. CEO/Technology Director: only the owner can act → dashboard *Needs your attention* → *Re-test & reactivate*, or revise via API. |
| **Budget exhausted** | Calls are refused before spending (no retries). Wait for the month to roll over, re-allocate agent budgets, or approve a `budget.request_increase`. |
| **E-mail outcome `unknown`** | Check the mail provider's sent log. Delivered → `POST /api/messages/{id}/resolve {"delivered": true}`. Not delivered → `{"delivered": false}` (re-queued once, same Message-ID). Never resend by hand outside the system. |
| **E-mail `failed`** | Permanent rejection (e.g. bad address). Correct the contact; redraft. |
| **QA scan failing** | `/health/details` → `last_qa_scan.failed`; run `wwos qa` for details; treat as an incident (section 7). |
| **Partner brief refused** | The scope still identifies the client: rewrite it in neutral terms (no names, contacts, company references). |

## 6. Backup and recovery

| Database | Backup | Restore |
|---|---|---|
| Cloud SQL | Automated daily (14 kept) + PITR 7 days; on-demand before every deploy | Point-in-time clone to a new instance, verify with `wwos db check` and `wwos qa`, then switch the connection (owner-authorised) |
| PostgreSQL (Compose) | `pg_dump -Fc "$URL" > wwos-$(date +%F).dump` nightly | `pg_restore --clean --if-exists -d "$URL" <file>`, then `wwos db check && wwos qa` |
| SQLite (dev) | `wwos backup --dest backups/` (integrity + row-count verified) | Stop the worker; `wwos restore <file> --yes` (takes a safety copy of the current DB first) |

Migrations: `wwos db status | upgrade | downgrade | check`. Upgrades on SQLite back up automatically;
on PostgreSQL they require `--backup-confirmed`; destructive revisions and downgrades require
`--allow-destructive`. A recovery drill (backup → change → restore → QA) is automated in the test suite.

## 7. Incident response

1. **Contain.** Suspend the affected agent(s) (`/api/agents/{id}/suspend`), or stop the worker (GCP:
   set `wwos-worker` max instances to 0). The API stays read-only usable.
2. **Assess.** `GET /api/audit`, worker logs, `wwos qa`, `GET /api/approvals`, budget status.
3. **Credentials.** If a key may be exposed: revoke API keys (set `active=false`), rotate the
   Anthropic/SMTP/DB secrets in Secret Manager (new version), redeploy. Sessions die with their key.
4. **Data.** If data was corrupted: restore to a point in time before the incident (owner-authorised).
5. **Communicate.** The owner decides on any external notification (clients, partners, regulators).
6. **Learn.** Add a regression test and a decision-log entry; run the full suite.

## 8. Scheduled operations (worker)

| Job | Frequency | Guarantee |
|---|---|---|
| CEO daily operating review | daily | exactly once per tenant per day |
| Operations daily health check | daily | exactly once per tenant per day |
| QA invariant scan | hourly | result in `system_status`, shown in `/health/details` |
| Outbox dispatch | every loop | at-most-once delivery |
| Budget reservation reconciliation | every minute | expired reservations become ledger spend exactly once |
