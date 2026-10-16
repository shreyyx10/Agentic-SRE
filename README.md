# Agentic SRE

An AI on-call agent for Kubernetes services on Google Cloud. When a Cloud Monitoring
alert fires, it gathers the evidence, works out the likely cause with Gemini, has a
second model check that diagnosis, and posts an **Incident Brief** into the incident's
Google Chat thread, usually within a minute.

Every claim in a Brief cites the evidence it rests on (a log line, a Kubernetes event,
a metric, a deployment change). Claims without a valid citation are dropped before
anyone sees them. The agent **reads only**: it uses a viewer-only identity that the
customer grants and can audit in their own IAM, and it never changes their cluster. It
proposes next steps; a person decides and runs them.

Built for the Google Cloud AI Builder Cup 2026.

## What it does

- **Explains an incident.** Logs, Kubernetes events, metrics, recent deployment changes
  (from the Admin Activity audit log) and the busiest services, gathered within a fixed
  budget, then a hypothesis from Gemini with the alternatives it ruled out and next
  steps with their risk.
- **Checks itself.** A critic model reviews every diagnosis against the same evidence
  and marks it supported, weak or contradicted. A contradicted diagnosis is labelled
  "one option, not the answer".
- **Treats customer text as data.** Logs and alert text are sanitised (secrets and card
  numbers redacted) and fenced off from instructions, so text planted in a log line or
  an alert cannot steer the agent.
- **Never goes silent.** If both models fail or answer unusably, the Brief still ships
  with the evidence and default first steps for that kind of alert.
- **One storm, one incident.** Repeated alerts for the same service and alert family
  join one incident and update its Brief instead of paging again. An alert that fires
  again soon after resolving reopens the same incident.
- **Links incidents with the same cause.** When a second incident's diagnosis cites the
  same deployment change as an open one, its Brief is posted as a reply in the first
  incident's thread.
- **Forecasts trouble.** Every 5 minutes it looks for memory, CPU or error rates
  climbing steadily towards their limit (a robust trend fit, ignoring spikes and noise)
  and raises a forecast Brief before anything fails.
- **Multi-tenant.** Each customer has its own Pub/Sub topic, push identity and reader
  identity; the tenant is decided by the authenticated channel, never by the payload.

## How it works

```
Customer project                             Platform project (this repository)
Cloud Monitoring alert policy
  └─ Pub/Sub notification channel ──────────► topic alerts-<customer>
                                               └─ push (OIDC) ──► INGEST (Cloud Run)
                                                  verify tenant · normalise · classify
                                                  · de-duplicate (Firestore)
                                                  └─ Cloud Tasks (OIDC) ──► ANALYSE (Cloud Run)
Logs · metrics · audit log ◄── reader identity (read-only) ──┤  evidence → sanitise →
                                                             │  Gemini hypothesis → citations
Cloud Scheduler (every 5 min) ──► ANALYSE /tasks/scan ───────┤  checked → critic → Brief
                                                             └─► Google Chat (webhook in
                                                                 Secret Manager)
```

One container image runs both services (`ASRE_SERVICE=ingest|analyse`). Models are
Gemini on Vertex AI: the hypothesis model with one fallback model, and a separate critic
(see [ADR-0002](docs/architecture/ADR-0002-model-platform.md)).

## Repository layout

```
src/asre/core/        models, classification, de-dup (+ Firestore), tenants, auth, queue, config, secrets
src/asre/ingest/      INGEST: Cloud Monitoring adapter and the Pub/Sub push endpoint
src/asre/analyse/     ANALYSE: task endpoint, pipeline, correlation, forecast scan endpoint
src/asre/agent/       model layer with fallback, prompt, output contract, critic, sanitiser, default steps
src/asre/evidence/    evidence sources and the read-only telemetry client
src/asre/predict/     forecasting (trend fit, rank correlation, time to limit)
src/asre/delivery/    Google Chat cards, threads, updates and resolutions
config/tenants/       one JSON file per customer: services, owners, routing (example-shop.json)
infra/bootstrap/      APIs, state bucket, image repository, keyless CI deploy identity (run once)
infra/platform/       Firestore, Pub/Sub, Cloud Tasks, Cloud Run, Scheduler, IAM, secrets, budget
infra/example_customer/  the customer side: reader grant, notification channel, alert policies
docs/                 architecture decisions (ADRs) and the customer access model
tests/                unit, integration (Firestore emulator) and regression tests
```

## Develop

Python 3.11 or later.

```bash
make install          # virtualenv + pinned dependencies
make lint             # ruff
make test             # unit tests, 80% coverage gate
make test-integration # needs the Firestore emulator (gcloud components install cloud-firestore-emulator)
make tf-check         # terraform fmt + validate for every stack
```

