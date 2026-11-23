# Zero-cost operation

**Goal:** run White Waters OS without spending any money. This is the default production profile
(ADR-030).

## What costs money, and what this profile does instead

| Item | Paid design | Zero-cost profile |
|---|---|---|
| AI (Claude API) | Pay per call; there is **no free tier** for the API | **AI off** (`WWOS_LLM_PROVIDER=none`): no model is ever called, every AI budget is $0. A $0 budget means even a misconfiguration cannot spend (tested) |
| Hosting | Google Cloud Run + Cloud SQL ≈ $97/month | **Your own computer** with Docker (Docker Desktop on Windows/macOS, Docker Engine on Linux) |
| Database | Cloud SQL | PostgreSQL in Docker on your computer |
| E-mail | Paid provider or Workspace | **Manual**: approved e-mails wait on the dashboard for you to send yourself. Or your **own mailbox via SMTP** (e.g. Gmail with an app password), capped by `WWOS_EMAIL_DAILY_LIMIT` (default 100/day) |
| Backups | Cloud SQL backups | `scripts/zero_cost_backup.sh`: compressed `pg_dump` files on your disk (keep a copy on a USB drive or your own cloud drive) |
| Monitoring | Cloud Monitoring alerts | Dashboard: the daily briefing, "Needs your attention", `/health` |
| CI | GitHub Actions | GitHub Actions; the repository is **public**, and Actions on standard runners has been free for public repositories (not re-verified 2026-11-23) |

Why not "free" Google Cloud: its always-free small VM still needs a public IPv4 address, which Google
charges at **$0.005/hour (~$3.65/month)**. Only one hour per month is free (Google Cloud network
pricing page, read 2026-11-23). Cloud SQL has no free tier. Your own computer is the only fully
$0 option this project can confirm.

## What keeps working with AI off

The **rules engine** runs the business. None of this uses AI:
- **Leads:** API intake, qualification by transparent scoring, CRM records, proposal generation, outreach email (owner-approved).
- **Daily briefing:** decisions waiting, pipeline, deals stalled for 7 days, new leads, partner pipeline, operational problems.
- **Follow-ups:** a polite follow-up is drafted for each stalled proposal (once per week), for your approval.
- **Won deals:** a draft statement of work, a signature request, and a next-step note for you.
- **Partners:** the acquisition pipeline, owner confirmation, anonymised briefs and owner-approved partner email.
- **Documents:** invoices, statements of work and status reports from templates.
- **Platform:** approval queue, audit log, dashboard, backups, the hourly QA scan, and tenant isolation.

## What pauses with AI off

- **CEO objectives** (planning, delegation, QA review, executive reports): the API refuses them with a clear message.
- **Tasks assigned to agents:** the API refuses them. Internally queued AI work is marked **skipped**, never faked.
- **The AI daily reviews:** replaced by the rules briefing.
- **The Sales agent's advisory notes on qualification:** the scoring decision itself is unaffected.
- **Agent acceptance tests:** reduced to static checks.

## Set-up on your computer

1. Install Docker (Docker Desktop on Windows/macOS) and Git.
2. Get the code:
   ```bash
   git clone https://github.com/groverstech/aiorg_consulting.git && cd aiorg_consulting
   ```
3. Create the settings file and set a long random `POSTGRES_PASSWORD` in it:
   ```bash
   cp deploy/env.zero-cost.example deploy/.env
   ```
4. Start the database, apply the schema, then start the app and the worker:
   ```bash
   docker compose -f deploy/docker-compose.yml up -d db
   ```
   ```bash
   docker compose -f deploy/docker-compose.yml --profile migrate run --rm migrate
   ```
   ```bash
   docker compose -f deploy/docker-compose.yml up -d api worker
   ```
5. Create the firm (this prints your owner key **once**; store it safely):
   ```bash
   docker compose -f deploy/docker-compose.yml exec api wwos create-tenant "White Waters Consulting Group"
   ```
6. Open http://localhost:8000/dashboard and sign in with the key.
7. Schedule `scripts/zero_cost_backup.sh` to run nightly (cron, or Task Scheduler with WSL).

The containers restart automatically when Docker starts. The system works only while the computer is on.
The dashboard is reachable only from that computer. This is deliberate: plain HTTP is accepted only for
`localhost`.

**Status of these steps:** the Compose file and settings are validated, and the same images build and
migrate in CI. The steps have not been run on a desktop machine from this environment (no Docker daemon here).

## Optional: send e-mail through your own mailbox (still $0)

Set `WWOS_EMAIL_TRANSPORT=smtp` and the `WWOS_SMTP_*` values in `deploy/.env`. For Gmail, that means
`smtp.gmail.com`, port 587, `starttls`, your address, and an app password created in your Google Account.

Every client e-mail still needs your approval unless you change `WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL`.
Keep `WWOS_EMAIL_DAILY_LIMIT` below your mailbox's published daily sending limit; check it in your
provider's help pages, since it was not verifiable from this environment.

## Turning AI on later (costs money)

1. Decide a monthly cap.
2. Set `WWOS_LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, and `WWOS_ORG_MONTHLY_BUDGET_USD` / `WWOS_MAX_AGENT_BUDGET_USD`.
3. Raise the existing agents' budgets with `POST /api/agents/{id}/revise`.

Spend can never exceed the caps; this is enforced atomically per call.
