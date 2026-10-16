output "ingest_url" {
  value = google_cloud_run_v2_service.ingest.uri
}

output "analyse_url" {
  value = google_cloud_run_v2_service.analyse.uri
}

output "run_services" {
  description = "Set as the GitHub Actions variable GCP_RUN_SERVICES (deploy order: analyse first)."
  value = join(" ", concat(
    [
      google_cloud_run_v2_service.analyse.name,
      google_cloud_run_v2_service.ingest.name,
    ],
  ))
}

output "ingest_service" {
  value = google_cloud_run_v2_service.ingest.name
}

output "tasks_queue" {
  value = google_cloud_tasks_queue.analyse.name
}

output "firestore_database" {
  value = google_firestore_database.main.name
}

output "tenant_topics" {
  description = "Per-tenant topic ID; pass to infra/example_customer as alert_topic_id."
  value       = { for cid, t in google_pubsub_topic.alerts : cid => t.id }
}

output "tenant_reader_service_accounts" {
  description = "Grant these logging.viewer + monitoring.viewer in the customer project (and nothing else)."
  value       = { for cid, sa in google_service_account.reader : cid => sa.email }
}

output "secrets" {
  description = "Secret Manager secrets ANALYSE can read. Add values with: gcloud secrets versions add <id> --data-file=-"
  value       = { for id, s in google_secret_manager_secret.app : id => s.id }
}
