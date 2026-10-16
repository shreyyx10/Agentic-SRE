data "google_project" "this" {}

locals {
  # Google platform fact: the Pub/Sub service agent's email format.
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  # Names derived once from variables; everything below references these.
  ingest_name       = "${var.name_prefix}-ingest"
  analyse_name      = "${var.name_prefix}-analyse"
  tasks_invoker     = "${var.name_prefix}-tasks-invoker"
  ingest_aud        = "${var.name_prefix}-ingest"
  analyse_aud       = "${var.name_prefix}-analyse"
  queue_name        = "${var.name_prefix}-analyse"
  dead_letter_topic = "${var.alert_topic_prefix}-dead-letter"
  topic_name        = { for cid in keys(var.customers) : cid => "${var.alert_topic_prefix}-${cid}" }

  # Contract with asre.core.dedup: the collections whose documents carry expires_at.
  # ("causes": asre.analyse.pipeline cause correlation, one document per cited deployment change.)
  ttl_collections = ["alert_groups", "history", "causes"]

  # Contract with asre.core.logging: the structured-log "event" values the services emit.
  log_events = [
    "alerts_received", "groups_created", "dedup_uncertain", "ingest_rejected",
    "enqueue_failed", "brief_pending", "task_rejected",
  ]

  # Tenant configuration handed to the services: the profile (services, routing)
  # comes from config/tenants/<id>.json; identities come from the resources below.
  # No secrets — only project IDs and service-account emails.
  tenants = [
    for cid, c in var.customers : merge(
      jsondecode(file("${path.module}/../../config/tenants/${cid}.json")),
      {
        customer_id            = cid
        gcp_project_id         = c.gcp_project_id
        push_service_account   = google_service_account.push[cid].email
        subscription           = "projects/${var.project_id}/subscriptions/${local.topic_name[cid]}-push"
        reader_service_account = google_service_account.reader[cid].email
      }
    )
  ]
  tenants_json = jsonencode(local.tenants)

  # ASRE_SECRET_<ID> = projects/<p>/secrets/<id>; the code resolves the value at runtime.
  secret_env = {
    for id, s in google_secret_manager_secret.app :
    "ASRE_SECRET_${upper(replace(id, "-", "_"))}" => s.id
  }

  common_env = {
    GOOGLE_CLOUD_PROJECT    = var.project_id
    ASRE_FIRESTORE_DATABASE = google_firestore_database.main.name
    ASRE_ANALYSE_AUDIENCE   = local.analyse_aud
    ASRE_TASKS_INVOKER_SA   = google_service_account.tasks_invoker.email
    ASRE_TENANTS_JSON       = local.tenants_json
  }

  ingest_env = merge(local.common_env, {
    ASRE_SERVICE                   = "ingest"
    ASRE_THREADS                   = tostring(var.ingest_service.concurrency)
    ASRE_REQUEST_TIMEOUT_S         = tostring(var.ingest_service.timeout_seconds)
    ASRE_INGEST_AUDIENCE           = local.ingest_aud
    ASRE_TASKS_QUEUE               = google_cloud_tasks_queue.analyse.id
    ASRE_ANALYSE_URL               = "${google_cloud_run_v2_service.analyse.uri}/tasks/analyse"
    ASRE_DEDUP_WINDOW_MINUTES      = tostring(var.dedup.window_minutes)
    ASRE_DEDUP_UPDATE_INTERVAL_MIN = tostring(var.dedup.update_interval_minutes)
    ASRE_RETENTION_DAYS            = tostring(var.dedup.retention_days)
    ASRE_ENQUEUE_LEASE_S           = tostring(var.dedup.enqueue_lease_seconds)
    ASRE_DEDUP_REOPEN_MINUTES      = tostring(var.dedup.reopen_minutes)
    ASRE_DEDUP_TIMEOUT_MS          = tostring(var.dedup.dedup_timeout_ms)
    ASRE_ENQUEUE_TIMEOUT_MS        = tostring(var.dedup.enqueue_timeout_ms)
    ASRE_DEDUP_FIRST_TIMEOUT_MS    = tostring(var.dedup.dedup_first_timeout_ms)
    ASRE_ENQUEUE_FIRST_TIMEOUT_MS  = tostring(var.dedup.enqueue_first_timeout_ms)
    ASRE_TX_ATTEMPTS_PER_ROUND     = tostring(var.dedup.tx_attempts_per_round)
    ASRE_TX_CONTENTION_ROUNDS      = tostring(var.dedup.tx_contention_rounds)
    ASRE_TX_BASE_BACKOFF_MS        = tostring(var.dedup.tx_base_backoff_ms)
    ASRE_TX_MAX_BACKOFF_MS         = tostring(var.dedup.tx_max_backoff_ms)
  })

  analyse_env = merge(local.common_env, local.secret_env, {
    ASRE_SERVICE           = "analyse"
    ASRE_THREADS           = tostring(var.analyse_service.concurrency)
    ASRE_REQUEST_TIMEOUT_S = tostring(var.analyse_service.timeout_seconds)
    # Gemini models (ADR-0002): Pro for the hypothesis, falling back to Flash.
    ASRE_MODEL_LOCATION             = var.models.location
    ASRE_MODEL_HYPOTHESIS           = var.models.hypothesis
    ASRE_MODEL_HYPOTHESIS_FALLBACK  = var.models.hypothesis_fallback
    ASRE_MODEL_CRITIC               = var.models.critic
    ASRE_MODEL_HYPOTHESIS_THINKING  = var.models.hypothesis_thinking
    ASRE_MODEL_CRITIC_THINKING      = var.models.critic_thinking
    ASRE_MODEL_HYPOTHESIS_TIMEOUT_S = tostring(var.models.hypothesis_timeout_seconds)
    ASRE_MODEL_FALLBACK_TIMEOUT_S   = tostring(var.models.fallback_timeout_seconds)
    ASRE_MODEL_CRITIC_TIMEOUT_S     = tostring(var.models.critic_timeout_seconds)
    ASRE_MODEL_COOLDOWN_S           = tostring(var.models.cooldown_seconds)
    # Predictive mode de-duplicates forecasts like alerts, with the same settings as INGEST.
    ASRE_DEDUP_WINDOW_MINUTES      = tostring(var.dedup.window_minutes)
    ASRE_DEDUP_UPDATE_INTERVAL_MIN = tostring(var.dedup.update_interval_minutes)
    ASRE_RETENTION_DAYS            = tostring(var.dedup.retention_days)
    ASRE_ENQUEUE_LEASE_S           = tostring(var.dedup.enqueue_lease_seconds)
    ASRE_DEDUP_REOPEN_MINUTES      = tostring(var.dedup.reopen_minutes)
  })

}

