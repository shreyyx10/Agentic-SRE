# ADR-0008 — Keyless CI deployment by image digest

- **Status**: Proposed — implemented and verified live 29 Sep 2026
- **Deciders**: Engineering Lead

## Context
A design review found a deploy path that used the `latest` tag, and a pipeline identity that could change far more than it needed to. A downloaded service-account key would also break ADR-0007: it would be a long-lived secret sitting in GitHub.

## Options
- **A. GitHub Actions with Workload Identity Federation, deploy by digest, revisions only (chosen).** *Against*: the identity pool's trust condition must match the repository name and branch exactly; a rename needs a re-apply.
- **B. Cloud Build triggers.** Keyless as well. *Against*: a second CI system next to the GitHub checks reviewers see; less visible in the public repository.
- **C. A service-account key stored as a GitHub secret.** *Against*: a long-lived credential, which ADR-0007 rules out.

## Decision
- **One identity pool, one trust condition.** Bootstrap creates a Workload Identity pool and provider whose `attribute_condition` accepts exactly one repository and one branch (`github_repository`, `deploy_branch`). Only that principal set may impersonate the deployer service account.
- **The deployer holds a deliberately narrow set of rights:**
  - `artifactregistry.writer` on the one image repository;
  - `run.developer` on each Cloud Run service it deploys (ANALYSE and INGEST);
  - `iam.serviceAccountUser` on each of their runtime identities.

  It cannot change IAM, Firestore, Pub/Sub or the queue. Those changes are made by a human with `terraform apply`.
- **Build once, deploy by digest.** CI tags the image with the commit SHA (tags are immutable in the repository) and resolves the digest. It rolls each service in `GCP_RUN_SERVICES`, ANALYSE first, to `image@sha256:…`. Terraform ignores the image field, so an apply never reverts a deployment.
- **The deploy job runs only after every check passes, and only on the default branch.** The checks are tests with the emulator, Terraform validation, checkov and gitleaks.
- **Pipeline settings are GitHub repository variables, not secrets:** `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_DEPLOYER_SA`, `GCP_IMAGE_REPOSITORY` and `GCP_RUN_SERVICES`. The runner image is pinned (`ubuntu-24.04`) through judging, and can be overridden with `CI_RUNNER`.

## Consequences
- **No key exists anywhere**, so there is nothing to rotate or leak.
- **Renaming the repository or the default branch breaks deployment** until bootstrap is re-applied.
- **Merges into `main` are gated by hand.** A private repository on GitHub's free plan cannot enforce branch protection, so the operator must see all checks green before merging (see the README's Deploy section).
- **Adding a service takes three changes in `infra/platform`**: the two deployer grants (`deployer` and `deployer_acts_as_runtime`) and the `run_services` output. Then a human applies and updates `GCP_RUN_SERVICES`. Without the grants, CI fails with `PERMISSION_DENIED` on that service.
- **Verified 29 Sep 2026**: the first run built, pushed and deployed both services; `asre-ingest` runs `…/agentic-sre/agentic-sre@sha256:…`.
