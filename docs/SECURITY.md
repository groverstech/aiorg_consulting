# Security

## Model

The language model is treated as **untrusted input**. Every action an agent takes goes through
code-enforced gates; prompts are never the security boundary.

| Control | Enforcement |
|---|---|
| Authentication | Per-tenant API keys (256-bit random, stored as SHA-256). Dashboard uses server-side sessions (random token, hashed, 8 h expiry, revoked on logout), cookie HttpOnly + SameSite=Strict + Secure in production |
| Authorisation | Roles viewer < operator < owner; unknown roles fail closed. Owner-only: approvals, partner NDA attestation, objectives, agent management, e-mail reconciliation, audit log, queue drain |
| Cross-site requests | Origin check on form posts and cookie-authenticated API writes (compares to `WWOS_PUBLIC_ORIGIN` behind TLS proxies) |
| Tenant isolation | ORM-level filter on every tenant-scoped statement + write guard; referenced IDs resolved through the tenant session |
| Agent permissions | Tool allow-list per agent; strict schema validation of every model-supplied argument (no unknown keys, types checked, amounts finite/positive); tools not offered are never executed |
| Privilege containment | Agents grant only tools they hold; only the owner creates/revises/suspends provisioning-capable agents; founding role names reserved; owner-set budgets only raised by the owner |
| Owner authority | Transfers, signatures, filings, budget increases, irreversible purges, partner disclosures (always) and client e-mail (by policy) become approval requests with a content preview; approved actions re-validate before executing |
| Budget | Atomic per-call reservations (PostgreSQL row lock), per-agent-lineage and organisation hard limits, ledger written exactly once. Optional installation hard cap (`WWOS_HARD_MONTHLY_CAP_USD`, environment-only, advisory lock across tenants; approvals cannot exceed it) and spending pacing (ADR-031) |
| Confidentiality | Partner briefs only from the anonymiser (reserved kind), normalised-fingerprint leak check, NDA attested by owner, re-checked at send |
| Prompt injection | Untrusted text (CRM fields, lead text, tool results, error text) cannot trigger privileged actions: privileged actions are gated in code; recovery prompts fence error text as data |
| Secrets | Never in the repository or Terraform variables; Secret Manager in GCP; DB URL assembled at runtime from the socket path and a secret |
| Data protection | Cloud SQL TLS-only, IAM connector, daily backups + PITR; SQLite backups verified; restore takes a safety copy first |
| Headers | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Cache-Control: no-store`; documents served with a CSP that forbids scripts |
| Audit | Every tool call, denial, approval, provisioning, NDA attestation, reconciliation and failure is logged with the acting agent or `api:<key name>` |
| Production mode | Refuses SQLite, plain-text SMTP, insecure cookies, non-https origin, weak admin tokens, unmigrated schema |

## Independent reviews

All reviews were performed by separate reviewer agents with no implementation role, which proved
findings with scripts. Every confirmed finding has a regression test in
`tests/unit/test_review_regressions.py` or `tests/unit/test_partner_confidentiality.py`.

| Round | Scope | Findings | Status |
|---|---|---|---|
| 5 | Agent SDK runtime, budget proxy, partner acquisition (`a0b9510`) | 1 high (billed spend lost when a stream broke), 2 medium (cache-write under-pricing; partner-domain shadowing), 5 low | All fixed with regression tests (`tests/unit/test_review_round5.py`) |
| 1 | Initial system | 1 critical (hidden kwargs → privilege escalation), 3 high (duplicate e-mail on retry, blocked approvals/key squatting, lost acceptance-test spend), 5 medium, 10 low | All fixed; re-verified |
| 2 | Re-verification | 2 medium (endless lease reclaim, Origin check behind TLS proxy), 4 low | All fixed |
| 3 | Full hardened system (`dc6be1b`) | 4 high, 4 medium, 8 low | All fixed; re-verified |
| 3b | Re-verification (`3269a82`) | 1 medium (leak-check bypasses), 3 low | Fixed; residual risk documented below |

### Round 3 (this hardening phase): findings and remediation

| ID | Severity | Finding | Remediation | Evidence |
|---|---|---|---|---|
| H1 | High | Forged `partner_brief` via `docs.generate` bypassed anonymisation and NDA; owner approved without seeing content | Reserved document kinds; send re-verifies provenance, NDA and leaks; preview in dashboard **and** API | `test_forged_partner_brief_is_impossible`, `test_send_brief_rechecks_and_owner_sees_the_content`, `test_api_approvals_include_preview` |
| H2 | High | Anonymisation was literal; spacing/punctuation/abbreviation leaked identity | Normalised fingerprints by identifier type, whole-word replacement, refusal on any surviving fingerprint | `test_obfuscated_identifiers_never_reach_a_brief` (9 cases), `test_round4_obfuscations_never_reach_a_brief` (6 cases) |
| H3 | High | Operator could squat schedule keys and stop every tenant's jobs and QA | API lead keys namespaced; per-tenant isolation in the scheduler | `test_lead_keys_cannot_squat_schedule_keys` |
| H4 | High | `alembic` undeclared: production image could not start | Dependency declared, wheel verified in a clean venv, pinned lock file, CI starts the image and migrates inside it | CI `infrastructure` job; `requirements.lock` |
| M1 | Medium | Revising an agent reset its monthly spend | Limits apply across the agent lineage | `test_revision_does_not_reset_agent_spend` |
| M2 | Medium | Partner sub-domains bypassed partner detection | Sub-domain matching in policy, outbox and QA | `test_partner_subdomains_are_recognised` |
| M3 | Medium | Templates did not set production mode | `WWOS_ENV=production`, required origin, separate migration step | `deploy/env.example`, `deploy/docker-compose.yml` |
| M4 | Medium | NaN/huge/malformed values caused 500s | Finite, bounded, shape-checked inputs | `test_deal_values_validated`, `test_bad_invoice_line_items_are_400` |
| N1 | Medium | Leak check bypassable (homoglyphs, digit swaps, short names, partial names) | Unicode/homoglyph folding, digit-swap undo, short names as whole words, surname parts; API preview | tests above |
| N2 | Low | False positives (generic mailboxes) and mangled short names | Generic mailbox list; whole-word replacement; no literal replacement under 3 chars | `test_generic_mailboxes_and_short_names_do_not_break_ordinary_text` |
| N3 | Low | Agent could raise an owner-set agent budget | Owner-set budgets only raised by the owner | `test_agents_cannot_raise_owner_set_budgets_or_spoof_role_case` |
| N4 | Low | Reserved roles case-sensitive | Role names normalised | same |
| L* | Low | Tool calls without tools offered, login lockout of all users, role hijack, injection into recovery prompt, non-read-only `wwos qa`, restore without safety copy, weak attribution, missing headers | All fixed | `test_no_tools_offered_means_no_tools_executed`, `test_agents_cannot_claim_founding_roles`, `test_security_headers_and_attribution`, `test_successful_logins_do_not_count_towards_lockout` |

Two further defects were found by CI rather than the review: the drift check imported no models on
the CLI path, and a settle window under-admitted budget. Both are fixed in `0bf49bf`.

### Round 5: findings and remediation

| ID | Severity | Finding | Remediation | Evidence |
|---|---|---|---|---|
| R5-H1 | High | Budget proxy released the reservation when the upstream stream dropped or the CLI disconnected after billing, so spend never reached the ledger | After a 200 the call is always recorded; upstream drained even if the CLI hangs up; unreported output priced at the cap | `test_H1_upstream_drop_after_billing_is_recorded_at_worst_case`, `test_H1_client_disconnect_still_drains_and_records_usage` |
| R5-M1 | Medium | Cache writes priced at 1× input (actual up to 2×); premium betas forwarded | Cache writes 2×, reads 1×; premium betas and fast mode stripped | `test_M1_premium_betas_and_fast_mode_are_stripped` |
| R5-M2 | Medium | A prospect sharing a partner's domain could shadow an inactive/confirmed partner | Strictest-match resolution; overlapping domains refused; delivery-time re-check | `test_M2_overlapping_partner_domains_refused`, `test_M2_inactive_partner_blocked_even_after_queueing` |
| R5-L1 | Low | `count_tokens` forwarded for any model, unlimited | Agent model only; 50 per run | `test_L1_count_tokens_needs_the_agent_model_and_is_capped` |
| R5-L2 | Low | Plain e-mail to a prospect not checked for client identity | Every partner-domain message checked against open deals at draft and send | `test_L2_partner_mail_cannot_carry_client_identity` |
| R5-L3 | Low | CLI scratch files and launcher left on disk | TMPDIR per run; launcher removed at exit | `test_cli_scratch_files_stay_inside_the_run_directory` |
| R5-L4 | Low | Proxy read bodies before checking the token; no size or concurrency cap | Token first; 20 MB cap; 32 concurrent requests | `test_L4_token_checked_before_body_and_size_capped` |
| R5-L5 | Low | Streams without a final usage event under-billed | Missing output priced at the cap | H1 tests |

Earlier hands-on findings in the Agent SDK integration were fixed before review:
- The CLI injected inherited account variables into prompts. Fixed with an allowlisted launcher, proven by a canary test.
- A killed worker could leave the CLI running as an orphan. Fixed with a parent-death signal; the test was mutation-checked.
- The CLI requested `max_tokens` of 128000. Fixed by clamping in the proxy.

## Residual risks (accepted, documented)

1. **Anonymisation cannot be complete.** Deliberate misspellings ("Akme"), initials ("AH Ltd"),
   numbers spelled as words and paraphrases ("the logistics firm in Leeds we met") can evade any text
   filter. The control is the **mandatory owner preview**: every partner disclosure waits for the owner,
   who sees the exact text in the dashboard and the API.
2. **Agent SDK runtime depends on CLI behaviour** (identity text and an environment message added to the
   context; defaults that change between releases). Pinned version; offline suite incl. canary must pass
   on every upgrade (`docs/AGENT_SDK_EVALUATION.md`).
3. **Live-model behaviour is untested.** Prompt-injection resistance relies on code gates, which are
   tested; how the live model reacts to adversarial content is not.
4. **Single-process login rate limiting.** Per-process memory; with several API instances the
   effective limit is multiplied. Add a shared store (e.g. database) if exposed to the internet at scale.
5. **CI actions are pinned to version tags, not commit SHAs.** Pin to SHAs before granting the deploy
   workflow production credentials.
6. **Terraform state holds the DB password** (see the deployment guide): keep state in a private bucket.

## Reporting

Security issues: contact the owner directly. Do not file public issues.
