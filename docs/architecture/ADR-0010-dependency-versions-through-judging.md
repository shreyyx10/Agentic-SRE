# ADR-0010 — Dependency versions through judging: Google provider 8.4, no unplanned major upgrades

- **Status**: Proposed — implemented 29 Sep 2026
- **Deciders**: Engineering Lead

## Context
The public link must keep working from submission (17 Oct) through judging (6 Nov), and through the finale (4 Dec) if the team is shortlisted. On 28 Sep, Dependabot opened eight major-version PRs, including the Google Terraform provider moving from 6 to 8. The grouped PR that followed was merged before its checks had run. It moved the version constraint from `~> 6.40` to `~> 8.4`, even though an `ignore: semver-major` rule was already in place. Separately, GitHub moves `ubuntu-latest` to Ubuntu 26 on 19 Oct, in the middle of judging.

## Options
- **A. Stay on provider 8.4, verified, and block the next major explicitly (chosen).** Nothing had been applied yet, so there was no state to migrate.
- **B. Revert to `~> 6.40`.** *Against*: an older line that stops receiving fixes; and 8.4 had already passed every check.

## Decision
- **Google provider `~> 8.4`** in all three stacks. It was verified with the real 8.4.0 provider before the first apply:
  - `validate` passes;
  - the full test suite passes;
  - checkov reports 0 failures;
  - the release notes from 6.x to 8.4 were reviewed for every resource type used. The only change affecting us is a fix for a permanent diff on Cloud Run's `scaling` block.
- **Dependabot**:
  - one grouped PR per ecosystem per week, with minor and patch updates only;
  - `hashicorp/google >= 9.0.0` blocked by name, because the generic rule did not stop a constraint rewrite;
  - major upgrades of GitHub Actions deferred until after the finale. Their Node 20 deprecation warnings are harmless, because GitHub already runs them on Node 24.
- **The CI runner is pinned** to `ubuntu-24.04`, overridable with the `CI_RUNNER` repository variable.
- **Merge discipline.** Run `gh pr checks <n> --watch` on its own, and merge only after every check is green.

## Consequences
- The toolchain cannot change under the judges without a deliberate commit.
- Major upgrades pile up. Schedule them after 4 Dec, one ecosystem at a time.
