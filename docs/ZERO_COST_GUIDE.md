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

## The $20 plan: AI on, at most $20 a month

Owner decision, 2026-11-23: up to **$20** for AI. Everything above stays the same: your own computer,
$0 hosting, the free rules engine and free e-mail. The whole $20 goes to Claude, spent where it helps
most (ADR-031).

### How the $20 is protected

| Safeguard | What it does |
|---|---|
| **Hard cap** `WWOS_HARD_MONTHLY_CAP_USD=20` | Total AI spend across the installation in a calendar month can never pass $20. It is set only in `deploy/.env`: no approval, dashboard action or agent can raise it. Checked atomically before every model call, with the worst case priced in (tested with 24 concurrent workers on PostgreSQL) |
| **Organisation and agent budgets** | Each department has its own share, so one agent cannot use everyone's budget |
| **Spending pace** `WWOS_BUDGET_PACING_BURST_PCT=25` | Spreads the budget over the month. By day *d*, spend may reach $20 × *d* / days-in-month + $5. Unused allowance carries forward, so a quiet week leaves more for later. Work over today's allowance **waits until tomorrow**; it is not lost, and no agent is penalised |
| **Prepaid credit** (your Anthropic account) | Buy only the credit you mean to spend and leave automatic top-up off; then the account itself cannot be charged more. Check these options in the Anthropic Console, since they could not be verified from here |

### Where the money goes

The right model for each job:
- the best model writes anything a client or partner reads;
- the cheapest model does the routine checking.

| Tier | Model (price per million tokens, in/out) | Departments | Share of $20 | Typical runs/month* |
|---|---|---|---|---|
| Premium | Claude Opus 5.5 ($4 / $20) | CEO, Sales, Delivery, Legal | $4.00, $3.60, $2.00, $0.80 | ~42, ~38, ~21, ~8 |
| Standard | Claude Sonnet 5.5 ($2 / $10) | Technology, QA, Marketing; new specialist agents | $1.20, $1.20, $1.00 | ~25, ~25, ~21 |
| Economy | Claude Haiku 5.5 ($0.10 / $0.50, low effort) | Operations, Finance | $0.60, $0.40 | ~250, ~166 |
| Unallocated | n/a | Specialists the CEO creates | $5.20 | n/a |

\* Each estimate assumes a typical run of about 4 model turns: 12,000 input tokens, mostly cached, and
3,000 output tokens. This is an estimate, not a measurement: no live calls have been made. The dashboard
and `GET /api/budget` show actual spend.

Other savings:
- **Prompt caching.** Within an agent run, each turn re-reads the instructions, tools and earlier turns at
  a tenth of the normal input price.
- **Weekly AI reviews.** The CEO and Operations reviews run weekly (`WWOS_AI_REVIEW_CADENCE=weekly`). The
  free rules-based briefing still runs every day.
- **The Messages API runtime.** It is lighter than the optional Agent SDK runtime.

What $20 typically buys in a month (estimate):
- about 9–10 leads taken from intake to a sent proposal by the Sales agent;
- several CEO objectives;
- weekly reviews;
- room for a few specialist agents.

To rebalance, run `wwos ai-plan` with another budget, or revise one agent's budget. Your edits must stay
within the $20 cap.

### Switching from $0 to the $20 plan

1. Create an API key in the Anthropic Console, and buy **$20** of credit with automatic top-up off.
2. Edit `deploy/.env` so it matches `deploy/env.low-cost.example`. That means
   `WWOS_LLM_PROVIDER=anthropic`, your `ANTHROPIC_API_KEY`, and the five settings in the example file:
   - `WWOS_HARD_MONTHLY_CAP_USD=20`
   - `WWOS_ORG_MONTHLY_BUDGET_USD=20`
   - `WWOS_MAX_AGENT_BUDGET_USD=5`
   - `WWOS_BUDGET_PACING_BURST_PCT=25`
   - `WWOS_AI_REVIEW_CADENCE=weekly`
3. Restart, then apply the plan. It shows the table above and moves each department to its model and
   share as a new, tested agent version:
   ```bash
   docker compose -f deploy/docker-compose.yml up -d api worker
   ```
   ```bash
   docker compose -f deploy/docker-compose.yml exec api wwos ai-plan --tenant "White Waters Consulting Group" --budget 20
   ```
   Add `--dry-run` to see the plan without changing anything. Applying it with AI on makes 9 short test
   calls: one per department, a few cents in total.
4. The dashboard's spend card shows the spend and how much is usable today.

To go back to $0, set `WWOS_LLM_PROVIDER=none` and restart. Nothing else is needed.
