# Known issues and limitations

Each item states its impact and the path to resolution. None is a known critical defect.

| # | Issue | Impact | Resolution path |
|---|---|---|---|
| 1 | **Live Claude behaviour unverified.** All agent decisions in tests come from simulated models. | Quality of planning, QA verdicts, writing and prompt-injection resistance of the live model is unknown. Code gates hold regardless. | Owner authorises AI spend (OA-2); run the acceptance scenarios against the live model with a small cap; tune instructions. |
| 2 | **Not deployed.** No GCP resources exist. | System runs only locally/CI. | Owner actions OA-3/OA-4 (deployment guide). |
| 3 | **Anonymisation is best-effort.** Misspellings, initials, spelled-out numbers and paraphrase can evade text filters. | Possible client identification in a partner brief. | Every partner disclosure is owner-approved with full preview (mandatory control). |
| 4 | **SMTP cannot guarantee exactly-once.** Ambiguous outcomes become `unknown`. | Rare manual reconciliation; a manual resend may duplicate (same Message-ID mitigates). | Use a provider API with idempotency keys and delivery status lookup if volume grows. |
| 5 | **Budget estimate uses chars÷3 for input tokens.** | Unusual text could tokenise above the estimate; overshoot bounded by one call per concurrent run. | Use the token-counting endpoint before large calls if needed. |
| 6 | **No payment, e-signature or regulator integrations.** | Approved actions are recorded authorisations; the owner performs them. | Add integrations when the owner selects providers. |
| 7 | **No HR data model.** | HR responsibilities are coordination only. | Owner to specify HR requirements (personal data has legal obligations). |
| 8 | **Claude Agent SDK not used** (ADR-022). | Deviation from the stated stack. | Owner may overrule; the runtime is isolated behind `wwos/llm.py` + `agents/runtime.py`. |
| 9 | **Original master mandate unavailable.** | Scope reconstructed from owner instructions (`MASTER_IMPLEMENTATION_MANDATE.md`). | Owner confirms or supplies the original. |
| 10 | **Login rate limit is per process.** | Multiplied with several API instances. | Shared store if internet-exposed at scale. |
| 11 | **CI actions pinned to tags, not SHAs.** | Supply-chain exposure for the deploy workflow. | Pin to SHAs before granting production credentials. |
| 12 | **Terraform state contains the DB password.** | State must be protected. | Private, versioned GCS state bucket, or set the password with `gcloud` outside Terraform. |
| 13 | **SQLite is development-only** (single process). | Not for multi-process or production use (enforced in production mode). | Use PostgreSQL. |
| 14 | **Existing tenants keep their founding agents' original tool lists** after upgrades that add tools (e.g. objectives/partners tools added this phase). | Older tenants' CEO/QA lack new tools until revised. | Owner revises those agents (`POST /api/agents/{id}/revise` with the new tool list). No production tenants exist yet. |
| 15 | **Node 20 deprecation warnings** in GitHub Actions. | None today. | Update action versions. |
