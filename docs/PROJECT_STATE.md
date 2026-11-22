# Project state

_Last updated: 2026-11-22. Update this file at the end of every working session._

## Status summary
The operating system is implemented, tested and deployment-ready. It runs today with the
offline model provider and the local email outbox. Going live with real Claude agents and
real email needs the owner actions listed below; no code changes are required for that.

## Delivered milestones
| # | Milestone | State |
|---|---|---|
| 1 | Multi-tenant data model with enforced isolation | Done, tested (SQLite + PostgreSQL) |
| 2 | Nine departments, nine founding agents | Done |
| 3 | Agent runtime: tool loop, permission, approval and budget gates | Done |
| 4 | Agent lifecycle: provision, acceptance test, version, suspend, reactivate, replace | Done |
| 5 | Dynamic agent creation by the CEO and Technology agents, with privilege containment | Done, tested end to end |
| 6 | Budget enforcement and immutable cost ledger | Done |
| 7 | CRM (contacts, companies, deals, stage machine, activity history) | Done |
| 8 | Documents (proposal, SOW, invoice, status report; HTML + Markdown) | Done |
| 9 | Communications: email outbox with at-most-once delivery, SMTP transport | Done (SMTP not yet configured) |
| 10 | Durable task queue, retries, leases with claim tokens, dead-letter, self-healing | Done |
| 11 | Lead → qualification → proposal → outreach workflow | Done |
| 12 | Owner approval queue (API + dashboard) | Done |
| 13 | Management dashboard and REST API with roles | Done, verified in a real browser (light, dark, mobile) |
| 14 | Scheduler: daily CEO review, daily operations check, hourly QA scan | Done |
| 15 | Independent QA: invariant scanner + acceptance suite + external review | Done |
| 16 | Backup / restore (verified), Docker, Compose with PostgreSQL, CI | Done (container build not run here: no Docker daemon) |

## Test results (latest run)
- `pytest`: **88 passed** (unit, acceptance, provider and review-regression tests).
- PostgreSQL 16: `scripts/smoke.py` OK, `wwos qa` 8/8, claim-token race verified (stale worker gets `lease_lost`, one deal).
- Live server: lead submitted over HTTP, 4 workflow steps succeeded, dashboard rendered with 0 console errors.
- Claude provider: request shape, refusal fallback, error classification verified against a mock HTTP transport. **No live API call has been made** (no credentials; it would spend owner funds).

## Independent QA review
A separate reviewer agent audited the code adversarially and proved its findings with scripts:
1 critical, 3 high, 5 medium and 10 low defects. All were fixed, each with a regression test
(`tests/unit/test_review_regressions.py`), and a second verification pass confirmed the fixes.
That pass raised 2 further medium and 4 low items, now also fixed and tested. While fixing these,
an additional SQLite transaction bug was found and fixed (ADR-014). See ADR-011 to ADR-015.

## Known limitations
- Live Claude behaviour (prompt quality, tool-choice quality) is unverified until an API key is provided.
- No payment, e-signature or regulator integrations: approved transfers, signatures and filings are
  *authorised* records the owner carries out (ADR-009).
- Budget pre-check can overshoot slightly with several concurrent workers (ADR-007).
- An SMTP error raised after the server accepted a message can cause a resend (rare; see ADR-011).
- Schema migrations (Alembic) are needed before the first post-launch schema change (ADR-016).
- Login rate limiting is per process (in memory).
- The master implementation mandate was not available; scope was derived from the amendment (ADR-001).

## Owner actions (the only blockers)
| ID | Action | Why only the owner can do it | Unblocks |
|---|---|---|---|
| OA-1 | Share the original Master Implementation Mandate (add it to the repo as `docs/MASTER_MANDATE.md`) | Not found in the repo, Drive or Gmail | Reconciling scope against the original plan |
| OA-2 | Provide an Anthropic API key and confirm the monthly AI budget (default $500) | New expenditure + credential | Live agents (`WWOS_LLM_PROVIDER=anthropic`) |
| OA-3 | Choose hosting and authorise its cost (any container host + managed PostgreSQL) | New cloud expenditure | Production deployment |
| OA-4 | Provide SMTP credentials and a sending address on the firm's domain | Credential / external account | Real client email |
| OA-5 | Decide whether agents may email clients without per-message approval (`WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL`) | Business policy (§9) | Fully unattended outreach |

## Next steps for the following session
1. On OA-2: run the acceptance suite's scenarios against the live model with a $5 cap, tune agent instructions, choose models per agent by cost.
2. On OA-3: deploy with `deploy/docker-compose.yml`, run `create-tenant`, verify `/health`, `wwos qa`, a backup and a restore.
3. Add Alembic baseline before any schema change.
4. Extend workflows: won deal → SOW → invoice, and a weekly status-report cycle for active engagements.
