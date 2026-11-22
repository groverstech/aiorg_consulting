# White Waters Consulting Group: Master Implementation Mandate (reconstructed)

**Status: reconstructed, pending owner confirmation.** The original Master Implementation Mandate
was never available in this repository, the owner's Google Drive or Gmail (searched 2026-11-22).
This document reconstructs it **only** from requirements the owner actually supplied during the
project, and from decisions recorded in `docs/DECISION_LOG.md`. Each requirement cites its source:

- **[A]** Owner's *Mandatory Amendment: Full Autonomous Execution and Zero Routine Human Dependency*
- **[H]** Owner's *Final Autonomous Hardening and Production Readiness Mandate*
- **[D]** An engineering decision taken under the authority in [A]/[H] (see `docs/DECISION_LOG.md`)

Where the owner has not supplied a fact, this document says **"Not supplied — owner to confirm"**
rather than inventing one. No company facts, partner agreements, registrations, revenue,
credentials or capabilities are asserted here.

---

## 1. The business

| Item | Content | Source |
|---|---|---|
| Legal name | White Waters Consulting Group | [A] (name used throughout) |
| Business model | A consulting firm whose day-to-day operations are run by an "Autonomous AI Business Operating System": AI agents organised into departments, under the owner's authority | [A] |
| Services offered to clients | Not supplied — owner to confirm. The system is service-agnostic; documents describe engagements generically | — |
| Jurisdictions, registrations, tax status | Not supplied — owner to confirm | — |
| Existing clients, revenue, staff | Not supplied — none assumed | — |
| Partner network | Indian partner firms and international service providers exist as a category of collaborator [H]. Named partners, agreements and NDAs: **not supplied** — none are assumed; each must be entered and NDA-attested by the owner | [H] |

## 2. Organisational structure

Nine departments, each staffed by at least one AI agent [A: "all departments are integrated"; H: "all nine departments"]:

| # | Department (code) | Founding agent | Responsibilities | Source |
|---|---|---|---|---|
| 1 | Executive Office (`executive`) | CEO | Autonomous management: priorities, resource allocation, objectives, dynamic agent provisioning, executive reporting | [A §6], [H §6] |
| 2 | Technology (`technology`) | Technology Director | Agent workforce health: create, test, improve, replace, suspend agents; model selection; cost control | [A §6] |
| 3 | Sales & Business Development (`sales`) | Sales Lead | Lead qualification, CRM accuracy, proposals, client correspondence; business development; partner coordination (via specialist agents) | [A], [H §3] |
| 4 | Consulting Delivery (`delivery`) | Engagement Manager | Client delivery: SOWs, milestones, status reports | [H §3] |
| 5 | Finance (`finance`) | Finance Controller | Invoicing, AI cost tracking, transfer *requests* (owner-approved) | [A], [H §3] |
| 6 | Marketing (`marketing`) | Marketing Manager | Demand generation, contact capture, campaign drafts | [H §3] |
| 7 | Legal & Compliance (`legal`) | Legal & Compliance Officer | Agreements and regulatory filings prepared for owner signature/submission | [A §9], [H §3] |
| 8 | Operations & People (`operations`) | Operations Manager | Workflow throughput, service health; people operations (HR) coordination; fallback assignee for reassigned work | [H §3 "operations", "HR"] |
| 9 | Quality Assurance (`qa`) | QA Auditor | Independent verification of other departments' work | [A §4] |