# The hypothesis call, its fallback and the critic must all fit inside one ANALYSE
# request, with time left for evidence gathering and delivery.
check "model_timeouts_fit_the_request" {
  assert {
    condition = (
      var.models.hypothesis_timeout_seconds + var.models.fallback_timeout_seconds
      + var.models.critic_timeout_seconds
    ) < var.analyse_service.timeout_seconds - 60
    error_message = "models.*_timeout_seconds must sum to less than analyse_service.timeout_seconds - 60."
  }
}

# A redelivered alert must be able to take over an analysis whose enqueue failed,
# so Pub/Sub's minimum backoff has to outlast the enqueue lease.
check "backoff_outlasts_enqueue_lease" {
  assert {
    condition     = var.pubsub.min_backoff_seconds > var.dedup.enqueue_lease_seconds
    error_message = "pubsub.min_backoff_seconds must be greater than dedup.enqueue_lease_seconds."
  }
}

# A cold instance may spend both first-call budgets in one request; together they
# must fit inside INGEST's request timeout, with room for the rest of the request.
check "first_call_budgets_fit_ingest_timeout" {
  assert {
    condition     = (var.dedup.dedup_first_timeout_ms + var.dedup.enqueue_first_timeout_ms) / 1000 < var.ingest_service.timeout_seconds
    error_message = "dedup.dedup_first_timeout_ms + dedup.enqueue_first_timeout_ms must be less than ingest_service.timeout_seconds."
  }
}

# ------------------------------------------------------------------ Firestore
# Location is immutable after creation (ADR-0001). Deleting the database would
# delete every tenant's incident history, so it is abandoned, never destroyed.
resource "google_firestore_database" "main" {
  name                    = var.firestore_database
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  delete_protection_state = "DELETE_PROTECTION_ENABLED"
  deletion_policy         = "ABANDON"

  lifecycle {
    prevent_destroy = true
  }
}

