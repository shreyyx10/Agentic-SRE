data "google_project" "this" {}

# APIs this stack uses (platform facts, not settings).
resource "google_project_service" "enabled" {
  for_each = toset([
    "cloudresourcemanager.googleapis.com", "compute.googleapis.com", "container.googleapis.com",
    "logging.googleapis.com", "monitoring.googleapis.com", "pubsub.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ------------------------------------------------------------ GKE Autopilot
resource "google_container_cluster" "shop" {
  # This cluster plays the customer's workload for the demo; it is not platform
  # infrastructure. Autopilot already enforces VPC-native networking, Workload
  # Identity (GKE metadata server), shielded nodes and no client certificates,
  # which checkov cannot see from this resource. Accepted for a demo cluster:
  # checkov:skip=CKV_GCP_12: Autopilot uses Dataplane V2 network policy enforcement
  # checkov:skip=CKV_GCP_13: Autopilot disables client certificate issuance
  # checkov:skip=CKV_GCP_23: Autopilot clusters are VPC-native (alias IPs)
  # checkov:skip=CKV_GCP_69: Autopilot enables Workload Identity and the GKE metadata server
  # checkov:skip=CKV_GCP_61: demo cluster; VPC flow logs not required
  # checkov:skip=CKV_GCP_64: demo cluster; public nodes keep image pulls and kubectl simple
  # checkov:skip=CKV_GCP_25: demo cluster; see CKV_GCP_64
  # checkov:skip=CKV_GCP_20: demo cluster operated from team laptops
  # checkov:skip=CKV_GCP_66: demo images are Google's public Online Boutique images
  # checkov:skip=CKV_GCP_65: demo cluster; team members are project IAM users
  name                = var.cluster_name
  location            = var.region
  enable_autopilot    = true
  deletion_protection = false # demo cluster: scale down or destroy after the finale

  release_channel {
    channel = var.release_channel
  }

  # Same labels as the provider default_labels; explicit here for the IaC scanner (CKV_GCP_21).
  resource_labels = var.labels

  depends_on = [google_project_service.enabled]
}

# ------------------------------------------- Read-only access for the platform
# The whole of what the platform may do in this project: read logs (including
# Admin Activity audit logs, which carry deployment history) and read metrics.
# Visible in IAM, audited in Cloud Audit Logs, revocable in one click.
resource "google_project_iam_member" "platform_reader" {
  for_each = toset(["roles/logging.viewer", "roles/monitoring.viewer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${var.reader_service_account}"
}

# ------------------------------------------------ Alert delivery to the platform
# Cloud Monitoring publishes incidents to the platform's per-tenant topic.
# Creating this channel provisions this project's Monitoring notification
# service account; then set monitoring_channel_ready = true in infra/platform.
resource "google_monitoring_notification_channel" "agentic_sre" {
  display_name = "Agentic SRE (${var.customer_id})"
  type         = "pubsub"
  labels = {
    topic = var.alert_topic_id
  }
  depends_on = [google_project_service.enabled]
}

# ------------------------------------------------------ Log-based metrics
# Field names are from Online Boutique v0.10.7 frontend request logs: logrus JSON,
# message "request complete", fields http.req.path, http.resp.status and
# http.resp.took_ms. The frontend logs at debug level by default, so every request is logged.
resource "google_logging_metric" "frontend_5xx" {
  name   = "frontend_5xx"
  filter = <<-EOT
    resource.type="k8s_container"
    labels."k8s-pod/app"="${var.frontend_app_label}"
    jsonPayload."http.resp.status">=500
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "checkout_latency_ms" {
  name            = "checkout_latency_ms"
  filter          = <<-EOT
    resource.type="k8s_container"
    labels."k8s-pod/app"="${var.frontend_app_label}"
    jsonPayload."http.req.path"="${var.checkout_path}"
  EOT
  value_extractor = "EXTRACT(jsonPayload.\"http.resp.took_ms\")"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "ms"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 32
      growth_factor      = 1.4
      scale              = 1
    }
  }
}

# ------------------------------------------------------------ Alert policies
# Policies carry user labels the platform reads: service (when the resource
# does not identify it), alert_type (explicit classification) and severity.
resource "google_monitoring_alert_policy" "container_restarts" {
  display_name = "Container restarts (Online Boutique)"
  combiner     = "OR"
  user_labels = {
    alert_type  = "container_crash"
    environment = var.environment
    severity    = var.alert_severity.container_restarts
  }

  conditions {
    display_name = "restart_count > ${var.restart_threshold} over 10 min"
    condition_threshold {
      filter          = "resource.type = \"k8s_container\" AND metric.type = \"kubernetes.io/container/restart_count\" AND resource.labels.namespace_name = \"${var.k8s_namespace}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.restart_threshold
      duration        = "0s"
      # Ten minutes, not one: a crash-looping container backs off up to 5 minutes between
      # restarts, and a one-minute window read those pauses as recovery (live, 12 Oct).
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_DELTA"
      }
      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "${var.alert_auto_close_seconds}s"
  }

  notification_channels = [google_monitoring_notification_channel.agentic_sre.id]

  documentation {
    content   = "A container in the storefront is restarting. Agentic SRE will post an Incident Brief to the incident thread."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "frontend_5xx" {
  display_name = "Storefront 5xx errors"
  combiner     = "OR"
  user_labels = {
    service     = var.frontend_app_label
    alert_type  = "error_rate"
    environment = var.environment
    severity    = var.alert_severity.frontend_5xx
  }

  conditions {
    display_name = "frontend 5xx > ${var.frontend_5xx_per_minute_threshold} per minute"
    condition_threshold {
      filter          = "resource.type = \"k8s_container\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.frontend_5xx.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.frontend_5xx_per_minute_threshold
      duration        = "60s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "${var.alert_auto_close_seconds}s"
  }

  notification_channels = [google_monitoring_notification_channel.agentic_sre.id]
}

resource "google_monitoring_alert_policy" "checkout_latency" {
  display_name = "Checkout latency p95"
  combiner     = "OR"
  user_labels = {
    service     = var.frontend_app_label
    alert_type  = "high_latency"
    environment = var.environment
    severity    = var.alert_severity.checkout_latency
  }

  conditions {
    display_name = "checkout p95 above threshold for 2 min"
    condition_threshold {
      filter          = "resource.type = \"k8s_container\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.checkout_latency_ms.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.checkout_latency_threshold_ms
      duration        = "120s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
      }
      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "${var.alert_auto_close_seconds}s"
  }

  notification_channels = [google_monitoring_notification_channel.agentic_sre.id]
}
