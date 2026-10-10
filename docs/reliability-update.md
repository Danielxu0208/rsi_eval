# Reliability and portability update

This update addresses the requested items 1–9 and 11 from the repository review.
It keeps the four native method loops and benchmark scoring rules. No model or
paid judge was called during this update's validation.

All requested items are complete within the validation scope below. The APEX
audit passed for all 31 pinned worlds, with one task selected per world.

## Changes

| Item | Change | Validation |
| --- | --- | --- |
| 1 | AHE maps task IDs containing `/` to stable filesystem aliases and retains the original IDs in feedback and history. | Native deterministic rounds exercise aliases, debugger queries, statistics and delayed attribution. |
| 2 | AHE checks every final-test selection before starting, with each selection's effective runtime options. | Missing prerequisites in a later test set fail before the search loop. |
| 3 | Meta-Harness and RRSI distinguish native completion from strict changed-candidate acceptance. | Valid rejected proposals can retain a measured incumbent; missing measurements and infrastructure failures still fail. Explicit acceptance audits remain stricter. |
| 4 | All four methods expose `rsi-eval method test`. | Each loads its verified, measured native selection and can evaluate new selections without changing search history. |
| 5 | All four optional dependency environments install from scratch. | Four new environments passed installation, `pip check`, native imports and prerequisite checks; four more passed lock replay. |
| 6 | Method preparation records platform-specific dependency locks. Explicit runtime snapshots cover materialized tasks, Harbor packages and Git tasks. | A real Docker fixture preserved its image/assets after source deletion and a changed build argument. A real Harbor registry oracle passed capture/restore/replay with the same image. |
| 7 | New managed experiments support verified `experiment export/import`. | All four native integrations recovered after relocation; the checks include interrupted runs, RRSI worktrees and missing SDK trial recovery. |
| 8 | Audit every pinned APEX world through native startup, MCP discovery/read, initial/final snapshots and cleanup. | All 31 worlds passed, with one official task per world and 74 discovered tools each; no agent or rubric judge was run. |
| 9 | Custom Compose builds isolate local contexts, external Dockerfiles and local additional contexts by content. | Real Docker builds with identical file metadata and different hidden bytes used the correct contents, while retaining runtime mounts, environment files and overrides. |
| 11 | Synchronize setup/recovery guides, packaged resources and automated checks. | Clean installed-wheel checks run outside the checkout; the local offline suites pass. GitHub-hosted CI is not claimed. |

`method test` preserves raw benchmark metrics. Explicit score mappings and
selection callbacks remain method configuration, rather than changes to the
benchmark verifier. See the [method guide](methods.md) for commands and the
[runtime snapshot guide](runtime-snapshots.md) for dependency and image retention.

## Evidence and limits

The main suite passed 694 tests and 8 subtests, with 10 skips. Eight skipped
HarnessX checks were exercised in its optional native environment, where the
57-test suite passed. The other two skips require matplotlib and OpenCV for
existing EngDesign tests. These suites overlap; their counts must not be added.
The snapshot and recovery checks include package/Git replay, native task names,
cache deletion, missing evidence, and changed task bytes or paths.

The [Docker snapshot record](validation/runtime-snapshot-20261011.json),
[Harbor registry snapshot record](validation/registry-snapshot-20261011.json),
[Compose isolation record](validation/compose-isolation-20261011.json), and
[host lock evidence](../integrations/locks/validation.json) preserve their
validation scope and source hashes.

The [APEX world record](validation/apex-worlds-20261011.json) covers native
healthchecks, a real read-only MCP call, both native snapshot archives and
cleanup for every pinned world. The audit enabled the existing initial-snapshot
flag for message-only tasks through a temporary Compose layer; official task
files and graders stayed unchanged. Earlier diagnostic attempts and source
observations remain recorded. This is environment coverage, not a run of all
240 tasks or their paid rubric judges.

The [consolidated validation record](validation/reliability-update-20261011.json)
indexes test logs, installation checks, source hashes and the delivery wheel.

The clean wheel contains the dependency-free core, 24 evaluation configurations,
two legacy subsets, the Codex template and four method integrations. From a new
core environment outside the repository, all four methods passed preparation,
prerequisite checks, export, import and checks at the new location. This check
reused the separately audited new optional environments; it is distinct from
the eight fresh dependency installations. The four-method wheel check preceded
a final SDK fix for native Harbor task names. That fix passed the full local
suite, a real registry snapshot replay and a separate clean-wheel check;
the method integration files remained byte-identical.

Portability covers newly prepared managed experiments on the same host platform
with an exactly matching method environment. It preserves candidate and raw
evaluation bytes, records control-file path changes, and rebinds credentials
separately. It does not repair old experiments or promise cross-platform imports.

Host locks pin package versions and record downloaded artifact hashes. Runtime
snapshots archive all observed Compose service images and prepared task assets,
including native Harbor package and Git tasks. Neither mechanism freezes external web services
or model behavior. Paid judge credentials and required hardware remain separate
prerequisites. Old mutable image tags are not reconstructed.

The main repository's Git history, remotes and publication were left untouched.
The five excluded items also remain unchanged: paid native-judge access,
NVIDIA/CUDA validation, native EngDesign grading changes, incomplete legacy
experiment recovery, and reconstruction of historical mutable resources.

This update establishes functional and recovery behavior. It does not claim
full-dataset scores, paper reproduction, or an exhaustive method/benchmark matrix.
