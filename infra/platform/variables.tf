# Every environment-specific value and every operational tuning knob is a
# variable. What stays literal in main.tf is either a Google platform fact (API
# and role names, service-agent email formats) or a contract with the Python
# code (environment-variable names, Firestore collection names, log event names),
# each of which is defined in exactly one place.

# ------------------------------------------------------------ Environment
variable "project_id" {
  description = "Platform project ID."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud Tasks and Firestore. ONE-WAY DOOR for Firestore — see ADR-0001."
  type        = string
}

variable "deployer_service_account" {
  description = "Email of the CI deploy identity (infra/bootstrap output deployer_service_account)."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for platform resource names (services, service accounts, queue, metrics, audiences). Must match infra/bootstrap."
  type        = string
  default     = "asre"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,10}$", var.name_prefix))
    error_message = "name_prefix must be 2-11 characters of lowercase letters, digits and '-'."
  }
}

variable "labels" {
  description = "Labels applied to every labelable resource."
  type        = map(string)
  default = {
    app         = "agentic-sre"
    environment = "hackathon"
    managed-by  = "terraform"
  }
}

variable "customers" {
  description = <<-EOT
    Tenants, keyed by customer_id (also the name of config/tenants/<customer_id>.json).
    Set monitoring_channel_ready = true only after the customer's Pub/Sub notification
    channel exists (creating it provisions the Monitoring notification service account
    that must be granted publish rights on the tenant's topic).
  EOT
  type = map(object({
    gcp_project_id           = string
    gcp_project_number       = string
    monitoring_channel_ready = optional(bool, false)
  }))
  validation {
    condition     = alltrue([for cid in keys(var.customers) : can(regex("^[a-z][a-z0-9-]{2,20}$", cid))])
    error_message = "customer_id must match ^[a-z][a-z0-9-]{2,20}$ (it is embedded in service-account IDs)."
  }
}

# ------------------------------------------------------------ Naming conventions
variable "alert_topic_prefix" {
  description = "Tenant topics are <prefix>-<customer_id>; the dead-letter topic is <prefix>-dead-letter."
  type        = string
  default     = "alerts"
}

variable "push_sa_prefix" {
  description = "Per-tenant Pub/Sub push identities are <prefix>-<customer_id>."
  type        = string
  default     = "push"
}

variable "reader_sa_prefix" {
  description = "Per-tenant read-only identities (granted by the customer) are <prefix>-<customer_id>."
  type        = string
  default     = "cust"
}

# ------------------------------------------------------------ Images
variable "ingest_image" {
  description = "Initial image for INGEST. The default placeholder (Google's hello container) lets the first apply succeed before CI has built the real image; CI then deploys by digest and Terraform ignores image changes."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "analyse_image" {
  description = "Initial image for ANALYSE (see ingest_image)."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

# ------------------------------------------------------------ Cloud Run
variable "ingress" {
  description = "Cloud Run ingress for both services. Pub/Sub push and Cloud Tasks in the same project count as internal traffic."
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"
}

variable "cloud_run_deletion_protection" {
  type    = bool
  default = false
}

variable "ingest_service" {
  description = "INGEST sizing. concurrency is also the Gunicorn thread count; timeout_seconds also sets Gunicorn's (longer) worker timeout."
  type = object({
    cpu             = optional(string, "1")
    memory          = optional(string, "512Mi")
    timeout_seconds = optional(number, 10)
    concurrency     = optional(number, 8)
    min_instances   = optional(number, 0)
    max_instances   = optional(number, 10)
  })
  default = {}
}

variable "analyse_service" {
  description = "ANALYSE sizing (see ingest_service)."
  type = object({
    cpu             = optional(string, "1")
    memory          = optional(string, "1Gi")
    timeout_seconds = optional(number, 300)
    concurrency     = optional(number, 4)
    min_instances   = optional(number, 0)
    max_instances   = optional(number, 5)
  })
  default = {}
}

# ------------------------------------------------------------ Firestore + dedup
variable "firestore_database" {
  description = "Firestore database ID."
  type        = string
  default     = "(default)"
}

variable "dedup" {
  description = <<-EOT
    De-duplication behaviour, passed to INGEST as environment variables.
    window_minutes: repeats inside this window join the open incident.
    update_interval_minutes: at most one Brief update per interval while it keeps firing.
    reopen_minutes: an alert that fires again this soon after its resolution reopens the
    same incident (crash loops resolve and re-fire); 0 turns this off.
    retention_days: storage TTL (expires_at) for groups and history.
    enqueue_lease_seconds: must be SHORTER than pubsub.min_backoff_seconds.
    dedup_timeout_ms / enqueue_timeout_ms: budgets inside the INGEST request.
    dedup_first_timeout_ms / enqueue_first_timeout_ms: the same budgets for the first
    call from a new instance, which also opens the connection and fetches a token.
    tx_*: alert-storm handling. A group transaction that keeps losing to concurrent
    writers is re-run for up to tx_contention_rounds rounds (tx_attempts_per_round
    commits each), sleeping a random time up to min(tx_max_backoff_ms,
    tx_base_backoff_ms * 2^round) between rounds.
  EOT
  type = object({
    window_minutes           = optional(number, 60)
    update_interval_minutes  = optional(number, 15)
    reopen_minutes           = optional(number, 10)
    retention_days           = optional(number, 30)
    enqueue_lease_seconds    = optional(number, 20)
    dedup_timeout_ms         = optional(number, 500)
    enqueue_timeout_ms       = optional(number, 500)
    dedup_first_timeout_ms   = optional(number, 3000)
    enqueue_first_timeout_ms = optional(number, 5000)
    tx_attempts_per_round    = optional(number, 5)
    tx_contention_rounds     = optional(number, 8)
    tx_base_backoff_ms       = optional(number, 25)
    tx_max_backoff_ms        = optional(number, 1000)
  })
  default = {}
}

# ------------------------------------------------------------ Gemini models
variable "models" {
  description = <<-EOT
    Gemini models for ANALYSE (ADR-0002), passed as ASRE_MODEL_* environment variables.
    location: Vertex AI location; "global" per ADR-0001. "australia-southeast1" keeps
    inference in Sydney but serves only gemini-3.5-flash among Gemini 3.x (option C).
    hypothesis: the Pro model that forms the hypothesis.
    hypothesis_fallback: answers when the hypothesis model is missing, out of quota,
    overloaded or slow. "" disables the fallback.
    critic: the model that checks the hypothesis.
    *_thinking: minimal | low | medium | high.
    *_timeout_seconds: per call; the three must fit inside analyse_service.timeout_seconds.
    cooldown_seconds: how long a hypothesis model that returned 404 or 429 is skipped.
  EOT
  type = object({
    location                   = optional(string, "global")
    hypothesis                 = optional(string, "gemini-3.1-pro-preview")
    hypothesis_fallback        = optional(string, "gemini-3.8-flash")
    critic                     = optional(string, "gemini-3.8-flash")
    hypothesis_thinking        = optional(string, "medium")
    critic_thinking            = optional(string, "low")
    hypothesis_timeout_seconds = optional(number, 60)
    fallback_timeout_seconds   = optional(number, 45)
    critic_timeout_seconds     = optional(number, 30)
    cooldown_seconds           = optional(number, 600)
  })
  default = {}

  validation {
    condition = alltrue([
      for t in [var.models.hypothesis_thinking, var.models.critic_thinking] :
      contains(["minimal", "low", "medium", "high"], t)
    ])
    error_message = "models.*_thinking must be one of minimal, low, medium, high."
  }
}

# ------------------------------------------------------------ Pub/Sub
variable "pubsub" {
  description = "Tenant topic and push subscription behaviour."
  type = object({
    message_retention_seconds = optional(number, 604800)
    ack_deadline_seconds      = optional(number, 20)
    min_backoff_seconds       = optional(number, 30)
    max_backoff_seconds       = optional(number, 600)
    max_delivery_attempts     = optional(number, 10)
  })
  default = {}
}

# ------------------------------------------------------------ Cloud Tasks
variable "tasks_queue" {
  description = "The analyse queue: dispatch rate and retry policy."
  type = object({
    max_dispatches_per_second = optional(number, 5)
    max_concurrent_dispatches = optional(number, 10)
    max_attempts              = optional(number, 5)
    min_backoff_seconds       = optional(number, 5)
    max_backoff_seconds       = optional(number, 300)
    max_doublings             = optional(number, 4)
    log_sampling_ratio        = optional(number, 1.0)
  })
  default = {}
}

# ------------------------------------------------------------ Secret Manager
variable "secrets" {
  description = <<-EOT
    Secret Manager secrets ANALYSE may read, keyed by secret ID, value = purpose.
    Terraform creates the empty secret and the IAM grant only. The VALUE is added
    by a human with `gcloud secrets versions add` and never passes through
    Terraform, tfvars or state. Each ID is exposed to ANALYSE as
    ASRE_SECRET_<ID in UPPER_SNAKE_CASE> = the secret's resource name.
  EOT
  type        = map(string)
  default = {
    "chat-webhook-url" = "Google Chat incoming-webhook URL for Brief delivery (week 2, T4.9)"
  }
  validation {
    condition     = alltrue([for id in keys(var.secrets) : can(regex("^[a-z][a-z0-9-]{1,60}$", id))])
    error_message = "secret IDs must be lowercase letters, digits and '-'."
  }
}

# ------------------------------------------------------------ Budget
variable "billing_account" {
  description = "Billing account ID for the spend alert (without the billingAccounts/ prefix). Empty disables the budget."
  type        = string
  default     = ""
}

variable "monthly_budget" {
  description = "Monthly budget amount, in budget_currency."
  type        = number
  default     = 300
}

variable "budget_currency" {
  description = "Must match the billing account's currency."
  type        = string
  default     = "AUD"
}

variable "budget_alert_thresholds" {
  description = "Fractions of the budget at which billing admins are emailed."
  type        = list(number)
  default     = [0.5, 0.9, 1.0]
}

variable "predictive" {
  description = <<-EOT
    Predictive mode: how often Cloud Scheduler runs the forecast scan (cron, UTC).
    Each new forecast becomes a Brief; a trend that persists stays one incident.
  EOT
  type = object({
    enabled  = optional(bool, true)
    schedule = optional(string, "*/5 * * * *")
  })
  default = {}
}
