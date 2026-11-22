# Project state

_Last updated: 2026-11-22. Branch `claude/peaceful-ride-6fjetj` (not merged, not deployed).
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
| Live Claude agents | ✅ code | ✅ mock/fake API only | ❌ no live calls | ❌ | ❌ (no API key) |
| Migrations (0001–0004), backups, recovery | ✅ | ✅ incl. previous-release DB | ✅ | ❌ | ❌ |
| GCP infrastructure, CI/CD, cloud budget alerts | ✅ Terraform + workflows | ✅ `terraform validate`, image build in CI | ✅ | ❌ not applied | ❌ |

## Latest results
- Tests: **198 passed, 0 failed, 0 skipped** on `71223e0`: SQLite, PostgreSQL 16, headless Chromium, and the real Claude Code CLI against a local fake API (`TEST_REPORT.md`).
- CI: every job and step passed on `c017555`, `851814d` and later pushes; the final HEAD is verified in the completion report.
- Security: 5 independent review rounds. Every critical and high finding is fixed with a regression test; the residual risks are documented (`SECURITY.md`).
- Dependency audit (`pip-audit`, including the Agent SDK extra): no known vulnerabilities.

## Decisions this phase
- **ADR-027 (Agent SDK).** Integrated as a secured, selectable runtime; the Messages API remains the default pending owner choice (OA-8). Evidence: `AGENT_SDK_EVALUATION.md`.
- **ADR-028 (partners).** Acquisition is separated from owner-only confirmation; no prospect is treated as a partner.
- **ADR-029 (cloud spend).** Optional GCP billing budget with alerts.

## Owner actions (the only blockers)

| ID | Action | Why only the owner | Unblocks |
|---|---|---|---|
| OA-1 | Confirm `MASTER_IMPLEMENTATION_MANDATE.md` (or supply the original); supply services, jurisdictions and commercial terms (§10) | Business facts and policy | Scope certainty, filled SOW/partner terms |
| OA-2 | Authorise AI spend (default cap $500/month); add the Anthropic API key to Secret Manager | New expenditure + credential | Live agents |
| OA-3 | GCP project with billing; authorise ~$97/month; grant permissions (`DEPLOYMENT_GUIDE.md` §3); run `terraform apply`; optionally set `billing_account_id` for budget alerts | Cloud expenditure + account permissions | Deployment |
| OA-4 | SMTP account on the firm's domain | Credential | Real e-mail |
| OA-5 | Decide whether agents may e-mail clients without per-message approval | Business policy | Unattended client outreach |
| OA-6 | Enter real partner firms; attest NDAs, agreements and credentials only where actually signed or verified | Legal facts | Partner work |
| OA-7 | GitHub `production` environment with required reviewers; Workload Identity Federation variables; pin actions to SHAs | Repository/cloud admin | Deploy workflow |
| OA-8 | Choose the production agent runtime: `messages` (default, recommended) or `agent_sdk` (+364 MB image, 1 GiB worker, ~$2.63/month) | Architecture preference in the original mandate | Final runtime |

## Next steps (after owner actions)
1. Live-model trial with a small cap on both runtimes; tune agent instructions; choose models per agent.
2. Deploy with the deploy workflow; verify `/health`, `/health/details`, `wwos qa`; run a restore drill.
3. Fill commercial terms into templates once supplied.
