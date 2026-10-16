terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.4"
    }
  }
  # Partial config: terraform init -backend-config="bucket=$STATE_BUCKET"
  backend "gcs" {
    prefix = "example-customer"
  }
}

provider "google" {
  project        = var.project_id
  region         = var.region
  default_labels = var.labels
  # Charge API quota to this project, not the operator's default quota project
  # (same reason as infra/platform).
  user_project_override = true
  billing_project       = var.project_id
}
