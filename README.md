# White Waters AI Business Operating System

A multi-tenant service in which Claude-powered agents, organised into nine departments, run the
day-to-day operations of White Waters Consulting Group. They handle lead intake and qualification,
CRM, proposals, client correspondence, partner coordination, invoicing and reporting, and they manage
their own workforce. Hard limits set by the owner are enforced in code: budgets, permissions,
approvals and confidentiality.

**Status:** implemented, tested and CI-validated; **not deployed**. **Default profile: zero cost.** It
runs on your own computer with AI off, and the rules engine operates the business
(`docs/ZERO_COST_GUIDE.md`). Paid AI and Google Cloud hosting are opt-in. See `docs/PROJECT_STATE.md`.

## What it does
- **CEO autonomy.** Give the CEO an objective. It plans department tasks, creates missing specialist agents, delegates, gets QA review, retries or reassigns failed work, updates the CRM and writes an executive report.
- **Agent workforce.** Agents are created, tested, versioned, suspended and repaired. Agent-created agents can never exceed their creator's permissions or budget.
- **Budget.** Atomic per-call reservations against agent and organisation limits, recorded in an immutable ledger.
- **Owner approval queue.** Payments, signatures, filings, budget increases, partner disclosures and (by policy) client e-mail all wait for the owner, who sees the exact content.
- **Business data.** CRM with a validated deal pipeline; proposals, SOWs, invoices, status reports, anonymised partner briefs and executive reports.
- **E-mail.** An outbox with at-most-once SMTP delivery and owner reconciliation of uncertain sends.
- **Confidentiality.** Clients are isolated from each other, and partners only ever receive anonymised briefs.

## Documentation
| Document | Contents |
|---|---|
| `docs/ZERO_COST_GUIDE.md` | **Start here:** running at $0 on your own computer |
| `MASTER_IMPLEMENTATION_MANDATE.md` | Reconstructed business mandate (pending owner confirmation) |
| `docs/PROJECT_STATE.md` | Status, results, owner actions |
| `docs/ARCHITECTURE.md` | How it works |
| `docs/SECURITY.md` | Controls, review findings, residual risks |
| `docs/TEST_REPORT.md` | What was tested and the results |
| `docs/DEPLOYMENT_GUIDE.md` | GCP deployment, cost estimate, permissions, rollback |
| `docs/OPERATIONS_MANUAL.md` | Day-to-day operation, monitoring, recovery, incidents |
| `docs/KNOWN_ISSUES.md` | Limitations and their resolution paths |
| `docs/DECISION_LOG.md` | Architecture and policy decisions |
| `docs/AGENT_SDK_EVALUATION.md` | Claude Agent SDK evaluation and secured runtime |

## Quick start (local, no spend)
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```
```bash
.venv/bin/wwos create-tenant "White Waters Consulting Group" --budget 500
```
```bash
.venv/bin/wwos serve
```
```bash
.venv/bin/wwos worker
```
```bash
.venv/bin/python -m pytest -q
```
