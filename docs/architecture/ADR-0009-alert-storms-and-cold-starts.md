# ADR-0009 — Alert storms and cold starts: no hot-document transactions, and a first-call budget

- **Status**: Proposed — implemented and verified 29 Sep 2026 (emulator in CI, and live)
- **Deciders**: Engineering Lead

## Context
Two failures showed up once the code met real infrastructure.

1. **Alert storms.** The first design ran every repeat alert through a read-modify-write transaction on its group document. In CI, the Firestore emulator test that fires 50 identical alerts at once failed, because some transactions gave up after 10 attempts. The client retries a conflicting commit immediately, so all 49 losers collide again. Adding jittered backoff between rounds did not fix it: the emulator still gave up after 8 rounds, and the test took 90 s instead of 26 s. The design itself created a hot spot.
2. **Cold starts.** The first live smoke test logged `enqueue_failed: 504 Deadline Exceeded`. The first Cloud Tasks call from a new instance also opens the gRPC channel and fetches an access token, and 500 ms was not enough. No alert was lost: Pub/Sub redelivered it 30 s later and it succeeded. But the first alert after a quiet period is often the one that matters.

## Options (storms)
- **A. Take plain repeats off the transaction path (chosen).** A repeat inside the window that needs no decision is recorded with one server-side `Increment`. Concurrent increments never conflict.
- **B. Backoff only.** *Against*: shown insufficient on the emulator. It treats the symptom and makes storms slower.
- **C. Sharded counters.** *Against*: more documents and reads for a problem that A removes.

## Decision
- **Storm path.** After `create()` loses, INGEST reads the group without a transaction. If the group is open, inside its window, needs no update and needs no analysis takeover, INGEST records the repeat with one atomic update: `occurrence_count` is incremented, and `last_occurrence`, `dedup_until` and `expires_at` are set. The decision is `SUPPRESS`. Every decision that creates work goes through the existing side-effect-free transaction, which re-decides on fresh data:
  - a new incident after the window;
  - the time-based update;
  - a takeover when enqueueing failed;
  - resolution.
- **Why a stale read is safe.** If the group changed after the read, the only effects are:
  - one occurrence is counted on the document that now holds the key;
  - one alert is suppressed instead of triggering an update, which the next repeat triggers instead.

  Nothing is lost or duplicated.
- **Backoff stays as a second layer.** A transaction that still loses is re-run for up to 8 rounds, with full-jitter exponential backoff (25 ms base, 1 s cap). Only contention (`Aborted`) is retried; any other error is raised at once.
- **First-call budget.** Until a call succeeds on an instance, dedup gets 3 s and enqueue gets 5 s. After that, each gets 500 ms. A Terraform `check` requires the two first-call budgets together to fit inside INGEST's request timeout.
- **Everything is a variable** (ADR-0007): the `dedup.tx_*` retry settings, and `dedup.dedup_first_timeout_ms` and `dedup.enqueue_first_timeout_ms` for the cold-start budgets.

## Consequences
- **Measured.** 50 concurrent identical alerts now produce 1 group, a count of exactly 50, and 0 transactions. A 49-thread simulation of the old design had 37 of 49 give up; the new design had none. All emulator tests pass in CI.
- **Live smoke test, 29 Sep:** no `enqueue_failed` and no `dedup_uncertain` on a freshly started revision.
- **The occurrence count returned in a `SUPPRESS` decision is a lower bound under concurrency.** The stored count is exact.
- **The repository interface gains `increment()`,** which both the in-memory and the Firestore repositories implement.
