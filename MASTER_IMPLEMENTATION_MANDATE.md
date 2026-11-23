# White Waters Consulting Group: Master Implementation Mandate (reconstructed)

**Status: reconstructed, pending owner confirmation (OA-1).** The original Master Implementation
Mandate was never available in this repository, the owner's Google Drive or Gmail (searched
2026-11-22). This document reconstructs it **only** from requirements the owner actually supplied,
and from recorded engineering decisions. Each requirement cites its source:

- **[A]** *Mandatory Amendment: Full Autonomous Execution and Zero Routine Human Dependency*
- **[H]** *Final Autonomous Hardening and Production Readiness Mandate*
- **[V]** *Final Verification and Architecture Decision* instruction
- **[D]** an engineering decision under [A]/[H]/[V] (see `docs/DECISION_LOG.md`)

Where the owner has not supplied a fact, this document says **"not supplied — owner to confirm"**.
No company facts, partner agreements, registrations, revenue, credentials or capabilities are
asserted. **No partner is assumed to exist:** every partner starts as a prospect.

---

## 1. The business

| Item | Content | Source |
|---|---|---|
| Name | White Waters Consulting Group | [A] |
| Model | A consulting firm whose operations are run by an Autonomous AI Business Operating System: AI agents organised into nine departments, under the owner's authority | [A], [H] |
| Delivery network | Indian Chartered Accountant (CA) and Company Secretary (CS) partner firms, and international service providers, acquired and coordinated by the firm | [H §3], [V §3] |
| Services offered, jurisdictions, registrations, commercial terms | **Not supplied — owner to confirm** | — |
| Existing clients, partners, revenue, staff | **None assumed** | — |

## 2. Organisation (nine departments)

| # | Department | Founding agent | Responsibilities | Source |
|---|---|---|---|---|
| 1 | Executive Office | CEO | Objectives, priorities, resource allocation, dynamic agent provisioning, executive reporting | [A §6], [H §6] |
| 2 | Technology | Technology Director | Create, test, version, repair, suspend agents; model selection; cost control | [A §6] |
| 3 | Sales & Business Development | Sales Lead | Lead generation intake, qualification, CRM, proposals, client correspondence; **partner acquisition** (prospect registration, outreach, pipeline) | [H §3], [V §3] |
| 4 | Consulting Delivery | Engagement Manager | Statements of work, milestones, status reports | [H §3], [V §3] |
| 5 | Finance | Finance Controller | Invoices, AI spend reporting, transfer *requests* | [A], [H §3] |
| 6 | Marketing | Marketing Manager | Demand generation, contact capture, campaign drafts | [H §3], [V §3] |
| 7 | Legal & Compliance | Legal & Compliance Officer | Agreements and filings prepared for owner signature/submission | [A §9], [H §3] |
| 8 | Operations & People | Operations Manager | Workflow health, people operations (HR) coordination, fallback assignee | [H §3] |
| 9 | Quality Assurance | QA Auditor | Independent verification, QA verdicts on objective work | [A §4] |

Specialists (e.g. Partner Coordinator, research analysts) are created on demand by the CEO or
Technology Director, inheriting at most their creator's tools and budget headroom. [A §6], [D: ADR-015, ADR-026]

## 3. Autonomy

1. CEO and Technology agents independently identify needs, create, allocate, test, version, replace,
   suspend and improve agents, optimise models and control cost within approved limits. [A §6]
2. Given an objective, the CEO plans department tasks, identifies missing capabilities, creates and
   tests specialists, assigns work within inherited permissions and budgets, coordinates departments,
   obtains QA review, retries or reassigns failed work, updates the CRM and reports to the owner. [H §6]
3. Only the owner can create agents that create agents; founding roles are reserved. [D: ADR-015, ADR-026]
4. Every agent version passes an automated acceptance test before acting. [A §4], [D: ADR-010]

## 4. Partner acquisition and coordination

1. **Indian CA/CS firms and international providers** are recorded with a *stated* category
   (CA firm, CS firm, CA/CS firm, international provider, other). The category is unverified until
   the owner attests the firm's credentials. [V §3], [D: ADR-028]
2. **Acquisition** (agents): identified → contacted → in discussion → NDA requested, or declined. [V §3]
3. **Every message to a prospect** is owner-approved, and the owner sees the full text. [H §3], [D: ADR-028]
4. **Confirmation** (owner only): requires an NDA on file and a partnership agreement. Prospects are
   never treated as partners. [V §3]
5. **Coordination**: only confirmed partners receive work, and only as anonymised briefs. Every disclosure is owner-approved. [H §3]

## 5. Confidentiality and commercial protections

1. Client data is isolated per tenant; no cross-client data in prompts, documents or e-mail. [H §3]
2. Partners never receive client identities: briefs are anonymised, leak-checked and refused if any
   identifier survives; the owner previews every disclosure. [H §3], [D: ADR-021]
3. Partner briefs instruct partners not to identify or contact the end client (non-circumvention
   wording) and state that terms bind only when confirmed in writing by an authorised signatory. [D]
4. Proposals and SOWs are non-binding until signed by the owner; agents may not promise prices,
   dates or terms absent from the CRM or an approved document. [A §9], [D]
5. Specific commercial terms (fees, payment terms, liability caps, governing law, partner revenue
   sharing, non-solicitation periods): **not supplied — owner to confirm.**

## 6. Owner authority (financial and legal boundaries)