# Storage reclamation ONLY. The customer-visible de-duplication window is
# dedup_until, checked exactly in code (P1-2). TTL deletion is best-effort
# (typically within 24 h), and it acts only on Timestamp values (P1-1).
resource "google_firestore_field" "ttl" {
  for_each   = toset(local.ttl_collections)
  database   = google_firestore_database.main.name
  collection = each.value
  field      = "expires_at"
  ttl_config {}
  index_config {} # exempt the TTL field from single-field indexing
}

# ----------------------------------------------------------- Service accounts
resource "google_service_account" "ingest" {
  account_id   = local.ingest_name
  display_name = "Agentic SRE ingest (Cloud Run)"
}

resource "google_service_account" "analyse" {
  account_id   = local.analyse_name
  display_name = "Agentic SRE analyse (Cloud Run)"
}

resource "google_service_account" "tasks_invoker" {
  account_id   = local.tasks_invoker
  display_name = "Identity Cloud Tasks uses to call ANALYSE"
}

# One push identity per tenant: INGEST binds the tenant to this identity.
resource "google_service_account" "push" {
  for_each     = var.customers
  account_id   = "${var.push_sa_prefix}-${each.key}"
  display_name = "Pub/Sub push identity for ${each.key}"
  lifecycle {
    precondition {
      condition     = length("${var.push_sa_prefix}-${each.key}") <= 30
      error_message = "push_sa_prefix + customer_id is longer than the 30-character service-account limit."
    }
  }
}

# One reader identity per tenant. The customer grants it logging.viewer and
# monitoring.viewer in THEIR project, and nothing else (read-only by construction).
resource "google_service_account" "reader" {
  for_each     = var.customers
  account_id   = "${var.reader_sa_prefix}-${each.key}"
  display_name = "Read-only access to ${each.key}'s project (customer-granted)"
  lifecycle {
    precondition {
      condition     = length("${var.reader_sa_prefix}-${each.key}") <= 30
      error_message = "reader_sa_prefix + customer_id is longer than the 30-character service-account limit."
    }
  }
}

# ----------------------------------------------------------------- Pub/Sub
# One topic per tenant, publishable only by that tenant's Cloud Monitoring.
# A shared topic could not tell tenants apart; per-tenant topics make the
# channel itself the proof of identity (ADR-0006).
resource "google_pubsub_topic" "alerts" {
  # checkov:skip=CKV_GCP_83: Google-managed encryption for the hackathon; CMEK is a post-MVP hardening item
  for_each                   = var.customers
  name                       = local.topic_name[each.key]
  message_retention_duration = "${var.pubsub.message_retention_seconds}s"
}

resource "google_pubsub_topic_iam_member" "monitoring_publisher" {
  for_each = { for cid, c in var.customers : cid => c if c.monitoring_channel_ready }
  topic    = google_pubsub_topic.alerts[each.key].name
  role     = "roles/pubsub.publisher"
  # Google platform fact: the customer project's Monitoring notification agent.
  member = "serviceAccount:service-${each.value.gcp_project_number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
}

resource "google_pubsub_topic" "dead_letter" {
  # checkov:skip=CKV_GCP_83: Google-managed encryption for the hackathon; CMEK is a post-MVP hardening item
  name                       = local.dead_letter_topic
  message_retention_duration = "${var.pubsub.message_retention_seconds}s"
}

# Keeps dead-lettered messages for inspection and drain (runbook, T3.11).
resource "google_pubsub_subscription" "dead_letter_hold" {
  name                       = "${local.dead_letter_topic}-hold"
  topic                      = google_pubsub_topic.dead_letter.id
  message_retention_duration = "${var.pubsub.message_retention_seconds}s"
  ack_deadline_seconds       = 60
  expiration_policy {
    ttl = ""
  }
}

resource "google_pubsub_subscription" "push" {
  for_each                   = var.customers
  name                       = "${local.topic_name[each.key]}-push"
  topic                      = google_pubsub_topic.alerts[each.key].id
  ack_deadline_seconds       = var.pubsub.ack_deadline_seconds
  message_retention_duration = "${var.pubsub.message_retention_seconds}s"

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.ingest.uri}/pubsub/${each.key}"
    oidc_token {
      service_account_email = google_service_account.push[each.key].email
      audience              = local.ingest_aud
    }
  }

  # Minimum backoff is longer than the dedup enqueue lease (see the check above),
  # so a redelivered alert can always take over an analysis that failed to enqueue.
  retry_policy {
    minimum_backoff = "${var.pubsub.min_backoff_seconds}s"
    maximum_backoff = "${var.pubsub.max_backoff_seconds}s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.pubsub.max_delivery_attempts
  }

  expiration_policy {
    ttl = ""
  }
}

