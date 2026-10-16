terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.4"
    }
  }
  # Bootstrap creates the remote-state bucket, so its own (small) state is local.
  # The path is supplied at init time and lives OUTSIDE the repository:
  #   terraform init -backend-config="path=$ASRE_HOME/bootstrap.tfstate"
  backend "local" {}
}

provider "google" {
  project        = var.project_id
  region         = var.region
  default_labels = var.labels
}