The owner alone decides: new cloud expenditure or budget increases; credentials and external
accounts; binding legal commitments and signatures; financial transfers; regulatory filings;
material business-model changes; irreversible production actions; fundamental policy. [A §9]
Plus: outbound client e-mail until the owner changes the policy [H §2B]; every partner message
(outreach and disclosure); partner confirmation and NDA/credential attestation [V §3].
Approval records an authorisation; with no payment/e-signature/regulator integration, the owner
performs the act. [D: ADR-009]

## 7. Technology, hosting and expenditure

| Requirement | Source | Implementation |
|---|---|---|
| Claude models, no dependency on ChatGPT/OpenAI | [A §1, §8] | Anthropic SDKs; default `claude-opus-5-5` |
| Claude Agent SDK | [H §3], [V §2] | Integrated as a secured runtime (`WWOS_AGENT_RUNTIME=agent_sdk`), all safeguards preserved; production default stays the Messages API pending owner confirmation (OA-8). Evidence: `docs/AGENT_SDK_EVALUATION.md` [D: ADR-027] |
| Google Cloud hosting | [H §3, §7], [V §3] | Cloud Run, Cloud SQL PostgreSQL 16, Secret Manager, Artifact Registry, Monitoring (`deploy/gcp`, not applied) |
| **Zero spend** (owner instruction 2026-11-23) | Owner | Default profile: AI off ($0 budgets), rules engine, manual or own-mailbox e-mail, self-hosted on the owner's computer (`docs/ZERO_COST_GUIDE.md`, ADR-030). AI and GCP remain opt-in paid options |
| Controlled expenditure | [A §6], [H §2A], [V §3] | AI: atomic per-call budget reservations (agent lineage + organisation hard caps). Cloud: fixed instance limits and an optional billing budget with alerts [D: ADR-017, ADR-029] |
| Operational autonomy | [A §3, §10] | Durable queue, retries, self-healing, scheduled reviews, hourly QA scan |
| Security, isolation, migrations, e-mail reliability | [A §4], [H §2, §4] | See `docs/SECURITY.md`, ADR-017 to ADR-019 |

## 8. Delivery, testing and reporting

- Phases are milestones, not approval checkpoints; each ends with acceptance and regression tests, independent QA, fixes and documentation. [A §3]
- An independent QA function verifies every critical property. [A §4]
- Errors are diagnosed, fixed and regression-tested. [A §5]
- Idempotency and rollback are used for business actions. [A §5]
- No fabricated success; blockers are isolated with the exact owner action. [A §7], [H §9]
- Synthetic data and simulated AI/e-mail only; simulated model behaviour is never presented as live performance. [H §5]
- No merge, deployment, paid AI, chargeable resources or real external communication without owner authorisation. [H §1, §7], [V]
- Documentation set and final reporting as listed in [H §8–9] and [V §5].

## 9. Compliance assessment (2026-11-22)

| Requirement | Status | Evidence |
|---|---|---|
| Business development & lead generation | **Implemented, tested**: inbound lead API → qualification → proposal → owner-approved outreach; marketing contact capture and campaign drafts. *Gap:* no outbound prospecting integrations (lead databases, LinkedIn); none specified | `test_full_business_cycle_lead_to_sent_proposal` |
| Indian CA/CS partner acquisition & coordination | **Implemented, tested**: categories, pipeline, owner-only confirmation, owner-approved outreach, briefs only to confirmed partners | `test_partner_acquisition_outreach_is_owner_approved_and_prospects_get_no_work`, `test_ca_cs_categories_only_for_indian_partners` |
| International partner acquisition & coordination | **Implemented, tested**: same pipeline (`international_provider`) | same, `test_partner_confidentiality.py` |
| Confidentiality & commercial protections | **Implemented, tested**: isolation, anonymised briefs, owner previews, non-binding wording. *Gap:* specific commercial terms not supplied | `test_partner_confidentiality.py` (21), isolation tests |
| Dynamic specialist-agent creation | **Implemented, tested** (both runtimes) | `test_ceo_agent_dynamically_creates…`, `test_ceo_provisions_a_specialist_through_the_sdk` |
| CEO autonomy | **Implemented, tested with simulated model** (both runtimes); live-model judgement unverified | `test_ceo_runs_an_objective…`, `test_full_autonomous_organisation_scenario_on_the_agent_sdk` |
| All nine departments | **Implemented, tested** (founding agents pass acceptance tests) | bootstrap tests, scenario |
| Financial & legal approval boundaries | **Implemented, tested** | `test_owner_reserved_action_becomes_approval…`, approval tests |
| Marketing & client delivery | **Implemented, tested**: contacts and campaign drafts; won deal → draft SOW from the accepted proposal → owner signature request → Engagement Manager milestone planning; status reports and invoices on demand. Dates, milestones and payment terms stay "to be agreed" (not supplied), so no invoice is raised automatically | `test_won_deal_hands_off_to_delivery_without_inventing_terms` |
| GCP hosting & controlled expenditure | **Prepared, validated in CI** (Terraform validate, image build), not applied; AI caps enforced and tested under concurrency | CI `infrastructure` job, PostgreSQL concurrency tests |
| HR | **Coordination only.** *Gap:* no HR data model; requirements not supplied | — |

## 10. Items for the owner to confirm or supply

1. Whether this reconstruction matches the original mandate (or supply the original).
2. Services, jurisdictions, registrations and commercial terms (§1, §5).
3. Real partner firms (as prospects), and NDAs and agreements only where actually signed (§4).
4. Runtime choice for production (OA-8, §7) and HR requirements (§2).