# The Pub/Sub service agent mints push tokens and forwards to the dead-letter topic.
resource "google_service_account_iam_member" "pubsub_mints_push_tokens" {
  for_each           = var.customers
  service_account_id = google_service_account.push[each.key].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_agent
}

resource "google_pubsub_topic_iam_member" "pubsub_dead_letter_publisher" {
  topic  = google_pubsub_topic.dead_letter.name
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "pubsub_dead_letter_subscriber" {
  for_each     = var.customers
  subscription = google_pubsub_subscription.push[each.key].name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}

# -------------------------------------------------------------- Cloud Tasks
resource "google_cloud_tasks_queue" "analyse" {
  name     = local.queue_name
  location = var.region

  rate_limits {
    max_dispatches_per_second = var.tasks_queue.max_dispatches_per_second
    max_concurrent_dispatches = var.tasks_queue.max_concurrent_dispatches
  }

  retry_config {
    max_attempts  = var.tasks_queue.max_attempts
    min_backoff   = "${var.tasks_queue.min_backoff_seconds}s"
    max_backoff   = "${var.tasks_queue.max_backoff_seconds}s"
    max_doublings = var.tasks_queue.max_doublings
  }

  stackdriver_logging_config {
    sampling_ratio = var.tasks_queue.log_sampling_ratio
  }
}

# ----------------------------------------------------------- Secret Manager
# Containers only. Values are added out of band:
#   printf '%s' "$VALUE" | gcloud secrets versions add <id> --data-file=- --project <platform>
# so no secret value is ever in the repository, in tfvars or in Terraform state.
resource "google_secret_manager_secret" "app" {
  # checkov:skip=CKV_GCP_128: Google-managed encryption for the hackathon; CMEK is a post-MVP hardening item
  for_each  = var.secrets
  secret_id = each.key
  annotations = {
    purpose = each.value
  }
  replication {
    user_managed {
      replicas {
        location = var.region # data residency (ADR-0001)
      }
    }
  }
}

# ANALYSE may read exactly these secrets, nothing else in the project.
resource "google_secret_manager_secret_iam_member" "analyse_reads" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.analyse.email}"
}

