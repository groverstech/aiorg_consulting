# Deployment guide

> **Default since 2026-11-23: zero cost.** Run on your own computer with AI off. See
> `docs/ZERO_COST_GUIDE.md` and ADR-030. Everything below describes the **paid** Google Cloud option,
> kept for when the owner decides to spend money.

**Status: prepared, not deployed.** No cloud resources have been created and nothing has been
deployed. Every step below that creates a chargeable resource needs the owner's authorisation and
credentials. CI validates the Terraform (`terraform validate`) and builds and starts the container image on
every push.

## 1. Target architecture (Google Cloud, `deploy/gcp/`)

| Component | Service | Configuration |
|---|---|---|
| Web API + dashboard | Cloud Run service `wwos-api` | 1 vCPU / 512 MiB, request-based billing, scale 0–3, `/health` probes |
| Background worker | Cloud Run service `wwos-worker` | 1 vCPU / 512 MiB, instance-based billing, exactly 1 instance, `/healthz` probe, internal ingress |
| Schema migrations | Cloud Run job `wwos-migrate` | `wwos db upgrade --backup-confirmed`, run only after an on-demand backup |
| Database | Cloud SQL for PostgreSQL 16, Enterprise | `db-custom-1-3840` (1 vCPU, 3.75 GiB), 10 GiB SSD (auto-grow), daily backups ×14, point-in-time recovery (7 days), TLS-only, IAM-authorised connector |
| Secrets | Secret Manager | `wwos-anthropic-api-key`, `wwos-smtp-password`, `wwos-db-password`, `wwos-admin-token` |
| Images | Artifact Registry `wwos` | keeps the 10 most recent images |
| Identity | Service account `wwos-runtime` | `cloudsql.client`, `secretmanager.secretAccessor` (per secret), `logging.logWriter`, `monitoring.metricWriter` only |
| Monitoring | Cloud Monitoring | uptime check on `/health`; alerts on uptime, QA-scan failure, dead tasks, unknown e-mail outcomes, budget refusals |

## 2. Cost estimate (monthly, USD)

Prices were read from cloud.google.com pricing pages on **2026-11-22** (us-central1, list price,
excluding taxes and committed-use discounts). 730 hours per month.

| Item | Recommended | Budget | High availability |
|---|---:|---:|---:|
| Cloud SQL compute | $49.31 (1 vCPU × $0.0413/h + 3.75 GiB × $0.007/GiB-h) | $25.55 (db-g1-small, $0.035/h; **no SLA**) | $98.62 (HA rates $0.0826 + $0.014/GiB-h) |
| Cloud SQL SSD, 10 GiB | $1.70 ($0.000232877/GiB-h) | $1.70 | $3.40 (HA rate) |
| Cloud SQL backups, ~10 GiB used | $0.80 ($0.000109589/GiB-h) | $0.80 | $0.80 |
| Worker (always on, 1 vCPU, 0.5 GiB) | $44.71 | $44.71 | $44.71 |
| API (scale to zero) | $0.16 | $0.16 | $0.16 |
| Artifact Registry (~2 GiB, 0.5 GiB free) | $0.15 | $0.15 | $0.15 |
| Secret Manager (4 secrets ≤ 6 free versions; < 10,000 accesses) | $0.00 | $0.00 | $0.00 |
| Logging (< 50 GiB free), uptime checks (< 1 M free) | $0.00 | $0.00 | $0.00 |
| **Total Google Cloud** | **$96.83** | **$73.07** | **$147.84** |

How these figures were calculated:
- **Worker:** 2,628,000 vCPU-s less 240,000 free × $0.000018 = $42.98, plus 1,314,000 GiB-s less 450,000 free × $0.000002 = $1.73. The Cloud Run free tier is shared across the whole billing account; if other projects already use it, add up to **$5.22**.
- **API:** 30,000 requests a month at 200 ms billed time, priced with no free tier: CPU $0.144 + memory $0.0075 + requests $0.012. Keeping one API instance warm (no cold starts) adds **$9.86**.

Additional items:
- **Alerting policies:** $0.35 per metric reference per month (5 policies = **$1.75/month**). The pricing page shows that charge taking effect **1 Sept 2027**; it is $0 before then.
- **Network egress** (SMTP and Claude API calls): expected < 1 GiB/month, inside the 1 GiB North America free tier.
- **Claude API usage is separate and owner-capped:** at most `WWOS_ORG_MONTHLY_BUDGET_USD` (default **$500/month**), enforced atomically per call. Model prices are in `wwos/budget.py`.
- **Not included:** domain name, e-mail provider fees, GitHub plan, taxes.

**Expected total with the default AI cap: about $97 (Google Cloud) + up to $500 (Claude) per month.**
Prices change; re-check them before approving.

## 3. Permissions the owner must provide

1. A GCP project with an active billing account (owner authorises the spend above).
2. For the one-time `terraform apply`: Project Owner, or the combination `roles/run.admin`,
   `roles/cloudsql.admin`, `roles/secretmanager.admin`, `roles/artifactregistry.admin`,
   `roles/iam.serviceAccountAdmin`, `roles/resourcemanager.projectIamAdmin`, `roles/monitoring.admin`,
   `roles/logging.configWriter`, `roles/serviceusage.serviceUsageAdmin`.
