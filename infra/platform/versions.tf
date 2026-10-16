terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.4"
    }
  }
  # Partial config, completed at init time (nothing environment-specific in the repo):
  #   terraform init -backend-config="bucket=$STATE_BUCKET"
  backend "gcs" {
    prefix = "platform"
  }
}

provider "google" {
  project        = var.project_id
  region         = var.region
  default_labels = var.labels
  # Charge API quota to the platform project instead of whatever quota project
  # the operator's local credentials default to. The Budgets API is enabled here
  # (infra/bootstrap), not necessarily in that default project.
  user_project_override = true
  billing_project       = var.project_id
}
