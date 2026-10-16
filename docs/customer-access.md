# Customer access: read-only by construction

The platform can read a customer's logs and metrics. It cannot change anything in their project, and the customer can prove it in their own IAM console.

## What the customer grants

One platform identity per customer, `cust-<customer_id>@<platform-project>.iam.gserviceaccount.com` (output `tenant_reader_service_accounts` of `infra/platform`), and exactly two roles in the customer's project:

```bash
READER="<infra/platform output tenant_reader_service_accounts[customer_id]>"   # e.g. $READER_SA in your asre.env
gcloud projects add-iam-policy-binding "$CUSTOMER_PROJECT" --member="serviceAccount:$READER" --role=roles/logging.viewer
gcloud projects add-iam-policy-binding "$CUSTOMER_PROJECT" --member="serviceAccount:$READER" --role=roles/monitoring.viewer
```

`roles/logging.viewer` includes Admin Activity audit logs, which is where deployment history comes from. No other role is needed.

## How to revoke

```bash
gcloud projects remove-iam-policy-binding "$CUSTOMER_PROJECT" --member="serviceAccount:$READER" --role=roles/logging.viewer
gcloud projects remove-iam-policy-binding "$CUSTOMER_PROJECT" --member="serviceAccount:$READER" --role=roles/monitoring.viewer
```

Revocation takes effect within minutes. Every read the platform makes appears in the customer's Cloud Audit Logs (Data Access logs, if enabled), attributed to that identity.

## How alerts reach the platform

The customer creates a Cloud Monitoring **Pub/Sub notification channel** pointing at the tenant topic from the `tenant_topics` output of `infra/platform` (by default `projects/<platform-project>/topics/alerts-<customer_id>`). The platform grants publish rights on that topic to the customer's Monitoring notification service account only (see ADR-0006). No webhook URL, token or secret is exchanged.

## Verification

- A write attempted as the reader identity fails with `PERMISSION_DENIED`.
- The customer project's audit log shows the platform's reads under the reader identity.
