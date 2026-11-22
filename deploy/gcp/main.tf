# White Waters OS on Google Cloud. DESCRIBES resources only: nothing is created until an
# authorised owner runs `terraform apply` with their own credentials (see docs/DEPLOYMENT_GUIDE.md).
#
# Resources: Artifact Registry repo, Cloud SQL for PostgreSQL 16 (private to the project,
# IAM-authenticated connector), Secret Manager secrets (values added by the owner, never here),
# least-privilege service accounts, Cloud Run services (api, worker), a Cloud Run job for
# migrations, uptime check and alert policies.

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  image   = "${var.region}-docker.pkg.dev/${var.project_id}/wwos/wwos:${var.image_tag}"
  db_conn = google_sql_database_instance.main.connection_name
  common_env = {
    WWOS_ENV                              = "production"
    WWOS_LLM_PROVIDER                     = var.llm_provider
    WWOS_DEFAULT_MODEL                    = "claude-opus-5-5"
    WWOS_ORG_MONTHLY_BUDGET_USD           = var.org_monthly_ai_budget_usd
    WWOS_MAX_AGENT_BUDGET_USD             = var.max_agent_budget_usd
    WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL = "true"
    WWOS_EMAIL_TRANSPORT                  = var.smtp_host == "" ? "outbox" : "smtp"
    WWOS_SMTP_HOST                        = var.smtp_host
    WWOS_SMTP_PORT                        = "587"
    WWOS_SMTP_SECURITY                    = "starttls"
    WWOS_SMTP_FROM                        = var.smtp_from
    WWOS_SMTP_USER                        = var.smtp_user
    WWOS_COOKIE_SECURE                    = "true"
    WWOS_PUBLIC_ORIGIN                    = var.public_origin
    FORWARDED_ALLOW_IPS                   = "*" # only Google's front end can reach the container
    # Unix socket provided by the Cloud Run Cloud SQL integration.
    WWOS_DATABASE_HOST_SOCKET = "/cloudsql/${google_sql_database_instance.main.connection_name}"
  }
  secrets = {
    ANTHROPIC_API_KEY  = "wwos-anthropic-api-key"
    WWOS_SMTP_PASSWORD = "wwos-smtp-password"
    WWOS_DB_PASSWORD   = "wwos-db-password"
    WWOS_ADMIN_TOKEN   = "wwos-admin-token"
  }
}

resource "google_project_service" "apis" {
  for_each = toset(["run.googleapis.com", "sqladmin.googleapis.com", "secretmanager.googleapis.com",
  "artifactregistry.googleapis.com", "monitoring.googleapis.com", "logging.googleapis.com", "iam.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "wwos" {
  location      = var.region
  repository_id = "wwos"
  format        = "DOCKER"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions { keep_count = 10 }
  }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- database
resource "google_sql_database_instance" "main" {
  name                = "wwos-pg"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true
  settings {
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    availability_type = var.db_high_availability ? "REGIONAL" : "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = var.db_disk_gb
    disk_autoresize   = true
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "02:00"
      transaction_log_retention_days = 7
      backup_retention_settings { retained_backups = 14 }
    }
    ip_configuration {
      ipv4_enabled = true # reachable only through the IAM-authorised Cloud SQL connector
      ssl_mode     = "ENCRYPTED_ONLY"
    }
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
    insights_config { query_insights_enabled = true }
    maintenance_window {
      day  = 7
      hour = 3
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "wwos" {
  name     = "wwos"
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "app" {
  name     = "wwos"
  instance = google_sql_database_instance.main.name
  # Password is read from Secret Manager at deploy time by the owner; never stored in state here.
  password = data.google_secret_manager_secret_version.db_password.secret_data
}

# ---------------------------------------------------------------- secrets (values added by the owner)
resource "google_secret_manager_secret" "s" {
  for_each  = toset(values(local.secrets))
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

data "google_secret_manager_secret_version" "db_password" {
  secret     = google_secret_manager_secret.s["wwos-db-password"].id
  depends_on = [google_secret_manager_secret.s]
}

# ---------------------------------------------------------------- identities (least privilege)
resource "google_service_account" "runtime" {
  account_id   = "wwos-runtime"
  display_name = "White Waters OS runtime (api, worker, migrations)"
}

resource "google_project_iam_member" "runtime_sql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "runtime_secrets" {
  for_each  = google_secret_manager_secret.s
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "runtime_logs" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

# ---------------------------------------------------------------- Cloud Run
resource "google_cloud_run_v2_service" "api" {
  name     = "wwos-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = var.api_min_instances
      max_instance_count = 3
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [local.db_conn] }
    }
    containers {
      image = local.image
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      startup_probe {
        http_get { path = "/health" }
      }
      liveness_probe {
        http_get { path = "/health" }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.runtime_secrets]
}

resource "google_cloud_run_v2_service" "worker" {
  name     = "wwos-worker"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = 1
      max_instance_count = 1 # one worker: horizontal scaling is safe (claim tokens, row-locked budgets) but unnecessary
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [local.db_conn] }
    }
    containers {
      image   = local.image
      command = ["wwos", "worker", "--health-port", "8080"]
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = false # instance-based billing: the worker runs continuously
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      startup_probe {
        http_get { path = "/healthz" }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.runtime_secrets]
}

resource "google_cloud_run_v2_job" "migrate" {
  name     = "wwos-migrate"
  location = var.region
  template {
    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      volumes {
        name = "cloudsql"
        cloud_sql_instance { instances = [local.db_conn] }
      }
      containers {
        image = local.image
        # The deploy workflow takes a Cloud SQL on-demand backup immediately before running this job.
        command = ["wwos", "db", "upgrade", "--backup-confirmed"]
        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name = "WWOS_DB_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = "wwos-db-password"
              version = "latest"
            }
          }
        }
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }
    }
  }
}

# Public dashboard/API access is through the service URL (or a domain mapping); the app does its
# own authentication, so unauthenticated invocation is allowed at the Cloud Run layer.
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------- monitoring
resource "google_monitoring_uptime_check_config" "api" {
  display_name = "wwos-api health"
  timeout      = "10s"
  period       = "300s"
  http_check {
    path    = "/health"
    use_ssl = true
  }
  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = trimprefix(var.public_origin, "https://")
    }
  }
}

resource "google_monitoring_notification_channel" "owner" {
  display_name = "Owner e-mail"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

resource "google_monitoring_alert_policy" "uptime" {
  display_name = "wwos-api is down"
  combiner     = "OR"
  conditions {
    display_name = "uptime check failing"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}

resource "google_logging_metric" "errors" {
  for_each = {
    qa_failed      = "\"QA scan:\" AND severity>=ERROR"
    task_dead      = "\"task.failed\" AND \"dead\""
    email_unknown  = "\"delivery outcome unknown\""
    budget_refused = "\"BudgetExceeded\""
  }
  name   = "wwos_${each.key}"
  filter = "resource.type=\"cloud_run_revision\" AND ${each.value}"
}

resource "google_monitoring_alert_policy" "log_alerts" {
  for_each     = google_logging_metric.errors
  display_name = "wwos ${each.key}"
  combiner     = "OR"
  conditions {
    display_name = each.key
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${each.value.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}

output "api_url" { value = google_cloud_run_v2_service.api.uri }
output "db_connection_name" { value = local.db_conn }
