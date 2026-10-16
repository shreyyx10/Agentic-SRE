output "cluster" {
  value = google_container_cluster.shop.name
}

output "notification_channel" {
  value = google_monitoring_notification_channel.agentic_sre.id
}

output "monitoring_notification_service_account" {
  description = "Granted publish on the platform topic once infra/platform sets monitoring_channel_ready = true."
  value       = "service-${data.google_project.this.number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
}

output "project_number" {
  value = data.google_project.this.number
}
