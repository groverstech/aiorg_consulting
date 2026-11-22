# White Waters AI Business Operating System

A multi-tenant service in which Claude-powered agents, organised into nine departments, run the
day-to-day operations of White Waters Consulting Group: lead intake, qualification, CRM,
proposals, client correspondence, invoicing, reporting and their own workforce management.
Hard limits set by the owner (budget, permissions, approvals) are enforced in code.

**What it does today**
- Lead → qualification → proposal → outreach workflow, fully automated, with retries and idempotency.
- CRM: contacts, companies, deals with a validated stage machine, and a complete activity history.
- Documents: proposals, statements of work, invoices and status reports (HTML + Markdown).
- Communications: transactional email outbox (SMTP-ready), sent exactly once.
- Agent workforce: a CEO and Technology Director that create, test, version, suspend and repair
  specialist agents themselves, within budget and without exceeding their own permissions.
- Owner approval queue for money, signatures, filings, budget increases and (by policy) client email.
- Budget enforcement per agent and per organisation, with an immutable cost ledger.
- Management dashboard, REST API, background worker with a daily CEO review, and an hourly QA scan.

**Start here**: `docs/RUNBOOK.md` (run and deploy), `docs/ARCHITECTURE.md` (how it works),
`docs/PROJECT_STATE.md` (status, test results, owner actions).

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/wwos create-tenant "White Waters Consulting Group" --budget 500
.venv/bin/wwos serve & .venv/bin/wwos worker
.venv/bin/python -m pytest -q
```