3. For GitHub deployments: a deploy service account reachable through Workload Identity Federation
   (no keys), with `roles/run.admin`, `roles/artifactregistry.writer`, `roles/cloudsql.editor`
   (on-demand backups) and `roles/iam.serviceAccountUser` on `wwos-runtime`.
4. GitHub repository settings: environment `production` with required reviewers (the owner), and
   variables `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SERVICE_ACCOUNT`.
5. Secret values (entered by the owner in Secret Manager, never in the repository): a random
   database password, a random admin token (≥ 32 characters), the Anthropic API key (only when AI
   spend is authorised), and the SMTP password (only when e-mail is authorised).

## 4. First deployment (owner-run, in order)

```bash
cd deploy/gcp && cp terraform.tfvars.example terraform.tfvars   # fill in project, domain, alert e-mail
terraform init
terraform apply -target=google_project_service.apis -target=google_secret_manager_secret.s
```
Add secret values (the DB password must exist before the database user is created):
```bash
openssl rand -base64 32 | gcloud secrets versions add wwos-db-password --data-file=-
openssl rand -hex 32 | gcloud secrets versions add wwos-admin-token --data-file=-
```
Build and push the first image (tag = git SHA), then create everything else:
```bash
terraform apply -var image_tag=<sha>
gcloud run jobs execute wwos-migrate --wait --region us-central1
gcloud run services proxy wwos-api --region us-central1   # or open the service URL
```
Create the firm's tenant and owner key (prints the key once):
```bash
gcloud run jobs create wwos-bootstrap --image <image> --command wwos --args "create-tenant,White Waters Consulting Group,--budget,500" ...
```
Then verify: `GET /health` returns ok; `GET /health/details` with `X-Admin-Token` shows
`last_qa_scan.passed=true`; sign in to the dashboard; take a manual Cloud SQL backup and restore it
into a scratch instance once to prove recovery.

**State file contains secrets.** Terraform reads the DB password to create the SQL user, so keep
Terraform state in a private, versioned GCS bucket with access limited to the owner (or create the
SQL user with `gcloud sql users set-password` and drop that resource).

## 5. Routine deployments

GitHub → Actions → **deploy** → enter the commit SHA that passed CI. After the `production`
environment approval, the workflow runs these steps; nothing happens on push:
1. Builds and pushes the image.
2. Takes an on-demand Cloud SQL backup.
3. Runs the migration job.
4. Deploys the new revision with **no traffic**.
5. Health-checks it.
6. Moves traffic to it.

## 6. Rollback

- **Application:** Actions → **rollback** with the previous API revision name and image SHA (traffic
  switch, seconds), or `gcloud run services update-traffic wwos-api --to-revisions <rev>=100`.
- **Schema:** migrations are additive; older code runs against a newer schema in normal cases.
  If a schema rollback is unavoidable, restore the pre-deploy backup (point-in-time recovery to just
  before the migration) after owner authorisation; `wwos db downgrade` requires
  `--allow-destructive --backup-confirmed`.

## 7. Single-host alternative (Docker Compose)

`deploy/docker-compose.yml` runs the same image with PostgreSQL on one VM:
1. `cp deploy/env.example deploy/.env` and fill it in. It sets `WWOS_ENV=production`, so a TLS proxy and an `https://` public origin are required.
2. Back up the database.
3. Run `docker compose -f deploy/docker-compose.yml --profile migrate run --rm migrate`.
4. Run `docker compose -f deploy/docker-compose.yml up -d api worker`.

## 8. Agent runtime option

The default runtime is the Messages API (`WWOS_AGENT_RUNTIME=messages`). To run agents on the Claude
Agent SDK instead (`docs/AGENT_SDK_EVALUATION.md`):
1. Build the image with `--build-arg EXTRAS=postgres,agent-sdk` (+364 MB).
2. Set `worker_memory = "1Gi"` in Terraform. At the fetched prices this is about +$2.63/month, for 1,314,000 more GiB-seconds.
3. Set `WWOS_AGENT_RUNTIME=agent_sdk` on the api and worker services.

The real API key stays in the worker; the CLI subprocess never receives it.

## 9. Activating live services (owner decisions)

| To activate | Change | Authorisation needed |
|---|---|---|
| Live Claude agents | add `wwos-anthropic-api-key` secret version; `llm_provider = "anthropic"` | AI spend up to the configured cap |
| Real e-mail | SMTP secret + `smtp_host`, `smtp_from`, `smtp_user` | E-mail account; messages still wait for approval |
| Agents e-mail clients without per-message approval | `WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL=false` | Business-policy decision (partner mail stays approval-gated regardless) |
| Partner sharing | confirm the partner (`/confirm`) after attesting the NDA | A signed NDA **and** partnership agreement |
| Agent SDK runtime | see section 8 | Owner choice (OA-8) |
| Cloud budget alerts | `billing_account_id`, `monthly_cloud_budget_usd` in Terraform | Billing Account permissions |
