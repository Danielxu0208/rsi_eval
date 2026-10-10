# Benchmark expansion validation

This work adds six configurations to the existing SDK and CLI. It checks small
task selections with the existing Codex artifact and a dedicated subscription.
It does not rerun method search loops, measure full-dataset scores, or reproduce
paper results. [Machine-readable evidence](validation-expansion.json) records
every completed attempt, including unsuccessful environment checks.

**Small-subset acceptance passed on macOS with Colima/Rosetta.** All six
configurations completed both a reference/oracle check and a separate Codex
candidate run. The candidate used `gpt-6-astra`, medium reasoning and Codex CLI
`0.162.0-alpha.2`. Model calls ran serially through the dedicated subscription.
Each selected task ran once per accepted job. Dollar cost remains unknown.

| Configuration | Supported catalog | Validation selection |
| --- | ---: | --- |
| Aider Polyglot, Harbor port | 225 | Python `dot-dsl`; Go `error-handling` |
| LiveCodeBench `release_v1` | 400 | Codeforces `1873_A`; LeetCode `2727` |
| MGSM agent protocol | 2,750 | `en-001`; `zh-002` |
| EngDesign-Open reviewed subset | 2 of 67 upstream open tasks | `AB_01`; `XY_01` |
| SWE-bench Multilingual, Harbor port | 300 | `preactjs__preact-3345` |
| SWE-bench Pro Public, Harbor port | 731 | OpenLibrary `00bec1e7c8f3272c469a58e1377df03f955ed478` variant `13642507b4fc1f8d234172bf8129942da2c2ca26` |

The catalogs provide complete selection for the declared profiles. They do not
claim every image has been executed. EngDesign's two-task profile remains an
explicit upstream subset even when both supported tasks run.

## Recorded results

Rewards below follow the task order in the table above. They are individual
verifier results, not estimates of full-benchmark accuracy. A zero means that
the candidate completed evaluation but failed the task's verifier.

| Configuration | Reference/oracle rewards | Codex rewards | Accepted run suffixes under `runs/expansion-` |
| --- | --- | --- | --- |
| Polyglot | 1, 1 | 0, 1 | `polyglot-pair-oracle-04`; `polyglot-pair-codex-02` |
| LiveCodeBench | 1, 1 | 1, 1 | `livecodebench-reference-02`; `livecodebench-codex-01` |
| MGSM | 1, 1 | 1, 1 | `mgsm-oracle-01`; `mgsm-codex-01` |
| EngDesign-Open subset | 1, 1 | 0, 1 | `engdesign-oracle-01`; `engdesign-codex-01` |
| SWE-bench Multilingual | 1 | 1 | `multilingual-oracle-01`; `multilingual-codex-01` |
| SWE-bench Pro Public | 1 | 1 | `pro-python-oracle-01`; `pro-python-codex-01` |

The evidence record contains 18 jobs and 29 trials, including earlier diagnostic
attempts. Raw Harbor results, verifier output and trajectories remain in the
local run directories. The portable record includes report and trajectory hashes
without authentication files.

Two earlier Polyglot pair checks exposed stale Docker build-context contents:
the Go oracle received the Python solution. The final pair passed after adding
[content-specific context isolation](harbor-context-isolation.md). Those failed
checks remain in the record. A separate Pro Go oracle also passed; the documented
candidate example uses OpenLibrary to keep the Mac integration check smaller.

## Offline and installation checks

The complete offline suite passed **206 tests and four subtests**, with one
scientific-dependency test skipped in the main environment. That EngDesign test
passed separately in an environment with NumPy, SciPy and Matplotlib. The real
EngDesign runs also exercised both upstream graders inside task containers.

A clean wheel installation listed all nine configurations and previewed all six
new examples without Harbor, NumPy or Pydantic. The README's offline demo passed
from the extracted source archive. The source distribution contains the grader
fixtures and their licenses. See [packaging evidence](validation-expansion-packaging.json).

These checks validate the shared evaluation path. They do not rerun Meta-Harness
or RRSI search loops on every new configuration. The original method integrations
retain their separate [two-round acceptance record](validation.md).

## Reproduce a small check

Complete the main README's Docker, Harbor and Codex login setup, then use one
of the six configurations in `examples/benchmarks/`. The model and CLI version
are explicit settings. Edit them to match your account before running.

```sh
export RSI_EVAL_SOURCE_CACHE="$PWD/.runtime/source-cache"
.venv/bin/rsi-eval preview --config examples/benchmarks/livecodebench.json
.venv/bin/rsi-eval run --config examples/benchmarks/livecodebench.json \
  --oracle --output runs/lcb-reference
.venv/bin/rsi-eval run --config examples/benchmarks/livecodebench.json \
  --output runs/lcb-candidate
```

LiveCodeBench's first source download is about 1.25 GB. The optional source cache
verifies content hashes on every use and does not reuse results. Its two declared
references are reviewed general programs, not official canonical solutions.
MGSM's oracle submits the dataset answer. EngDesign's references check the
unmodified upstream verifier. The Harbor ports use their bundled solutions.

## Scope and protocol

All configurations use the same `evaluate()` function. The method chooses task
IDs or a manifest for each call. No benchmark has a fixed evolve or test role.
The core has no new runtime dependencies; scientific dependencies run inside
task containers or the optional test environment.

Polyglot uses the declared Harbor port, which differs from Aider's native retry
controller. MGSM uses zero-shot agent submissions with an explicit numeric
answer format. LiveCodeBench preserves the official code grader and separates
single-submission rewards from pass@k. EngDesign retains both the native pass
flag and numerical score. See the individual benchmark documents for limits.

For the LiveCodeBench, MGSM and EngDesign materializers, grading data enters the
container after solving. The existing Harbor ports keep their original bundles;
Polyglot images include an encrypted oracle payload, so this framework does not
claim that their reference answers are inaccessible. The upstream coding graders
execute submitted programs and are not a security boundary for hostile code.
Container isolation and trusted candidate artifacts remain required.
