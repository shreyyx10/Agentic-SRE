output "state_bucket" {
  value = google_storage_bucket.tfstate.name
}

output "image_repository" {
  description = "Set as the GitHub Actions variable GCP_IMAGE_REPOSITORY."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "deployer_service_account" {
  description = "Set as the GitHub Actions variable GCP_DEPLOYER_SA, and as deployer_service_account in infra/platform."
  value       = google_service_account.deployer.email
}

output "workload_identity_provider" {
  description = "Set as the GitHub Actions variable GCP_WIF_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}
