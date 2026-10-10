# Additional method integrations

> Historical experiment record. TTHE was removed from the active package after
> these runs. The current methods, packaging, policies and recovery commands are
> documented in [the method guide](methods.md). The HarnessX guide records a later
> hand-authored changed-candidate check. Counts and results below describe the
> earlier source snapshot and have not been rewritten.

These optional integrations connect published improvement loops to RSI Eval.
They keep their own search state, candidate format and selection rules. The SDK
does not require a method base class. Install each method's dependencies in its
own environment; the core package remains dependency-free.

| Integration | Pinned upstream commit | Scope |
| --- | --- | --- |
| [AHE](ahe.md) | `8b2a55d97590363fe50c3cc6b5e833b020a4bb4c` | Published outer loop with Codex wrapper candidates and an adapted debugger transport |
| [TTHE](tthe.md) | `69a614d04f2a7f3eacb64a63193078ab4be56ba5` | Published LiveCodeBench population loop with Codex model calls and isolated public feedback |
| [HarnessX](harnessx.md) | `915520584c92cc35ed24aef1af17eec1d60c0b01` | Published harness-evolution workflow with native processors and a Codex model provider |

Each guide describes preparation, execution, output files and differences from
the upstream experiment. These are Codex adaptations. They do not establish the
paper scores or reproduce unpublished components.

## Execution contracts

AHE analyzes scored trajectories, records a change manifest and checks predicted
effects against later observations. Its adaptation uses the editable Codex
wrapper rather than the original NexAU runtime. The supported edit surfaces and
the partially published debugger are documented in its guide.

TTHE adapts on an ordered stream of unlabeled task batches. Its proposer and
judge receive public execution evidence. Hidden tests and formal scores stay
outside their workspaces. The selected harness carries forward; the grader later
scores cached submissions whose identities were committed during search. This
transductive protocol differs from freezing a candidate before all test inputs.
The first adapter targets the existing LiveCodeBench `release_v1` configuration.
Other TTHE domains require their own execution bridges.

HarnessX retains its own runtime. Codex returns model messages and structured
tool requests; HarnessX executes the tools and applies processors. Its published
evolution workflow is the integration target. Full paper AEGIS and model-weight
training are outside this adapter's scope.

## Choosing benchmarks

The benchmark catalog does not assign evolve or test roles. Each method still
has an execution and scoring contract. Binary pass/fail selection, continuous
objectives, label-free adaptation and task-provided tool services require
different capabilities. Unsupported combinations must fail explicitly rather
than discard tasks or substitute a different selection objective.

Custom Harbor targets can accept `artifact_dir` in their constructor. Supplying
that key in `TargetSpec.config` asks the SDK to bind it to the immutable candidate
snapshot. Generated logs and mutable state belong outside this directory.

## Validation

Validation checks functional execution on small task sets. It does not require
score improvements or full-dataset model runs. Offline checks use deterministic
external responses and pinned upstream control flow. Live checks use the
dedicated Codex subscription and Docker; they preserve model usage and native
grading evidence. Missing usage remains unknown, and subscription dollar cost is
not inferred from API pricing.

The 2026-10-10 experiments use `gpt-6-astra`, Codex CLI
`0.162.0-alpha.2`, the dedicated ChatGPT subscription, and Docker on macOS.
They run serially and use small, explicitly selected task sets.

| Method | Real execution evidence | Modification evidence |
| --- | --- | --- |
| AHE | Two native decisions, three TB 2.0 evaluations, and a TB 2.1 compatibility test completed | First decision changed the prompt; second decision kept it |
| TTHE | Two LiveCodeBench batches, two branches per batch, native judging, inheritance, and hidden grading completed | Both first-batch branches changed; both second-batch branches kept the selected harness |
| HarnessX | Two native rounds, history reads, both smoke checks, and seven benchmark trials completed | Both rounds explicitly kept the original configuration |

AHE's task rewards were `0`, `1`, and `1` on TB 2.0 `extract-elf`, followed
by `1` on the same task in TB 2.1. TTHE's committed outputs passed both
`lcb-leetcode-2727` and `lcb-leetcode-2879`. These small observations establish
execution evidence, not a benchmark-wide score or a generalization claim.

HarnessX scored `0` on `extract-elf` and `1` on `log-summary-date-ranges`
for its TB 2.0 baseline and both rounds. Its final TB 2.1 `extract-elf` trial
scored `0`. Native records confirm that each trial loaded its candidate and
that the second round read the first round's history. Both configurations were
semantically unchanged. The upstream workflow skips novelty and evidence
policy checks for these no-op decisions; deterministic native tests cover the
changed-candidate gates. This live run does not establish modification-path
coverage.

Execution completion and stronger modification checks remain separate. AHE
reports `acceptance_passed: false` because it did not make two changed proposals.
TTHE reports `evaluated_acceptance_incomplete` because its second batch made no
new changes. Both retained an incumbent through valid native decisions. The
adapter does not force artificial edits or rename those results as strict
acceptance passes.

The original AHE bookkeeping failures and HarnessX transport failures remain in
their run directories. AHE recovery restored exact model-generated artifacts
and native bookkeeping without new model calls. An earlier TTHE `react` run
graded both tasks successfully but produced only no-op branches; that record
also remains unchanged. The later TTHE output-to-observation checks were audited
against original receipts without rewriting the historical experiment.

The final offline suite passed 577 tests and 8 subtests, with 5 tests skipped.
A separate run with the pinned HarnessX checkout passed all 45 integration
tests, including its native runtime. These suites overlap and should not be
added together. A clean core-only wheel installation also passed CLI listing,
preview and the offline demo. The source distribution contains the optional
integrations and their guides.

The [machine-readable validation record](validation-method-expansion.json)
contains task results, candidate hashes, usage, recovery notes and delivered
source hashes. Raw traces remain in the local experiment directories named in
that record. They are not bundled with the SDK.

Each method guide provides commands, output paths and recovery instructions.
The original Meta-Harness/RRSI v0.1 acceptance is a separate experiment and is
unchanged by these results.
