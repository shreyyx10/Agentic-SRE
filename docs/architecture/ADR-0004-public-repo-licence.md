# ADR-0004 — Public repository and licence

- **Status**: Accepted — 17 Oct 2026
- **Deciders**: the team

## Context
The hackathon requires a public repository, not an open-source one. A public
repository with no licence is "all rights reserved": people can read it, and GitHub's
terms let them view and fork it on GitHub, but they may not copy, modify, distribute
or use the code.

## Options
- **A. Apache-2.0.** Anyone may use, modify and distribute it, including commercially.
  Once published under it, that version stays usable by anyone for good.
- **B. Source-available (e.g. BSL 1.1).** Readable and runnable; commercial use restricted.
- **C. No licence, all rights reserved.** Readable and evaluable; no reuse without permission.

## Decision
**C. All rights reserved.** `LICENSE` holds the copyright notice and states that no
licence is granted. The team keeps every option open: the code can be released under
an open-source licence later, which cannot be undone in the other direction.

## Consequences
Judges and readers can read, clone and run it to evaluate it. Nobody may reuse it
without written permission. Nothing proprietary (customer configuration, real
runbooks, credentials) enters this repository, and tenant files under
`config/tenants/` describe a fictional example only (ADR-0007).