## Configure a customer

Each customer is one file, `config/tenants/<customer_id>.json` (see
[`example-shop.json`](config/tenants/example-shop.json)): the cluster and namespace to
read, the services with their owners and how critical they are, and routing rules that
pick the owning team. Identities (topic, push and reader service accounts) are added by
`infra/platform`, not written by hand.

Alert policies need user labels so the agent knows what it is looking at: `service`
(the Kubernetes workload), `alert_type` (`container_crash`, `oom_killed`, `error_rate`, `high_latency`,
`saturation`), `environment` and `severity`. `infra/example_customer` shows
three complete policies.

## Deploy

You need two Google Cloud projects (the platform, and a customer project with a GKE
cluster to watch), Terraform 1.6+, `gcloud`, and a GitHub repository for CI.

1. **Keep settings outside the repository.** Make a folder next to your clone with an
   `asre.env` file and your `*.tfvars` files (nothing environment-specific goes in the
   repository; [ADR-0007](docs/architecture/ADR-0007-configuration-and-secrets.md)):

   ```bash
   export ASRE_HOME="$(cd .. && pwd)/.asre"
   export PLATFORM=<platform-project-id>
   export CUSTOMER_PROJECT=<customer-project-id>
   export REGION=<region>                       # e.g. australia-southeast1
   export GH_REPO=<owner>/<repo>
   export STATE_BUCKET=<globally-unique-bucket-name>
   export CUSTOMER_ID=example-shop              # must match config/tenants/<id>.json
   export CLUSTER_NAME=shop
   export K8S_NAMESPACE=default
   ```

2. **Bootstrap the platform project** (once): APIs, the Terraform state bucket, the
   image repository and the keyless CI deploy identity.

   ```bash
   cat > "$ASRE_HOME/bootstrap.tfvars" <<EOF
   project_id        = "$PLATFORM"
   region            = "$REGION"
   state_bucket_name = "$STATE_BUCKET"
   github_repository = "$GH_REPO"
   EOF
   terraform -chdir=infra/bootstrap init -backend-config="path=$ASRE_HOME/bootstrap.tfstate"
   terraform -chdir=infra/bootstrap apply -var-file="$ASRE_HOME/bootstrap.tfvars"
   ```

3. **Deploy the platform.** Fill `$ASRE_HOME/platform.tfvars` from
   `infra/platform/terraform.tfvars.example` (project, region, the deployer service
   account from step 2, and your customer with `monitoring_channel_ready = false`), then:

   ```bash
   terraform -chdir=infra/platform init -backend-config="bucket=$STATE_BUCKET"
   terraform -chdir=infra/platform apply -var-file="$ASRE_HOME/platform.tfvars"
   ```

4. **Connect the customer.** Apply `infra/example_customer` in the customer project (or
   do the same by hand): it grants the platform's reader identity `logging.viewer` and
   `monitoring.viewer` only, creates the Pub/Sub notification channel to the tenant
   topic (`terraform output tenant_topics`) and the alert policies. Then set
   `monitoring_channel_ready = true` and apply `infra/platform` again, which lets that
   channel publish. See [docs/customer-access.md](docs/customer-access.md).

5. **Add the Chat webhook** as a secret value (never in a file or variable):

   ```bash
   read -rs WEBHOOK && printf '%s' "$WEBHOOK" | \
     gcloud secrets versions add chat-webhook-url --data-file=- --project "$PLATFORM" && unset WEBHOOK
   ```

6. **Deploy the services through CI.** Set the repository variables `GCP_PROJECT_ID`,
   `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_DEPLOYER_SA`, `GCP_IMAGE_REPOSITORY` (outputs of
   `infra/bootstrap`) and `GCP_RUN_SERVICES` (output `run_services` of `infra/platform`).
   Every merge to `main` builds the image and rolls both services to it by digest
   ([ADR-0008](docs/architecture/ADR-0008-keyless-ci-deployment.md)).

7. **Check it.** `scripts/smoke_test.sh` runs an end-to-end check against the deployed
   platform, and `scripts/synthetic_alert.py` prints a Cloud Monitoring notification you
   can publish to the tenant topic to see a Brief arrive in Chat.

Defaults for every other setting (service sizes, timeouts, de-dup windows, retry
behaviour, the forecast scan, the monthly budget) are documented in each stack's
`variables.tf`.

## Licence

Copyright © 2026 Core-X. All rights reserved. The code is public so
it can be read and evaluated; no licence to use, copy, modify or distribute it is
granted. See [LICENSE](LICENSE).
