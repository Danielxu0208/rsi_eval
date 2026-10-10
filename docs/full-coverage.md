# Complete task coverage

The SDK must expose every public task in each declared, pinned configuration.
A small acceptance run does not justify a small supported set. This audit adds
the complete EngDesign-Open and Frontier-Eng configurations and checks the
other nine against their source releases. It does not run a full model scoring
experiment or reproduce paper results.

## Scope and evidence

| Configuration | Complete public set | Coverage evidence |
| --- | ---: | --- |
| Terminal-Bench 2.0 | 89 | Exact pinned Harbor registry comparison |
| Terminal-Bench 2.1 | 89 | Exact pinned Harbor registry comparison |
| SWE-bench Verified 1 | 500 | Pinned registry and official dataset IDs |
| Aider Polyglot 1.0-harbor | 225 | Pinned registry and original exercise IDs |
| LiveCodeBench release_v1 | 400 | Every source record and every encoded test case decoded |
| SWE-bench Multilingual 1.0-harbor | 300 | Pinned registry and official dataset IDs |
| SWE-bench Pro 1.0-harbor | 731 | Pinned public v1 registry and source IDs |
| MGSM 1.0-agent | 2,750 | All 11 pinned language files, 250 records each |
| DROP 1.0-agent | 86,945 | All 77,409 train and 9,536 dev records |
| EngDesign-Open open-c8ec338-v1 | 67 | Full source manifest and original schema/verifier routing |
| Frontier-Eng v1-bafa7d5 | 48 | All 47 v1 matrix tasks plus the upstream EngDesign aggregate |

The [nine-profile audit](coverage-audit-other.json) records comparisons, file
hashes, counts and exceptions. The [EngDesign guide](engdesign.md) and
[Frontier guide](frontier-eng.md) describe the complete execution adapters.
Runtime validation is recorded separately from source coverage: a listed task
or a successful `preview` alone does not establish that its grader ran.

“Complete” refers to the named public release. It does not include hidden test
answers, other versions or future tasks. SWE-bench Pro's later public releases
are different configurations. DROP's unreleased test labels are unavailable;
all published train/dev records remain selectable, including nine empty-answer
records. LiveCodeBench's two bundled oracle programs limit oracle checks, not
candidate selection across its 400 tasks.

## What changed

- EngDesign now packages all 67 task schemas, original evaluators, public inputs
  and private grader assets. Runtime recipes cover Python, Octave, Icarus
  Verilog and Webots. The two-task `open-subset-c8ec338-v1` profile remains a
  labeled legacy configuration.
- Frontier now uses the original unified runner and task metadata for all
  matrix entries. Runtime settings cover dependencies, external assets, GPUs
  and per-task platforms. The two-task `cpu-subset-bafa7d5-v1` profile remains
  a labeled legacy configuration.
- Explicit `benchmark_runtime` settings flow through JSON, SDK, `preview`,
  `check` and evaluation provenance. Failed prerequisites retain the original
  selection and return an infrastructure error; they do not omit tasks.
- Result collection builds one task-identity index for large evaluations.
  Every trial must still match its immutable package or generated task hash.
  Unknown or incomplete results do not become valid zero rewards.

## Selecting the full set

Omit `task_ids` and `manifest`:

```python
from rsi_eval import BenchmarkSelection, RunOptions, evaluate

result = evaluate(
    target=target,
    benchmark=BenchmarkSelection("engdesign-open", "open-c8ec338-v1"),
    options=RunOptions(concurrency=1, timeout=None),
    output_dir="runs/engdesign-all",
)
```

Use the same selection rule for every configuration. For JSON examples, remove
`benchmark.task_ids` and set `options.timeout` to `null` or a suitable whole-job
budget. A task's native timeout and evaluation budget remain separate from
this outer job timeout. Selecting all tasks does not assign them an evolve or
test role.

Before a real run, use `check` and read the benchmark's runtime requirements.
The current Apple Silicon Docker environment has no NVIDIA CUDA device.
GPU tasks remain in the catalog and fail explicitly when that prerequisite is
absent. Tasks with x86 toolchains require `platform: linux/amd64`; their emulated
execution is checked separately from ARM execution. MallocLab's official toolchain
and complete Harbor oracle path passed under Rosetta on this Mac. Set its
per-task platform override to `linux/amd64` when other tasks use ARM. SustainDC
also passed in an amd64 container with its unchanged native requirements; its
Ray dependency has no ARM64 distribution. No task is replaced
with an easier one or counted as passing without execution evidence.

