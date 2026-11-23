# Project state

_Last updated: 2026-11-23. Branch `claude/peaceful-ride-6fjetj` (not merged, not deployed).
Update at the end of every session. CI status of every commit:
https://github.com/groverstech/aiorg_consulting/actions?query=branch%3Aclaude%2Fpeaceful-ride-6fjetj_

## Status at a glance

| Capability | Implemented | Tested | Validated (independent review / CI) | Deployed | Operational |
|---|---|---|---|---|---|
| Multi-tenant platform, auth, roles, dashboard | ✅ | ✅ | ✅ | ❌ | ❌ |
| Nine departments, founding agents | ✅ | ✅ | ✅ | ❌ | ❌ |
| Dynamic agent provisioning, versioning, repair | ✅ | ✅ (both runtimes) | ✅ | ❌ | ❌ |
| CEO objective orchestration | ✅ | ✅ simulated model, both runtimes | ✅ mechanics | ❌ | ❌ |
| Business development: lead → proposal → owner-approved outreach | ✅ | ✅ | ✅ | ❌ | ❌ |
| Partner acquisition (Indian CA/CS, international) and confidential coordination | ✅ | ✅ | ✅ round 5 | ❌ | ❌ |
| Client delivery hand-off (won → SOW → signature request → plan) | ✅ | ✅ | CI | ❌ | ❌ |
| Atomic budget enforcement (both runtimes) | ✅ | ✅ incl. PostgreSQL concurrency | ✅ | ❌ | ❌ |
| E-mail outbox, SMTP, reconciliation | ✅ | ✅ fake SMTP | ✅ | ❌ | ❌ (no SMTP account) |
| Claude Agent SDK runtime (secured, optional) | ✅ | ✅ real CLI, fake API | ✅ round 5 | ❌ | ❌ |
| Zero-spend mode (AI off, rules engine, manual e-mail) | ✅ | ✅ (providers booby-trapped in tests) | CI | ❌ (owner's computer) | ❌ |
| $20/month AI plan (hard cap, pacing, model tiers, prompt caching) | ✅ | ✅ incl. PostgreSQL concurrency | CI | ❌ | ❌ (needs API key and credit) |
| Live Claude agents (optional, paid) | ✅ code | ✅ mock/fake API only | ❌ no live calls | ❌ | ❌ (off by owner instruction) |
| Migrations (0001–0004), backups, recovery | ✅ | ✅ incl. previous-release DB | ✅ | ❌ | ❌ |
| GCP infrastructure, CI/CD, cloud budget alerts | ✅ Terraform + workflows | ✅ `terraform validate`, image build in CI | ✅ | ❌ not applied | ❌ |

## Latest results
- Tests: **225 passed, 0 failed, 0 skipped** on the $20 plan: SQLite, PostgreSQL 16, headless Chromium, and the real Claude Code CLI against a local fake API (`TEST_REPORT.md`).
- CI: every job and step (test, audit, infrastructure) passed on `31813ea`, the zero-spend restructuring (run 45112612575), as on every push since run 4.
- Security: 5 independent review rounds. Every critical and high finding is fixed with a regression test; the residual risks are documented (`SECURITY.md`).
- Dependency audit (`pip-audit`, including the Agent SDK extra): no known vulnerabilities.

## Decisions this phase
- **ADR-027 (Agent SDK).** Integrated as a secured, selectable runtime; the Messages API remains the default pending owner choice (OA-8). Evidence: `AGENT_SDK_EVALUATION.md`.
- **ADR-028 (partners).** Acquisition is separated from owner-only confirmation; no prospect is treated as a partner.
- **ADR-029 (cloud spend).** Optional GCP billing budget with alerts.

## Profiles: zero cost, or the $20 plan (owner decisions 2026-11-23)
AI is off, so no model is ever called and every AI budget is $0. The rules engine runs:
- leads, proposals and follow-ups;
- the daily briefing;
- won-deal hand-offs;
- partner acquisition;
- the approval queue.

E-mail is manual (or your own mailbox). Hosting is your own computer. Details: `ZERO_COST_GUIDE.md`, ADR-030.

**$20 plan (approved budget):** the same setup with AI on, never more than $20 a month (installation hard
cap), paced across the month. Each department gets the right model: Opus for client-facing work and the
CEO, Sonnet for analysis, Haiku for routine checks. Prompt caching is on and AI reviews run weekly.
`deploy/env.low-cost.example`, `wwos ai-plan`, ADR-031.

## Owner actions to run at $0

| ID | Action | Why only the owner |
|---|---|---|
| Z-1 | Install Docker on a computer you own and follow `ZERO_COST_GUIDE.md` (about 15 minutes) | Needs your machine |
| Z-2 | Optional: connect your own mailbox (SMTP app password) so approved e-mails send automatically | Your credential |
| OA-1 | Confirm `MASTER_IMPLEMENTATION_MANDATE.md`; supply services and commercial terms | Business facts |
| OA-5 | Decide whether agents may e-mail clients without per-message approval (default: approval required) | Business policy |
| OA-6 | Enter real partner firms; attest NDAs, agreements and credentials only where actually signed or verified | Legal facts |

## To turn on the $20 plan

| ID | Action | Why only the owner |
|---|---|---|
| OA-2 | Create an Anthropic API key, buy $20 of credit (auto top-up off), set `deploy/.env` from `env.low-cost.example`, run `wwos ai-plan --budget 20` (`ZERO_COST_GUIDE.md`, "The $20 plan") | Your account, credential and payment |

## Optional paid upgrades beyond $20 (only if you later decide to spend more)

| ID | Upgrade | Cost |
|---|---|---|
| OA-3 / OA-7 | Google Cloud hosting (always on, reachable anywhere) | ≈ $97/month (recommended tier) per `DEPLOYMENT_GUIDE.md` |
| OA-8 | Agent SDK runtime (only with AI on) | +~$2.63/month on GCP |

## Next steps
1. Owner runs the zero-cost setup (Z-1), then signs in and checks the first daily briefing.
2. Run a backup and a restore drill on that machine (`scripts/zero_cost_backup.sh`, then `pg_restore`).
3. Fill commercial terms into templates once supplied.
4. With the $20 plan on: after the first week, compare actual spend per department (dashboard) with the estimates and rebalance with `wwos ai-plan` if needed.
