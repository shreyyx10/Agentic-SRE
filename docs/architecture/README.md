# Architecture Decision Records

Decisions that constrain the code live here, versioned with it. Format: Context → Options (with the case against the recommendation) → Decision → Consequences. The Decision section stays blank until someone actually decides, and records who decided and the argument that settled it.

| ADR | Decision | Status |
|---|---|---|
| [0001](ADR-0001-gcp-region.md) | GCP region: `australia-southeast1` | **Accepted** 28 Sep |
| [0002](ADR-0002-model-platform.md) | Model platform: Gemini 3.1 Pro with 3.8 Flash fallback, on Vertex AI | Accepted — 10 Oct |
| [0003](ADR-0003-no-autonomous-tickets.md) | No autonomous ticket creation; the Brief is the artifact | Proposed — due 29 Sep |
| [0004](ADR-0004-public-repo-licence.md) | Public repository, all rights reserved (no open-source licence) | **Accepted** 17 Oct |
| [0005](ADR-0005-dedup-key-alert-family.md) | De-duplication key uses the alert *family* | Proposed — implemented, verified live |
| [0006](ADR-0006-tenant-identity-from-channel.md) | Tenant identity comes from the authenticated channel | Proposed — implemented, verified live |
| [0007](ADR-0007-configuration-and-secrets.md) | Nothing environment-specific in the repository; secret values only in Secret Manager | **Accepted** 28 Sep |
| [0008](ADR-0008-keyless-ci-deployment.md) | Keyless CI deployment by image digest (Workload Identity Federation) | Proposed — implemented, verified live |
| [0009](ADR-0009-alert-storms-and-cold-starts.md) | Alert storms without hot-document transactions; first-call budget on cold instances | Proposed — implemented, verified live |
| [0010](ADR-0010-dependency-versions-through-judging.md) | Google provider 8.4; no unplanned major upgrades through judging | Proposed — implemented |

`Proposed` → `Accepted` → (later) `Superseded by ADR-NNNN`. Never delete a superseded ADR.

"Verified live" means the behaviour was observed on the deployed platform on 29 Sep 2026 (`scripts/smoke_test.sh`, all five checks passing), not only in tests.
