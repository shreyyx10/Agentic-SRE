# ADR-0007 — Configuration and secrets: nothing environment-specific in the repository, secret values only in Secret Manager

- **Status**: Accepted — 28 Sep 2026
- **Deciders**: Product Owner (set the three rules before the first `terraform apply`), Engineering Lead (implementation)

## Context
Before anything was applied to GCP, the Product Owner set three rules:
1. every value is parameterised and nothing is hard-coded;
2. no secrets in the codebase, and any secret lives in Secret Manager;
3. environment files and variables stay outside source control.

An audit of the first version found project IDs, resource names, timeouts, retry policies and thresholds written into Terraform, CI, the Makefiles and the scripts. It also found region defaults repeated in several places, and the Gunicorn thread count and timeouts copied by hand from the Cloud Run settings. Terraform variable files and the bootstrap state were git-ignored but lived inside the repository folder. Four secret-shaped test fixtures would also have failed the CI secret scan.

## Options
- **A. Variables everywhere, settings folder outside the repository, Secret Manager for the few real secrets (chosen).** *Against*: more variables to document; operators must keep a settings folder.
- **B. `.tfvars` and `.env` files git-ignored inside the repository.** Simpler. *Against*: a single `git add -f` or a changed ignore rule leaks environment values; the rule "outside source control" is only as strong as `.gitignore`.
- **C. Secrets as Terraform-managed secret versions or Cloud Run secret environment variables.** *Against*: the value passes through Terraform variables and state, or sits in the service's configuration.

## Decision
- **Parameterise what varies.** Every environment-specific value and every operational tuning value is a Terraform variable, an environment variable or a GitHub repository variable. Variables have documented defaults in `infra/*/variables.tf`. What stays literal is one of three things:
  - a **Google platform fact**, such as an API name, an IAM role or a service-agent email format;
  - a **contract with the code**, such as environment-variable names, Firestore collection names or log event names, each defined in exactly one place;
  - a **pinned tool version**.
- **Settings live next to the repository, never in it.** The settings folder (`$ASRE_HOME`) holds `asre.env`, one `.tfvars` file per stack, and the bootstrap state. For the hackathon it is the `.asre` folder beside the repository clone. The repository contains only `*.example` templates with placeholders. `.gitignore` is a second line of defence.
- **Secret values exist only in Secret Manager.**
  - Terraform creates the empty secret (replicated in the platform region) and grants `secretAccessor` on it to ANALYSE only.
  - A human adds the value with `gcloud secrets versions add`, reading it with `read -rs` so it never reaches shell history.
  - The service receives only the secret's name (`ASRE_SECRET_<ID>`) and reads the latest version at runtime with its own identity, cached for five minutes (`asre.core.secrets`).
- **Credentials are identities, not keys.** CI uses Workload Identity Federation (ADR-0008). The services use their Cloud Run identities. Gemini is reached through IAM (ADR-0002). Customer data is read by impersonating a per-tenant reader identity (ADR-0006).
- **Test fixtures that look like secrets are assembled at runtime from fragments.** The CI secret scan then runs without an allow-list that could hide a real leak.
- **API quota is charged to the stack's own project.** Each provider sets `billing_project` with `user_project_override = true`, so a stack never depends on the operator's default quota project.

## Consequences
- **A settings file is required.** A new environment needs a settings folder, created in the README's Deploy section Commands must quote `"$ASRE_HOME/..."`, because the folder path can contain spaces.
- **The state files hold no secret material**, because no key, token or secret version is ever created by Terraform.
- **Every secret costs one Terraform change and one human step.** Adding a secret means adding its ID to the `secrets` variable, applying, and then adding the value by hand.
- **Verified 29 Sep 2026**:
  - `gitleaks` reports no leaks in the working tree or history;
  - a scratch-repository test confirmed that `.tfvars`, `.env`, state and key files are never staged;
  - a scan found no environment-specific values outside the documentation and templates.
