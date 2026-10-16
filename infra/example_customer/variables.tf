# The CUSTOMER side, as an example: a GKE cluster to try the agent on (Google's public
# Online Boutique sample), the read-only grant for the platform's reader identity,
# the Pub/Sub notification channel, and three alert policies. A real customer runs the
# equivalent in their own project.

# ------------------------------------------------------------ Environment
variable "project_id" {
  description = "Customer project ID."
  type        = string
}

variable "region" {
  description = "Region for the GKE Autopilot cluster."
  type        = string
}

variable "customer_id" {
  description = "This customer's ID on the platform (matches infra/platform customers key)."
  type        = string
}

variable "alert_topic_id" {
  description = "The platform topic this customer's Cloud Monitoring publishes to (infra/platform output tenant_topics[customer_id])."
  type        = string
  validation {
    condition     = can(regex("^projects/[a-z][a-z0-9-]+/topics/[A-Za-z0-9._~+%-]+$", var.alert_topic_id))
    error_message = "alert_topic_id must be a full topic ID: projects/<project>/topics/<topic>."
  }
}

variable "reader_service_account" {
  description = "Platform reader identity for this customer (infra/platform output tenant_reader_service_accounts[customer_id])."
  type        = string
}

variable "labels" {
  description = "Labels applied to every labelable resource."
  type        = map(string)
  default = {
    app        = "online-boutique"
    purpose    = "agentic-sre-demo"
    managed-by = "terraform"
  }
}

# ------------------------------------------------------------ Cluster
variable "cluster_name" {
  type    = string
  default = "shop"
}

variable "release_channel" {
  type    = string
  default = "REGULAR"
}

variable "k8s_namespace" {
  description = "Namespace Online Boutique runs in (where your app runs)."
  type        = string
  default     = "default"
}

# ------------------------------------------------------------ Workload facts
# Online Boutique v0.10.7: the storefront's pod label and its checkout route.
variable "frontend_app_label" {
  type    = string
  default = "frontend"
}

variable "checkout_path" {
  type    = string
  default = "/cart/checkout"
}

# ------------------------------------------------------------ Alerting
variable "environment" {
  description = "Environment label on every alert policy (becomes part of the platform's de-duplication key)."
  type        = string
  default     = "prod"
}

variable "restart_threshold" {
  description = "Alert when a container restarts more than this many times per minute."
  type        = number
  default     = 0
}

variable "frontend_5xx_per_minute_threshold" {
  description = "Alert when storefront 5xx responses exceed this per minute (demo load gives ~5 checkouts/min; see make loadgen-demo)."
  type        = number
  default     = 2
}

variable "checkout_latency_threshold_ms" {
  description = "Alert when checkout p95 exceeds this for 2 minutes."
  type        = number
  default     = 1500
}

variable "alert_auto_close_seconds" {
  type    = number
  default = 1800
}

variable "alert_severity" {
  description = "severity user label per policy."
  type = object({
    container_restarts = optional(string, "critical")
    frontend_5xx       = optional(string, "critical")
    checkout_latency   = optional(string, "warning")
  })
  default = {}
}
