# Project state

_Last updated: 2026-11-22. Branch `claude/peaceful-ride-6fjetj`. Update at the end of every session._

## Status at a glance

| Capability | Implemented | Tested | Validated (independent review / CI) | Deployed | Operational |
|---|---|---|---|---|---|
| Multi-tenant platform, auth, roles, dashboard | ✅ | ✅ | ✅ | ❌ | ❌ |
| Nine departments, nine founding agents | ✅ | ✅ | ✅ | ❌ | ❌ |
| Dynamic agent provisioning, versioning, repair | ✅ | ✅ | ✅ | ❌ | ❌ |
| CEO objective orchestration (plan → QA → report) | ✅ | ✅ simulated model | ✅ mechanics | ❌ | ❌ |
| Partner coordination with confidentiality | ✅ | ✅ | ✅ (residual risk: see SECURITY.md) | ❌ | ❌ |
| CRM, lead workflow, documents | ✅ | ✅ | ✅ | ❌ | ❌ |
| Atomic budget enforcement | ✅ | ✅ incl. PostgreSQL concurrency | ✅ | ❌ | ❌ |
| E-mail outbox, SMTP, reconciliation | ✅ | ✅ fake SMTP server | ✅ | ❌ | ❌ (no SMTP account) |
| Live Claude agents | ✅ code | ✅ mock HTTP only | ❌ no live calls | ❌ | ❌ (no API key) |
| Migrations, backups, recovery | ✅ | ✅ incl. previous-release DB | ✅ | ❌ | ❌ |
| GCP infrastructure, CI/CD | ✅ Terraform + workflows | ✅ `terraform validate`, image build in CI | ✅ | ❌ not applied | ❌ |

"Deployed" and "Operational" are ❌ because deployment, paid AI and real e-mail need owner authorisation.

## Latest results
- Tests: **175 passed, 0 failed** (SQLite, PostgreSQL 16, headless Chromium). Details: `TEST_REPORT.md`.
- CI run 4 on `0bf49bf`: **success**. That covers all tests with PostgreSQL and browser, `pip-audit`, `terraform validate`, and the image build with migrations run inside the image.
- Independent security reviews: 4 rounds. All critical, high and medium findings are fixed with regression tests; residual risks are documented. Details: `SECURITY.md`.

## Hardening phase summary (2026-11-22)
- Budget: atomic reservations with a per-tenant row lock, a ledger written exactly once, crash reconciliation, and limits that apply across agent versions.
- E-mail: failures classified by SMTP phase, ambiguous outcomes never retried automatically, owner reconciliation, stable Message-IDs.
- Migrations: Alembic baseline matching the previous release, adoption of older databases, backups before upgrades, destructive gates, drift check, production verification.
- Partners: confidentiality controls, and owner approval with a content preview for every disclosure.
- CEO objectives: autonomous orchestration, validated end to end with a simulated model.
- Production: a production mode that refuses unsafe settings; GCP Terraform; CI/CD with deploy and rollback workflows; a cost estimate.
- Reconstructed master mandate.

## Owner actions (the only blockers)

| ID | Action | Why only the owner | Unblocks |
|---|---|---|---|
| OA-1 | Confirm `MASTER_IMPLEMENTATION_MANDATE.md` (or supply the original) and the open items in its §9 | Business facts and policy | Scope certainty |
| OA-2 | Authorise AI spend (default cap $500/month) and add the Anthropic API key to Secret Manager | New expenditure + credential | Live agents |
| OA-3 | Create/choose a GCP project with billing; authorise about $97/month (see `DEPLOYMENT_GUIDE.md` §2); grant the permissions in §3; run `terraform apply` | Cloud expenditure + account permissions | Deployment |
| OA-4 | Provide an SMTP account on the firm's domain | Credential | Real e-mail |
| OA-5 | Decide whether agents may e-mail clients without per-message approval | Business policy | Unattended outreach |
| OA-6 | Register real partners and attest NDAs only where signed | Legal fact | Partner work |
| OA-7 | GitHub: `production` environment with required reviewers; Workload Identity Federation variables | Repository/cloud admin | Deploy workflow |

## Next steps (after owner actions)
1. Run the scenario suite against the live model with a small cap; tune instructions; set per-agent models by cost.
2. Deploy with the deploy workflow; verify `/health`, `/health/details`, `wwos qa`; run a restore drill.
3. Pin GitHub Actions to commit SHAs before granting deploy credentials.