## Runtime audit results

| Scope | Actual validation | Remaining boundary |
| --- | --- | --- |
| Nine earlier configurations | Complete source membership and pinned identities audited; earlier small real runs retained | This audit did not launch every Harbor task image or run full model experiments |
| EngDesign-Open | All 67 original schemas and native graders executed, including Verilog, Octave and Webots | 39 published responses passed; 28 received valid native failure verdicts. These are execution checks, not 67 successful solutions |
| Frontier-Eng CPU tasks | All 44 native graders executed, including MallocLab, SustainDC and all seven subgraders of its EngDesign aggregate | 43 seeds produced valid native results; `QuantumComputing/task_02_clifford_t_synthesis` executed but its seed missed the native precision threshold |
| Frontier-Eng CUDA tasks | All four routes and runtime requirements implemented; dependency checks recorded separately | Actual installation, assets and GPU grading still require validation on an NVIDIA host |

EngDesign's [67-task replay record](data/engdesign-native-validation.json) records
startup probes, schema checks, original verdicts and artifact hashes. Frontier's
[48-entry record](frontier-full-coverage.json) records individual runtime evidence
and differences between shared audit images and task-specific recipes. Tests run
against a recorded source/runtime snapshot; a later compatibility fix does not
retroactively change those hashes. The [combined report](validation-full-coverage.json)
also preserves small Harbor oracle runs and two real Codex trials. Failed earlier
Frontier transport and namespace runs are explicitly rejected as acceptance evidence.

The CPU checks found and repaired missing assets, platform-specific dependencies,
nested namespace failures, discarded native artifacts and non-finite auxiliary
metrics. Environment failures now remain errors instead of appearing as valid zero
scores. Original scoring formulas and native time budgets remain in place. Input
and execution adaptations are documented in the benchmark guides, including
EngDesign's handling of answer-bearing input images.

## Remaining limitation: four Frontier GPU tasks

| Task | Required environment |
| --- | --- |
| `Aerodynamics/CarAerodynamicsSensing` | NVIDIA CUDA plus the original PhySense code, dataset and checkpoint |
| `KernelEngineering/MLA` | NVIDIA CUDA, original PyTorch/Triton environment |
| `KernelEngineering/TriMul` | NVIDIA CUDA, original PyTorch/Triton environment |
| `KernelEngineering/FlashAttention` | NVIDIA CUDA, original PyTorch/Triton environment |

The actual Docker GPU probe on this Mac failed with
`no known GPU vendor found`. CPU emulation or Apple's GPU cannot validate these
CUDA graders under the original rules. This is a hardware limitation; it is not
evidence that the benchmark itself cannot be integrated. All four tasks remain
selectable and report unmet prerequisites explicitly.

The [dependency audit](data/frontier-gpu-dependency-validation.json) resolved the
Car and FlashAttention environments using the declared Python versions.
MLA/TriMul's 227 pinned packages form a closed dependency graph with no detected
version conflicts, but their source builds remain untested. Large CUDA wheels,
model assets and GPU evaluations were not downloaded or run for this check.
Car's upstream bootstrap fetches a mutable PhySense branch and Google Drive
assets without upstream checksums. The adapter records the downloaded bytes;
reusable asset snapshots are needed to freeze those external inputs.

To close this gap, use an NVIDIA-enabled amd64 Docker host, complete the original
environment and asset installation, and run the four native seeds through the
same adapter. Full model scores are unnecessary. Until then, the project does
**not** claim that all Frontier tasks have passed runtime validation. Retaining
or removing this benchmark remains the user's decision.

## Validation boundary

The final development suite passed 366 tests and four subtests. Two optional
scientific tests skipped in that environment both passed in the separate
scientific environment. The [packaging audit](validation-full-coverage-packaging.json)
records installation of the dependency-free wheel in a clean environment,
all example previews and the offline README demo. These checks make no model calls.

The earlier [v0.1](validation.md), [first expansion](benchmark-expansion.md) and
[DROP/legacy Frontier](validation-drop-frontier.md) reports retain their original
scope. They do not retrospectively certify the new complete profiles.

The complete-profile audit checks source membership, actual task preparation,
native graders and small Harbor runs without requiring full model experiments.
Each remaining runtime failure must identify the affected task, its observed
cause and the environment or upstream change required. No benchmark is removed
because its current environment is unavailable; that decision belongs to the
user.
