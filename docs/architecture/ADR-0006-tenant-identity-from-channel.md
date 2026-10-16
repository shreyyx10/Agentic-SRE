# ADR-0006 — Tenant identity comes from the authenticated channel, never the payload

- **Status**: Proposed — implemented in `infra/platform` and `asre.ingest.app`; verified live 29 Sep 2026 (an unauthenticated POST to INGEST gets 404; the customer project's Monitoring agent is the only publisher on `alerts-example-shop`)
- **Deciders**: Engineering Lead

## Context
The old ingest accepted `customer_id` from the request body of a public endpoint. Anyone could post as any tenant. Cloud Monitoring webhooks cannot sign requests with HMAC (they support basic or token auth only), so HMAC could not have authenticated the flagship source either.

## Decision (proposed)
1. **One Pub/Sub topic per tenant** (`alerts-<customer_id>`), publishable only by that tenant's Cloud Monitoring notification service account.
2. **One push identity per tenant** (`push-<customer_id>`): the tenant's push subscription calls `POST /pubsub/<customer_id>` with an OIDC token for that identity.
3. INGEST has no `allUsers` binding; only tenant push identities hold `run.invoker`. In code it also verifies the token's audience, that the email equals *that path's* tenant push identity, and that the envelope's subscription is that tenant's subscription.
4. The payload's contents never select a tenant.

## Consequences
- No public ingest URL, no shared secret, no ALB/Cloud Armor needed for the prototype (T2.1 moves after submission, when a generic webhook for non-GCP senders is added).
- Onboarding a tenant = add it to `customers` in `infra/platform` and a profile in `config/tenants/`.
