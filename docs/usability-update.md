# Method compatibility and usability update

This is the 2026-10-10 validation record. See the later
[reliability update](reliability-update.md) for current fixes and checks.

GDPval and TTHE have been removed from the active package. The catalog now has
24 evaluation profiles and two legacy subsets. Meta-Harness, RRSI, AHE and
HarnessX remain available through the SDK and optional managed commands.

The changes add explicit score mappings and selection callbacks, correct RRSI's
manifest/full-set task selection, connect HarnessX to task-provided MCP, and
package the four method integrations and Codex template in the wheel. Managed
preparation freezes inputs and can recover recognized interrupted setup stages.
Core and method resume commands preserve prior evidence. The
[method guide](methods.md) describes commands, constraints and recovery scope.

Validation on 2026-10-10:

- The main offline suite passed **608 tests and 8 subtests**, with 8 skips. Six
  skipped HarnessX native/MCP checks were covered by the optional environment;
  two EngDesign checks lacked optional scientific dependencies in the core test
  environment.
- The separate HarnessX native/MCP suite passed **55 tests**. It overlaps the
  main suite; the counts should not be added together.
- A clean core-only wheel, used outside the repository, passed listing,
  preview, shared profile creation, the offline demo, and actual pinned local
  preparation and prerequisite checks for all four methods. These checks reused
  an existing optional dependency environment and made no model calls.
- A **hand-authored** changed HarnessX candidate passed native gates and ran
  a fresh TB 2.0 `log-summary-date-ranges` trial with reward 1. The actual model
  request contained the modification and matched the recorded candidate hash.
  The original proposer run still contains two no-op decisions. This new check
  verifies modified-candidate execution, not an algorithm-generated change.
- The real APEX world exposed 74 MCP tools; a read-only filesystem call and
  session cleanup succeeded. Native rubric grading still needs judge credentials.

The HarnessX functional check used seven Codex CLI calls, 81,763 input tokens
and 1,503 output tokens. Subscription dollar cost is unknown. Its
[evidence](validation/harnessx-functional-20261010.json) distinguishes the live
source snapshot from a later offline-tested cancellation-accounting change.

CI now separates the general offline suite, installed-wheel checks and optional
HarnessX runtime checks. The workflow was parsed locally and the corresponding
local checks passed; GitHub-hosted CI has not been executed in this workspace.
No full-dataset model runs, exhaustive method/benchmark matrix or paper-score
reproduction is claimed. See the
[machine-readable record](validation/usability-update-20261010.json) for hashes,
commands, receipts and remaining validation boundaries.
