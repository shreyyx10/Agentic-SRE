# ADR-0003 — No autonomous ticket creation; the Incident Brief is the artifact

- **Status**: Proposed — decide by Tue 29 Sep 2026 (blocks T4.8; decides whether T2.8 exists)
- **Deciders**: Product Owner (business call), Engineering Lead

## Context
An earlier design made an auto-created Jira ticket the output. The current design argues the ticket is off the mitigation path, is the costliest integration, and needs write credentials into the customer's system of record — contradicting "read-only by construction".

## Options
- **A. Remove it (recommended).** The Brief at a stable URL is the artifact; delivery is one adapter — a threaded **Google Chat** message for the hackathon. Ticket filing returns later as a human-triggered "File this" button. The platform holds no customer secret at all.
- **B. Keep it.** *For*: L1 works in ticket queues; "it files the ticket" demos well. *Against*: credential subsystem, write path, larger blast radius for any dedup failure.

## Decision
> _To be recorded._

## Consequences
With A, `T2.8` (per-customer credentials) is out of scope and the only public surface is the read-only Brief page for the demo tenant.
