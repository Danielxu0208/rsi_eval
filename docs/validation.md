# Validation status

The required v0.1 small-subset acceptance passed on macOS with Colima/Rosetta and a dedicated Codex subscription. This page distinguishes implementation checks from measured task outcomes. No API key or Claude subscription was used.

This page records the original three configurations and two method loops. The
six added benchmark profiles have a separate [expansion report](benchmark-expansion.md)
and [machine-readable record](validation-expansion.json).
The later DROP and Frontier-Eng additions have their own
[validation report](validation-drop-frontier.md).

| Layer | Status | Evidence |
| --- | --- | --- |
| SDK, CLI, candidate snapshots and result parsing | Offline tests passing | `tests/test_core.py` |
| Codex runner and Harbor target | Offline contract tests passing | `tests/test_codex.py`, `tests/test_harbor_codex.py`, `tests/test_runtime_contract.py` |
| Meta-Harness native loop | Offline fixtures passing | `tests/test_meta_integration.py` |
| RRSI native loop | Offline fixtures passing | `tests/test_rrsi_integration.py` |
| Source and wheel installation | Core-only quickstart passed without Harbor or model access | [Packaging evidence](validation-packaging.json) |
| Mac Docker runtime | Verified | Colima, Apple Silicon, Rosetta |
| Real benchmark oracles | All three passed, reward 1 | [Machine-readable record](validation-oracles.json) |
| Dedicated Codex subscription session | Normal device sign-in completed | Credentials kept outside the repository |
| Real Codex subscription execution | Host role call and two consecutive container trials passed | [Runtime evidence](validation-runtime.json); both task rewards were 0 |
| Real Meta-Harness two-round acceptance | Passed: two changed candidates and seven solver trials | [Method evidence](validation-meta.json) |
| Real RRSI two-round acceptance | Passed: two changed candidates and seven solver trials | [Method evidence](validation-rrsi.json) |
| Full benchmark scores / paper reproduction | Not run; outside acceptance scope | Small subsets test integration only |

Each evolve measurement below is one run of `extract-elf`, using the same fixed selection for the baseline and both candidates:

| Codex variant | Evolve configuration | Baseline reward | First candidate | Second candidate | Native final selection |
| --- | --- | ---: | ---: | ---: | --- |
| Meta-Harness | Terminal-Bench 2.0 | 0 | 0 | 0 | Baseline retained |
| RRSI | Terminal-Bench 2.1 | 0 | 0 | 1 | Second candidate accepted |

Both frozen final artifacts scored 1 on SWE-bench Verified `astropy__astropy-12907`. Meta-Harness scored 0 on the separate TB 2.1 `extract-elf` test; RRSI scored 0 on the separate TB 2.0 test. These were completed native evaluations. Related task versions and single repetitions do not establish generalization or reliable score improvement. RRSI's second candidate also scored 0 in its independent smoke trial.

Meta-Harness used two proposer CLI invocations and seven solver trials. RRSI used 43 role CLI invocations and seven solver trials. Role and policy usage are recorded separately; CLI invocations are not counts of internal model requests. The reports preserve the original selection decisions, second-round feedback reads, candidate hashes, native trajectories, verifier output and known token usage. Subscription dollar cost remains unknown.

The initial TB 2.0 oracle returned reward 1 but exposed a Harbor 0.18 metric-group crash when explicit tasks carried a dataset source without a dataset configuration. The wrapper now leaves native grouping at `adhoc` and retains dataset provenance in `resolved.json`. A regression checks the real Harbor metric resolver.

The original v0.1 offline suite passed 102 tests and four subtests. It includes interruption handling that preserves progress and prevents further role calls after an authentication or quota error, plus selection-file-relative task manifests. The expansion report records the newer suite.

Apple Silicon runs task containers in a Linux VM. Architecture settings and failed environment attempts remain in the oracle record. No task IDs, verifier tests or expected answers are changed to make an environment pass.
