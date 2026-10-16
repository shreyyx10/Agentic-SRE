variable "project_id" {
  description = "Platform project ID."
  type        = string
}

variable "region" {
  description = "Region for Firestore, Artifact Registry and the state bucket. ONE-WAY DOOR for Firestore — see ADR-0001."
  type        = string
}

variable "state_bucket_name" {
  description = "Globally unique name for the Terraform state bucket used by infra/platform and infra/example_customer."
  type        = string
}

variable "github_repository" {
  description = "owner/repo of the hackathon repository, used to scope the CI deploy identity."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must look like owner/repo."
  }
}

variable "deploy_branch" {
  description = "The only branch whose workflows may impersonate the deployer (must match the CI deploy job's branch)."
  type        = string
  default     = "main"
}

variable "name_prefix" {
  description = "Prefix for every named resource this stack creates (service accounts, repository). Must match infra/platform."
  type        = string
  default     = "asre"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,10}$", var.name_prefix))
    error_message = "name_prefix must be 2-11 characters of lowercase letters, digits and '-' (it is embedded in service-account IDs)."
  }
}

variable "image_repository_id" {
  description = "Artifact Registry repository ID for the service images."
  type        = string
  default     = "agentic-sre"
}

variable "wif_pool_id" {
  description = "Workload Identity pool ID for GitHub Actions. Pool IDs are soft-deleted for 30 days, so pick a new one if you recreate it."
  type        = string
  default     = "github"
}

variable "wif_provider_id" {
  description = "Workload Identity provider ID inside the pool."
  type        = string
  default     = "github-oidc"
}

variable "labels" {
  description = "Labels applied to every labelable resource (cost reporting, ownership)."
  type        = map(string)
  default = {
    app         = "agentic-sre"
    environment = "hackathon"
    managed-by  = "terraform"
  }
}
