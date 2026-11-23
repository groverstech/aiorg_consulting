# Known issues and limitations

Each item states its impact and the path to resolution. None is a known critical defect.

| # | Issue | Impact | Resolution path |
|---|---|---|---|
| 0 | **AI is off until the owner turns on the $20 plan.** | Until then, CEO objectives, agent tasks and AI reviews pause; the rules engine runs leads, proposals, follow-ups, briefings, hand-offs, partners. | OA-2: API key, $20 credit, `env.low-cost.example`, `wwos ai-plan` (`ZERO_COST_GUIDE.md`). |
| 0c | **$20 plan capacity figures are estimates.** Runs per month assume ~12,000 input tokens (mostly cached) and ~3,000 output tokens per run; no live call has been made. | Real usage may differ; the cap and pacing hold regardless, so the risk is fewer runs, never more spend. | After the first week, compare actual spend per department on the dashboard and rebalance with `wwos ai-plan`. |
| 0d | **Paced work waits until the next UTC midnight**, and a run refused part-way restarts from the beginning the next day (its earlier calls are already paid). | Slower progress on busy days; a little spend repeated. | Raise `WWOS_BUDGET_PACING_BURST_PCT` (more up-front room) or unset it to turn pacing off; the hard cap still applies. |
| 0a | **Zero-cost hosting is the owner's computer.** | Runs only while it is on; dashboard local-only; backups on local disk unless copied elsewhere. | Paid GCP option documented; any always-on machine the owner already has also works. |
| 0b | **Free-tier facts not re-verifiable here** (Gmail limits, GitHub Actions for public repos, Tailscale): those hosts were blocked in this session. | Figures not quoted; daily e-mail cap configurable. | Owner checks provider pages. |
| 1 | **Live Claude behaviour unverified.** All agent decisions in tests come from simulated models. | Quality of planning, QA verdicts, writing and prompt-injection resistance of the live model is unknown. Code gates hold regardless. | Owner authorises AI spend (OA-2); run the acceptance scenarios against the live model with a small cap; tune instructions. |
| 2 | **Not deployed.** No GCP resources exist. | System runs only locally/CI. | Owner actions OA-3/OA-4 (deployment guide). |
| 3 | **Anonymisation is best-effort.** Misspellings, initials, spelled-out numbers and paraphrase can evade text filters. | Possible client identification in a partner brief. | Every partner disclosure is owner-approved with full preview (mandatory control). |
| 4 | **SMTP cannot guarantee exactly-once.** Ambiguous outcomes become `unknown`. | Rare manual reconciliation; a manual resend may duplicate (same Message-ID mitigates). | Use a provider API with idempotency keys and delivery status lookup if volume grows. |
| 5 | **Budget estimate uses chars÷3 for input tokens.** | Unusual text could tokenise above the estimate; overshoot bounded by one call per concurrent run. | Use the token-counting endpoint before large calls if needed. |
| 6 | **No payment, e-signature or regulator integrations.** | Approved actions are recorded authorisations; the owner performs them. | Add integrations when the owner selects providers. |
| 7 | **No HR data model.** | HR responsibilities are coordination only. | Owner to specify HR requirements (personal data has legal obligations). |
| 8 | **Production runtime choice pending.** The Claude Agent SDK runtime is implemented and tested offline (ADR-027); the default remains the Messages API. | Owner choice (OA-8). | `WWOS_AGENT_RUNTIME=agent_sdk`, image extra `agent-sdk`, worker 1 GiB. |
| 8a | **Agent SDK: CLI-controlled context.** The CLI prepends an identity sentence and a mid-conversation environment message (working directory, platform) to every run; behaviour can change with CLI releases. | Text outside WWOS control in agent context (no tenant/account data after the allowlist). | Pin `claude-agent-sdk`; re-run `tests/unit/test_agent_sdk_runtime.py` (including the canary test) on every upgrade. |
| 8b | **Agent SDK: ~217 MiB per concurrent run, +364 MB image.** | Higher worker memory. | 1 GiB worker when using the SDK runtime. |
| 9 | **Original master mandate unavailable.** | Scope reconstructed from owner instructions (`MASTER_IMPLEMENTATION_MANDATE.md`). | Owner confirms or supplies the original. |
| 10 | **Login rate limit is per process.** | Multiplied with several API instances. | Shared store if internet-exposed at scale. |
| 11 | **CI actions pinned to tags, not SHAs.** | Supply-chain exposure for the deploy workflow. | Pin to SHAs before granting production credentials. |
| 12 | **Terraform state contains the DB password.** | State must be protected. | Private, versioned GCS state bucket, or set the password with `gcloud` outside Terraform. |
| 13 | **SQLite is development-only** (single process). | Not for multi-process or production use (enforced in production mode). | Use PostgreSQL. |
| 16 | **Commercial terms not supplied.** SOW dates, milestones, payment terms and partner terms stay "to be agreed". | No automatic invoicing; owner completes documents. | Owner supplies standard terms; templates and workflows can then fill them. |
| 17 | **No outbound lead-generation integrations** (lead databases, social networks). | Lead generation is inbound (API/web form) plus agent-drafted campaigns. | Add integrations once the owner selects providers and accepts their terms. |
| 14 | **Existing tenants keep their founding agents' original tool lists** after upgrades that add tools (e.g. objectives/partners tools added this phase). | Older tenants' CEO/QA lack new tools until revised. | Owner revises those agents (`POST /api/agents/{id}/revise` with the new tool list). No production tenants exist yet. |
| 15 | **Node 20 deprecation warnings** in GitHub Actions. | None today. | Update action versions. |
