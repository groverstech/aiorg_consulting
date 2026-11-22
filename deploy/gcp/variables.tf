variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "image_tag" { type = string }
variable "public_origin" {
  type        = string
  description = "https:// URL the dashboard is reached at (custom domain or the Cloud Run URL)"
}
variable "alert_email" { type = string }
variable "llm_provider" {
  type    = string
  default = "offline" # set to "anthropic" only after the owner authorises AI spend
}
variable "org_monthly_ai_budget_usd" {
  type    = string
  default = "500"
}
variable "max_agent_budget_usd" {
  type    = string
  default = "100"
}
variable "smtp_host" {
  type    = string
  default = ""
}
variable "smtp_from" {
  type    = string
  default = ""
}
variable "smtp_user" {
  type    = string
  default = ""
}
variable "db_tier" {
  type    = string
  default = "db-custom-1-3840" # 1 dedicated vCPU, 3.75 GiB (SLA-covered); "db-g1-small" for the budget option
}
variable "db_disk_gb" {
  type    = number
  default = 10
}
variable "db_high_availability" {
  type    = bool
  default = false
}
variable "api_min_instances" {
  type    = number
  default = 0
}
