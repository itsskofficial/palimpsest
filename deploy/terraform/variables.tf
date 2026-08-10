###############################################################################
# Everything you can set. Only two have no default: `api_key` and `notion_token`.
###############################################################################

variable "project" {
  description = "Prefix for every resource name."
  type        = string
  default     = "palimpsest"
}

variable "environment" {
  description = "Environment name; part of every resource name and of PALIMPSEST_ENV."
  type        = string
  default     = "prod"
}

variable "region" {
  description = "AWS region. ap-south-1 (Mumbai) is the low-latency choice from Pune."
  type        = string
  default     = "ap-south-1"
}

###############################################################################
# the two you must supply
###############################################################################

variable "api_key" {
  description = <<-EOT
    Bearer token the review app requires. Generate one with:
      python -c "import secrets; print(secrets.token_urlsafe(32))"
    The app refuses to bind non-locally without it — this deployment puts your notes
    behind a public load balancer, so it is not optional.
  EOT
  type        = string
  sensitive   = true
}

variable "notion_token" {
  description = <<-EOT
    Notion internal-integration secret. https://www.notion.so/my-integrations

    Remember to share pages with the integration — it sees nothing until you do, and
    the symptom is an empty sync rather than an error.
  EOT
  type        = string
  sensitive   = true
}

###############################################################################
# the model and the ingestion adapters
###############################################################################

variable "anthropic_api_key" {
  description = <<-EOT
    Claude does claim extraction, relation classification and the contradiction sweep.

    Without it the deployment still mirrors Notion, serves the review app, and runs the
    duplicate / staleness / open-question sweeps — those are lexical and need no model.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "model" {
  description = "Claude model id."
  type        = string
  default     = "claude-opus-5"
}

variable "firecrawl_api_key" {
  description = "Better web extraction. Falls back to a stdlib HTML reader when unset."
  type        = string
  sensitive   = true
  default     = ""
}

variable "openai_api_key" {
  description = "Optional dense retrieval on top of the built-in lexical index."
  type        = string
  sensitive   = true
  default     = ""
}

###############################################################################
# permission to write — deliberately plain task-definition config, not a secret
###############################################################################

variable "apply" {
  description = <<-EOT
    Whether this deployment may write to Notion at all. Off by default.

    Kept as ordinary (non-secret) configuration on purpose: turning it on should be a
    visible change to a Terraform plan that someone reads, not a value edited inside a
    secret nobody looks at.
  EOT
  type        = bool
  default     = false
}

variable "autonomy" {
  description = <<-EOT
    The highest RISK TIER that may be applied without human review:
      none    review everything (default)
      low     `new` and `corroborates` apply automatically
      medium  also `refines`, `supersedes`, `duplicate`, `extends`

    There is deliberately no `high` — contradictions are never applied automatically,
    and the application rejects the value.
  EOT
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "low", "medium"], var.autonomy)
    error_message = "autonomy must be none, low or medium. There is no `high`: a contradiction is never applied automatically."
  }
}

variable "min_confidence" {
  description = "Below this classifier confidence, a judgement goes to review regardless."
  type        = number
  default     = 0.75
}

variable "notion_root_pages" {
  description = "Comma-separated page ids to restrict the mirror to. Empty means everything shared with the integration."
  type        = string
  default     = ""
}

variable "notion_version" {
  description = <<-EOT
    Notion API version. Pinned: the 2025-09-03 release split databases into data
    sources, so letting this float changes response shapes under a running deployment.
  EOT
  type        = string
  default     = "2026-03-11"
}

###############################################################################
# database — Supabase by default
###############################################################################

variable "database_url" {
  description = <<-EOT
    Supabase connection string for the **running service**: the transaction pooler,
    port 6543. It is IPv4 (the direct host is IPv6-only on the free tier, which a
    Fargate task in this stack cannot reach) and it survives connection churn.

    Dashboard -> Project Settings -> Database -> Connection string -> Transaction pooler.

    Leave empty and set supabase_project_ref + supabase_db_password instead.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "supabase_project_ref" {
  description = "Your Supabase project ref. With supabase_db_password, builds database_url."
  type        = string
  default     = ""
}

variable "supabase_db_password" {
  description = "Supabase database password. Dashboard -> Project Settings -> Database."
  type        = string
  sensitive   = true
  default     = ""
}

variable "supabase_region" {
  description = "Supabase pooler region, e.g. ap-south-1. Must match your project."
  type        = string
  default     = "ap-south-1"
}

variable "supabase_url" {
  description = "https://<ref>.supabase.co. Needed only if the archive goes to Supabase Storage."
  type        = string
  default     = ""
}

variable "supabase_service_role_key" {
  description = <<-EOT
    Supabase service-role key, for Storage. Bypasses RLS, so it is backend-only and
    never reaches a browser. Omit to keep the source archive in S3, the default here.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "artifacts_to_supabase" {
  description = <<-EOT
    Archive ingested sources to Supabase Storage instead of S3. S3 is the default
    because the bucket is created here and the task role already reaches it.
  EOT
  type        = bool
  default     = false
}

variable "artifacts_bucket" {
  description = "Supabase Storage bucket, when artifacts_to_supabase = true."
  type        = string
  default     = "palimpsest-archive"
}

variable "create_rds" {
  description = "Create an RDS Postgres instead of using Supabase. Adds ~$15/month."
  type        = bool
  default     = false
}

variable "rds_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

###############################################################################
# sizing — the defaults are deliberately small
###############################################################################

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "api_cpu" {
  description = "Fargate CPU units. 512 = 0.5 vCPU."
  type        = string
  default     = "512"
}

variable "api_memory" {
  description = "MiB. The mirror plus a BM25 index over a personal workspace fits in 1 GB."
  type        = string
  default     = "1024"
}

variable "api_count" {
  type    = number
  default = 1
}

variable "sync_cpu" {
  description = "The mirror sync is I/O-bound on Notion's ~3 req/s limit, not CPU-bound."
  type        = string
  default     = "512"
}

variable "sync_memory" {
  type    = string
  default = "1024"
}

variable "log_level" {
  type    = string
  default = "info"
}

###############################################################################
# access, observability and cost
###############################################################################

variable "allowed_cidrs" {
  description = <<-EOT
    Who may reach the load balancer. This deployment can read and edit your private
    notes, so narrowing it to your own IP is strongly recommended — the API key is the
    real control, but defence in depth is free here.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "ACM certificate ARN. Empty means HTTP only — fine for a demo, not for real notes."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "container_insights" {
  description = "Container Insights costs extra per metric. Off by default."
  type        = bool
  default     = false
}

variable "monthly_budget_usd" {
  description = <<-EOT
    A budget alarm, because AWS Activate credits do not stop the service when they
    expire — they start billing the card on file.
  EOT
  type        = number
  default     = 100
}

variable "budget_alert_email" {
  description = "Where budget alerts go. Empty disables the budget."
  type        = string
  default     = ""
}
