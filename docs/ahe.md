# AHE with Codex

This integration runs the public Agentic Harness Engineering loop from
[commit `8b2a55d97590363fe50c3cc6b5e833b020a4bb4c`](https://github.com/china-qijizhifeng/agentic-harness-engineering/tree/8b2a55d97590363fe50c3cc6b5e833b020a4bb4c).
It adapts the debugger and editor to a dedicated Codex subscription and routes
benchmark execution through `rsi_eval.evaluate()`.

The native Python loop executes directly. It maintains task history, compares
successes and regressions, checks change predictions one iteration later, tracks
the best measured candidate, and supplies rollback guidance to the next editor.
The adapter does not require a MethodAdapter subclass.

## Debugger integration

The released Agent Debugger is partially open source. The upstream authors state
this limitation in their
[README](https://github.com/china-qijizhifeng/agentic-harness-engineering/blob/8b2a55d97590363fe50c3cc6b5e833b020a4bb4c/README.md#how-it-works).
The adapter preserves public debugger queries, task ordering, and layered report
construction. Its Codex analysis call replaces the NexAU debugger's interactive
internal tooling. Unpublished debugger behavior is outside the public integration.

## Prepare

For the packaged setup, use `rsi-eval method prepare --method ahe` followed by
`method check`, `method run` and `method resume`; see the
[shared method guide](methods.md). The commands below expose the native launcher
for users who want to manage its environment and checkout themselves.

Run these commands from the RSI Eval repository root. The integration remains
outside the core SDK and has its own optional dependency environment.

```bash
python3.12 -m venv .venv-ahe
.venv-ahe/bin/pip install -e . -r integrations/ahe/requirements.txt

git clone https://github.com/china-qijizhifeng/agentic-harness-engineering .runtime/method-sources/ahe
.venv-ahe/bin/python -m integrations.ahe prepare \
  --source .runtime/method-sources/ahe \
  --checkout .runtime/ahe-prepared
```

If the source clone already exists, reuse it. `prepare` exports the fixed commit
without changing the source clone. It refuses to overwrite an existing prepared
checkout. `prepared.json` records source identity, patch descriptions, and file
hashes. `check` and `run` verify these hashes before loading upstream code.

The source patches remove implicit `.env` loading and environment logging,
rename trace bridge files so they do not impersonate NexAU traces, and add a final
evaluation-only iteration. Without that last change, the public single-candidate
loop finishes immediately after making its final edit.
Patch version 2 also uses stable filesystem aliases for task IDs containing `/`.
The saved reverse map restores original IDs in native grouping, debugger detail
files, history, modification manifests and delayed attribution. Raw SDK results
keep their original task IDs.

## Check and run

Complete the dedicated Codex login and Harbor setup described in the root README.
On macOS, when using the README's Colima profile, load its Docker environment:

```bash
source scripts/mac-env.sh
```

Edit `integrations/ahe/example.json` to set the available model, installed Codex
version, dedicated auth directory, and Harbor executable. Paths resolve relative
to the JSON file. The example uses two editing rounds, one trial per task, and
serial execution. AHE evolves on TB 2.0 `extract-elf` and tests the exported
candidate on TB 2.1 `extract-elf`.

```bash
.venv-ahe/bin/python -m integrations.ahe check \
  --checkout .runtime/ahe-prepared \
  --config integrations/ahe/example.json

.venv-ahe/bin/python -m integrations.ahe run \
  --checkout .runtime/ahe-prepared \
  --config integrations/ahe/example.json \
  --output runs/ahe-example
```

`check` probes configuration, prepared code, candidate identity, CLI versions,
Docker, and the shape of authentication for evolve and every final-test selection,
including their own runtime settings. It makes no model call. `run` records
baseline results, executes two debugger/editor rounds, evaluates both candidates,
and tests the selected artifact. It saves authentication and quota failures under separate statuses and stops
subsequent calls, including when the upstream debugger catches an exception.
It does not substitute an API backend if the subscription fails. Benchmark-specific paid graders still need their own keys.

`target_pass_rate: null` runs the requested number of rounds even when an early
candidate passes the selected tasks. A numeric threshold preserves the native
early-stop condition. Such a run reports `stopped_target_reached` and the actual
number of evaluated rounds.

## Select datasets

`evolve` accepts one ordinary benchmark selection. `test` accepts a list of
independent selections. Each supports `name`, `version`, `task_ids`, and `manifest`.
Omit task IDs and the manifest to select the complete pinned configuration.
`benchmark_runtime` may appear in `run_options` or within an individual selection;
selection-specific settings replace the defaults.

AHE's public loop reasons about binary pass/fail outcomes. DROP F1, rubric
averages and heterogeneous Frontier-Eng objectives require an explicit
`method_policy.score` mapping to binary outcomes for `evolve`. An optional
`method_policy.selection` callback updates the best tracker from measured
snapshots, affecting subsequent rollback guidance and final export.
See [policy examples](methods.md#choose-an-objective). Raw scores remain intact,
including in final tests; missing measurements cannot become valid policy scores.

The debugger analyzes up to `debugger_max_tasks` tasks per round, with the native
default of 30. Evaluation still runs every selected task. Each debugger call runs
serially and receives the original public analysis query, verifier evidence, and
recorded agent traces. Long source traces retain their first and last 60,000
characters. Truncation is marked in the analysis input; the complete raw artifact
and its hash remain available on disk.

## Editable components

The default candidate is `templates/codex_agent`. AHE can change:

| Surface | Execution path |
|---|---|
| Python control code | `agent.py` supplies prompt construction and environment hooks |
| Prompt | `build_prompt` reads `prompt.md` |
| Reusable tools | `prepare_environment` uploads `tools/` into the task container |
| Input preparation | `prepare_environment` runs before Codex starts |
| Output processing | `after_run` runs before verification |
| Memory or skills | Candidate code must explicitly load the new files |

This runtime does not expose NexAU's per-tool middleware, YAML component registry,
or native sub-agent routing. Creating a new file only changes behavior if an
executed hook loads or calls it. The method prompt describes these concrete
surfaces and prohibits edits to model settings, credentials, verifiers, and
recorded feedback. Each edit must change actual candidate files, pass Python syntax checks, and
supply a manifest that covers those edits. A native KEEP decision uses an empty
manifest and must leave every executable candidate file unchanged. `change_manifest.json`
and `evolve_summary.md` are reserved method deliverables. The launcher archives
them outside the executable artifact and preserves rejected manifests for diagnosis.

The adapter uses Codex's editable workspace mode for the proposer. The debugger
uses role mode with tools disabled. Both use the same serial subscription login.

## Evidence and selection

The output directory contains these artifacts:

| Path | Contents |
|---|---|
| `run_identity.json` | Configuration, pinned upstream commit, seed hash |
| `prepared.json` in the checkout | Exact source and patch hashes |
| `runs/iteration_NNN/input/workspace/` | Candidate before that evaluation |
| `runs/iteration_NNN/input/benchmark/rsi-eval/` | SDK report and unchanged Harbor results |
| `runs/iteration_NNN/input/benchmark/ahe-view/` | Labelled compatibility view for native AHE statistics |
| `runs/iteration_NNN/input/analysis/` | Native overview/detail reports and Codex debugger records |
| `runs/iteration_NNN/evolve/` | Manifest, edit evidence, and native evolution summary |
| `runs/iteration_NNN/input/change_evaluation.json` | Attribution for the preceding round |
| `evolution_history.md` | Native accumulated evaluation and edit history |
| `method_calls.json` | Debugger/editor accounting, including failed calls |
| `evaluations.json` | Accepted target-agent evaluation records and usage |
| `evaluation_attempts.json` | Target attempts, including failed and superseded runs |
| `selected_candidate/` | Export of the best measured snapshot |
| `test/` | Independent evaluations of that exact export |
| `summary.json` | Completion state, actual rounds, hashes, selection, and report paths |

The best export follows the upstream best tracker: only a strictly better score
replaces the incumbent, so ties retain the earlier artifact. Choosing that export
for final testing is an adapter policy. Upstream's `post_evolve` ordinarily tests
the last workspace. Both best and last hashes appear in the summary. The export must match the
recorded hash of the corresponding measured candidate before final testing.

A proposer may keep an effective candidate when the evidence supports no further
change. The integration records this as `decision: keep` and reevaluates the
unchanged artifact. `status: completed` means the native loop and requested tests
finished. `changed_rounds` and `noop_rounds` distinguish actual edits from KEEP
decisions. A KEEP decision never counts as a new changed candidate.

The native loop attributes previous edits as EFFECTIVE, PARTIALLY_EFFECTIVE,
MIXED, INEFFECTIVE, or HARMFUL. It asks the next editor to keep or roll back changes
based on that evidence. The integration preserves this decision process; it does
not invent automatic per-change rollback thresholds. The original full-snapshot
rollback routine restores interrupted runs and remains available for inspection.

Token usage from the target stays separate from debugger/editor usage. Subscription
cost in dollars is unknown. A result must cover every requested trial and match
the evaluated candidate hash before it can enter native AHE statistics.

## Recover an interrupted run

First resolve the reported authentication, quota, environment, or candidate error.
Use `rsi-eval method resume --experiment EXPERIMENT` for a managed experiment.
The native launcher's `--resume` option also infers the last saved input iteration.
To choose that iteration explicitly:

```bash
.venv-ahe/bin/python -m integrations.ahe run \
  --checkout .runtime/ahe-prepared \
  --config integrations/ahe/example.json \
  --output runs/ahe-example \
  --resume-iteration 2
```

Iteration 1 evaluates the baseline and creates candidate 1. Iteration 2 evaluates
candidate 1 and creates candidate 2. Iteration 3 evaluates candidate 2 without
another edit. The saved input workspace must exist for the requested iteration.

Resume restores that snapshot using the native rollback function. It archives
aborted rounds and downstream test outputs under `resume_history/`, preserves
all previous model-call accounting, and reruns the selected round. Configuration
and seed identity must match. Completed runs require a fresh output directory.
