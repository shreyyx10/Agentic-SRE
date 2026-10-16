# ADR-0001 — GCP region

- **Status**: Accepted — 28 Sep 2026 · **Applied** 29 Sep 2026: Firestore `(default)`, Cloud Run, Cloud Tasks, Artifact Registry, the state bucket, the Secret Manager replica and the GKE cluster are all in `australia-southeast1`
- **Deciders**: Product Owner, Engineering Lead

## Context
A Firestore database's location cannot be changed after it is created ([docs](https://docs.cloud.google.com/firestore/native/docs/locations)). The first successful apply of `infra/platform` makes this decision whether or not anyone deliberates. The team is in Sydney; the first customers are expected in Australia; Gemini on Vertex AI is reached through the global endpoint, so region does not constrain model access.

## Options
- **A. `australia-southeast1` (recommended).** On-call follows the team's timezone; clean data-residency answer for Australian customers; low latency to them. *Against*: some GCP features reach US regions first.
- **B. `us-central1`.** Broadest feature availability. *Against*: support hours land at ≈2–11 AM Sydney; no AU residency story.
- **C. `nam5` multi-region.** Better durability. *Against*: US-centric, higher cost.

## Decision
**`australia-southeast1` (Sydney)** for Firestore, Cloud Run, Cloud Tasks, Pub/Sub and the demo GKE cluster. Gemini is reached through the Vertex AI global endpoint.

Decided by the engineering lead on 28 Sep 2026. The argument that settled it: operations follow the team, and region does not constrain model access.

## Consequences
Set `region` in `infra/bootstrap` and `infra/platform` tfvars. Firestore is protected either way: `deletion_policy = ABANDON`, delete protection on, `prevent_destroy`.