# ---------------------------------------------------------------- Cloud Run
# Neither service has an allUsers binding. INGEST is invoked only by tenant push
# identities; ANALYSE only by the Cloud Tasks invoker identity.
resource "google_cloud_run_v2_service" "analyse" {
  name                = local.analyse_name
  location            = var.region
  ingress             = var.ingress
  custom_audiences    = [local.analyse_aud]
  deletion_protection = var.cloud_run_deletion_protection

  template {
    service_account                  = google_service_account.analyse.email
    timeout                          = "${var.analyse_service.timeout_seconds}s"
    max_instance_request_concurrency = var.analyse_service.concurrency

    scaling {
      min_instance_count = var.analyse_service.min_instances
      max_instance_count = var.analyse_service.max_instances
    }

    containers {
      image = var.analyse_image

      dynamic "env" {
        for_each = local.analyse_env
        content {
          name  = env.key
          value = env.value
        }
      }

      resources {
        limits = {
          cpu    = var.analyse_service.cpu
          memory = var.analyse_service.memory
        }
      }

      startup_probe {
        http_get {
          path = "/health"
        }
      }
      liveness_probe {
        http_get {
          path = "/health"
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }

  depends_on = [google_firestore_database.main, google_secret_manager_secret_iam_member.analyse_reads]
}

resource "google_cloud_run_v2_service" "ingest" {
  name                = local.ingest_name
  location            = var.region
  ingress             = var.ingress
  custom_audiences    = [local.ingest_aud]
  deletion_protection = var.cloud_run_deletion_protection

  template {
    service_account = google_service_account.ingest.email
    timeout         = "${var.ingest_service.timeout_seconds}s"
    # Admit no more requests than the process can run at once (P1-7); the same
    # value sets the Gunicorn thread count (ASRE_THREADS).
    max_instance_request_concurrency = var.ingest_service.concurrency

    scaling {
      min_instance_count = var.ingest_service.min_instances
      max_instance_count = var.ingest_service.max_instances
    }

    containers {
      image = var.ingest_image

      dynamic "env" {
        for_each = local.ingest_env
        content {
          name  = env.key
          value = env.value
        }
      }

      resources {
        limits = {
          cpu    = var.ingest_service.cpu
          memory = var.ingest_service.memory
        }
      }

      startup_probe {
        http_get {
          path = "/health"
        }
      }
      liveness_probe {
        http_get {
          path = "/health"
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }

  depends_on = [google_firestore_database.main]
}

resource "google_cloud_run_v2_service_iam_member" "ingest_invoker" {
  for_each = var.customers
  name     = google_cloud_run_v2_service.ingest.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.push[each.key].email}"
}

resource "google_cloud_run_v2_service_iam_member" "analyse_invoker" {
  name     = google_cloud_run_v2_service.analyse.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tasks_invoker.email}"
}

# ---------------------------------------------------------------------- IAM
# Scoped to resources wherever GCP allows it (P1-10). Firestore IAM is
# project-level only; tenant isolation there is by document path, in code.
resource "google_project_iam_member" "ingest_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_cloud_tasks_queue_iam_member" "ingest_enqueuer" {
  name     = google_cloud_tasks_queue.analyse.name
  location = var.region
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.ingest.email}"
}

# Creating an OIDC task "as" the invoker requires actAs on that one account.
resource "google_service_account_iam_member" "ingest_acts_as_invoker" {
  service_account_id = google_service_account.tasks_invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_project_iam_member" "analyse_roles" {
  for_each = toset(["roles/datastore.user", "roles/aiplatform.user", "roles/discoveryengine.viewer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.analyse.email}"
}

# ANALYSE reads customer telemetry by impersonating that customer's reader.
resource "google_service_account_iam_member" "analyse_impersonates_reader" {
  for_each           = var.customers
  service_account_id = google_service_account.reader[each.key].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.analyse.email}"
}

# The CI identity may roll revisions of the services in output run_services and
# nothing else. A new service needs an entry in both maps below.
resource "google_cloud_run_v2_service_iam_member" "deployer" {
  for_each = merge(
    {
      ingest  = google_cloud_run_v2_service.ingest.name
      analyse = google_cloud_run_v2_service.analyse.name
    },
  )
  name     = each.value
  location = var.region
  role     = "roles/run.developer"
  member   = "serviceAccount:${var.deployer_service_account}"
}

resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  for_each = merge(
    {
      ingest  = google_service_account.ingest.name
      analyse = google_service_account.analyse.name
    },
  )
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account}"
}

# ------------------------------------------------------ Log-based metrics
# Each counts one structured-log event emitted by the services (asre.core.logging).
# SLOs and dashboards are built on these.
resource "google_logging_metric" "events" {
  for_each = toset(local.log_events)
  name     = "${var.name_prefix}/${each.value}"
  filter   = "resource.type=\"cloud_run_revision\" AND jsonPayload.event=\"${each.value}\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

# ------------------------------------------------------------------ Budget
resource "google_billing_budget" "platform" {
  count           = var.billing_account == "" ? 0 : 1
  billing_account = var.billing_account
  display_name    = "Agentic SRE platform (${var.project_id})"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.monthly_budget)
    }
  }

  dynamic "threshold_rules" {
    for_each = var.budget_alert_thresholds
    content {
      threshold_percent = threshold_rules.value
    }
  }
}

# ---------------------------------------------------------- Predictive mode
# Every few minutes Cloud Scheduler asks ANALYSE to scan each tenant's namespace
# for a trend heading past a limit (asre.predict.forecast). It calls as the tasks
# invoker, which ANALYSE already authenticates, so no new identity is needed.
resource "google_cloud_scheduler_job" "predictive_scan" {
  count            = var.predictive.enabled ? 1 : 0
  name             = "${var.name_prefix}-predictive-scan"
  region           = var.region
  description      = "Agentic SRE predictive mode: forecast each tenant's services"
  schedule         = var.predictive.schedule
  time_zone        = "Etc/UTC"
  attempt_deadline = "${var.analyse_service.timeout_seconds}s"

  retry_config {
    retry_count = 0 # the next scan is a few minutes away
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.analyse.uri}/tasks/scan"
    oidc_token {
      service_account_email = google_service_account.tasks_invoker.email
      audience              = local.analyse_aud
    }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.analyse_invoker]
}