**Partner coordination** (Indian partners and international service providers) [H §3]: performed by
specialist agents the CEO provisions on demand in Sales & Business Development (e.g. "Partner
Coordinator"), under the confidentiality rules in §5. [D: ADR-021]

**HR** [H §3]: assigned to Operations & People. **Gap:** no HR data model (employee records,
contracts, payroll) exists because the owner has not specified HR requirements, and personnel
data raises legal obligations that need owner decisions. Owner to specify.

## 3. Autonomy and the CEO

1. The CEO and Technology agents must independently identify resource needs, create specialist
   agents, allocate resources, manage workloads, replace failed agents, improve instructions, test
   new versions, suspend malfunctioning agents, optimise model selection and control costs —
   within approved infrastructure and expenditure limits. [A §6]
2. Given a business objective, the CEO must: break it into department tasks; identify missing
   capabilities; create and test specialists; assign work within inherited permissions and budgets;
   coordinate departments; have results reviewed by QA; retry or reassign failed work; update CRM
   and activity records; produce an executive report — without human intervention. [H §6]
3. Agents created by agents inherit at most their creator's permissions and budget headroom, and
   only the owner can create agents that themselves create agents. [D: ADR-012, ADR-015]
4. Every new agent or agent version must pass an automated acceptance test before it can act. [A §4], [D: ADR-010]

## 4. Owner authority (human intervention policy)

The owner is interrupted **only** for [A §9]:

- new cloud expenditure or budget increases beyond authorised limits;
- missing credentials or external account permissions;
- binding legal commitments and signing agreements;
- financial transfers;
- regulatory filings requiring authorised submission;
- material changes to the business model;
- irreversible actions affecting production data or infrastructure;
- fundamental business policy decisions.

Implemented as owner-approval categories (`wwos/policy.py`), plus two policy categories the owner
has not yet released [D: ADR-006, ADR-021]: outbound client e-mail (approval required until the
owner changes `WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL` [H §2B]) and any disclosure to a partner
(always approved). Routine technical and operational work never waits for the owner. [A §2]

## 5. Confidentiality

[H §3: "Confidentiality between the firm, clients, Indian partners and international service providers."]

1. **Between clients**: strict tenant isolation for every business that uses the platform, and no
   cross-client data in prompts, documents or e-mail.
2. **Clients → partners**: partners only ever receive anonymised briefs. Client company names,
   contact names, e-mail addresses, domains and phone numbers are replaced by a code name; the brief
   is refused if any identifier survives.
3. **Partners**: work can only be shared with a partner whose NDA the owner has attested is on file.
   Agents can register prospective partners but can never record an NDA.
4. **Every** message to a partner domain requires owner approval, regardless of e-mail policy.
5. Partner records and briefs are tenant-scoped like all other data.

## 6. Commercial restrictions and legal safeguards

1. Proposals state they are not binding until a statement of work is countersigned by an authorised
   signatory; SOWs are drafts until signed by the owner. [A §9]
2. Agents must not promise prices, dates or terms that are not in the CRM or an approved document. [D]
3. No agent can sign, file, pay or commit the firm; such actions become approval requests, and
   approval records an *authorisation*: the owner (or a future integration) performs the act. [A §9], [D: ADR-009]
4. Specific commercial terms (rates, payment terms, liability caps, governing law): **not supplied — owner to confirm.**

## 7. Technology and operations

| Requirement | Source | Implementation |
|---|---|---|
| Use Claude models; do not depend on ChatGPT/OpenAI | [A §1, §8] | Anthropic SDK; default model `claude-opus-5-5`, per-agent override |
| Claude Agent SDK | [H §3] | **Deviation, recorded:** agents use the Anthropic API SDK with an in-process tool loop, not the Claude Agent SDK, because the Agent SDK's built-in tools (shell, file editing) are inappropriate for business agents and every tool call must pass through this system's permission, approval and budget gates [D: ADR-004, ADR-022]. Owner may override. |
| Google Cloud Platform | [H §3, §7] | Cloud Run (API, worker, migration job), Cloud SQL PostgreSQL 16, Secret Manager, Artifact Registry, Cloud Monitoring — Terraform in `deploy/gcp`, not applied |
| Budget controls | [A §6], [H §2A] | Atomic per-call reservations; per-agent and organisation hard limits; immutable ledger |
| Operational autonomy | [A §3, §10] | Durable queue, retries, self-healing, scheduled CEO/operations reviews, hourly QA scan |
| Data isolation, security | [A §4], [H §4] | Tenant isolation in the ORM; independent security reviews |
| Migrations | [H §2C] | Alembic with backup safeguards and destructive gates |
| E-mail reliability | [H §2B] | Outbox, phase-classified SMTP, reconciliation; approval queue preserved |

## 8. Delivery, testing and reporting requirements

- Phases are milestones, not approval checkpoints; each phase ends with acceptance tests,
  regression tests, independent QA review, fixes, documentation. [A §3]
- A separate QA function verifies functional correctness, security, data isolation, agent
  permissions, budget enforcement, workflow reliability, CRM accuracy, document generation,
  communications, agent lifecycle and end-to-end operations. [A §4]
- Errors are diagnosed, fixed, regression-tested and documented; operations that could duplicate
  business actions use idempotency, safe retries and rollback. [A §5]
- No fabricated success; blocked items are isolated with the exact owner action needed. [A §7], [H §9]
- Tests use synthetic data and simulated AI/email services; no AI charges, no real contacts;
  simulated AI performance is never presented as live-model performance. [H §5]
- Do not merge, deploy, activate paid AI or initiate real external communication without owner
  authorisation; do not create chargeable cloud resources. [H §1, §7]
- Documentation to maintain: README, this mandate, PROJECT_STATE, ARCHITECTURE, SECURITY,
  TEST_REPORT, DEPLOYMENT_GUIDE, OPERATIONS_MANUAL, KNOWN_ISSUES, DECISION_LOG. [H §8]
- Final report: what was implemented, tested, results, security, infrastructure status, departments,
  provisioning, CRM/workflows, budget controls, limitations, owner actions, cost and permissions,
  branch, commits and CI status. [A §11], [H §9]

## 9. Items for the owner to confirm or supply

1. Whether this reconstruction matches the original mandate (or supply the original).
2. Services offered, jurisdictions, registrations and standard commercial terms (§1, §6).
3. Named partners and NDA status (§5); HR requirements (§2).
4. Whether the Claude Agent SDK deviation (§7) is acceptable.
5. The e-mail approval policy, AI budget, GCP project and credentials (see `docs/PROJECT_STATE.md`).
