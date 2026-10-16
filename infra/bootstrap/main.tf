# Run once per platform project, by a human, before anything else.
# Owns the things the pipeline needs to exist before it can run: APIs, the
# state bucket, the image repository and the low-privilege deploy identity.
# No secret VALUES are created here or anywhere else in Terraform: Gemini on
# Vertex AI is reached with IAM (ADC), so there is no model API key (ADR-0002).
# Secret Manager is enabled for the few credentials that cannot be IAM (see
# infra/platform: secrets); their values are added by a human, never via Terraform.
#
# The API list below is what the code and the other stacks use: platform facts,
# not settings, so it is a local rather than a variable.

locals {
  services = [
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtasks.googleapis.com",
    "discoveryengine.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sts.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "tfstate" {
  # checkov:skip=CKV_GCP_62: access is audited via Cloud Audit Logs (Data Access) instead of bucket access logs
  name                        = var.state_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_artifact_registry_repository" "images" {
  # checkov:skip=CKV_GCP_84: Google-managed encryption for the hackathon; CMEK is a post-MVP hardening item
  repository_id = var.image_repository_id
  location      = var.region
  format        = "DOCKER"
  description   = "Agentic SRE service images. Deployed by digest, never by tag."

  docker_config {
    immutable_tags = true
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.enabled]
}

# The CI identity. It can push images and roll Cloud Run revisions (granted
# per-service in infra/platform). It cannot change IAM, Firestore or Pub/Sub:
# those changes are applied by a human.
resource "google_service_account" "deployer" {
  account_id   = "${var.name_prefix}-deployer"
  display_name = "Agentic SRE CI deployer (image push + revision deploy only)"
  depends_on   = [google_project_service.enabled]
}

resource "google_artifact_registry_repository_iam_member" "deployer_writer" {
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

# GitHub Actions -> Workload Identity Federation -> deployer. No JSON keys.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = var.wif_pool_id
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  # checkov:skip=CKV_GCP_125: attribute_condition pins one repository AND one branch (values supplied by variables, which the check cannot resolve)
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = var.wif_provider_id
  display_name                       = "GitHub OIDC"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  # Only this repository's deploy branch can deploy.
  attribute_condition = "assertion.repository == \"${var.github_repository}\" && assertion.ref == \"refs/heads/${var.deploy_branch}\""
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com" # GitHub's fixed OIDC issuer (a platform fact)
  }
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}
