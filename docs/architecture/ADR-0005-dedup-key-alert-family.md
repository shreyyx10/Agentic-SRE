# ADR-0005 — The de-duplication key uses the alert family, not the fine-grained type

- **Status**: Proposed — implemented in `asre.core.models.FAMILY_OF` / `asre.core.keys`; verified live 29 Sep 2026 (smoke test: one alert → one group; its repeat suppressed)
- **Deciders**: Engineering Lead

## Context
The original design keyed alert groups on `{customer, service, alert_type, environment}`. A design review noted the compounding effect: one real incident reported by two alert policies with different types produces two groups, two analyses, two Briefs. The flagship case is exactly this — an OOM kill *causes* CrashLoopBackOff, and both are commonly alerted on.

## Decision (proposed)
Key on the **family**: `oom_killed` and `container_crash` → `container_failure`; `high_latency` → `latency`; `error_rate` → `errors`; `saturation`; `unknown`. The fine-grained type is still classified (with the precedence table), stored on the group, shown to the responder and used for routing.

## Consequences
- An OOM alert and a restart alert for the same service within the window are one incident (`test_oom_and_crashloop_from_two_policies_are_one_incident`).
- Risk: two genuinely different container failures of one service inside one window merge. Accepted — the Brief's evidence and hypothesis distinguish causes; a merged Brief is cheaper than a duplicate page.
