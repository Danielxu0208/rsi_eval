# Meta-Harness with Codex

This integration executes the [upstream Meta-Harness](https://github.com/stanford-iris-lab/meta-harness)
controller from commit `8123ccabe2b19fa1123090b2e5f1bacc20963ce8`.
Each experiment exports an independent copy with `git archive`. The original
`meta-harness/` clone remains unchanged.

The native loop runs baseline evaluation, proposals, validation, smoke tests,
scored evaluation, and frontier/history updates. It replaces the incumbent only
when a candidate scores strictly higher. The launcher binds execution functions
to Codex and the shared `evaluate()` SDK. Its only source patches remove the
unused Claude wrapper import and implicit dotenv loading.

This Codex variant uses an editable directory containing Python hooks, prompts,
and tools. The Harbor entrypoint is `rsi_eval.harbor_codex:Agent`. Native history
retains `agents.NAME:AgentHarness` as a directory reference. The candidate itself
implements the shared Codex artifact contract. This setup changes the original
Claude, KIRA/Terminus2, 89-task, and two-trial configuration.

## Run

For the managed setup, use `rsi-eval method prepare --method meta-harness` with
your profile and experiment directory, then `method check` and `method run`.
The [shared method guide](../../docs/methods.md) gives the complete commands.
The native workflow below lets you manage the backend and source clone yourself.

Follow the root README to prepare Python, Harbor's execution environment, and a
dedicated Codex CLI login. Edit `codex.model` and `codex.auth_dir` in the JSON
configuration. Proposals and target execution use the configured subscription
login. Some benchmarks require separate judge API credentials. No model access
backend is switched automatically.

From the project root, install the backend and obtain the pinned upstream code:

```bash
python3.12 -m venv .venv-harbor
.venv-harbor/bin/python -m pip install -e '.[harbor]'
git clone https://github.com/stanford-iris-lab/meta-harness.git meta-harness
git -C meta-harness checkout --detach 8123ccabe2b19fa1123090b2e5f1bacc20963ce8
export PATH="$PWD/.venv-harbor/bin:$PATH"
```

Reuse the backend if already installed. If the clone already contains the pinned
commit, skip the Git commands. The launcher exports that commit without changing
the source checkout. To use a clone elsewhere, add `--upstream-source PATH`.
Then run:

```bash
.venv-harbor/bin/python -m integrations.meta_harness.launcher \
  --config integrations/meta_harness/example.json \
  --output-dir runs/meta-example
```

The example configuration evolves on TB2.0 `extract-elf`, then evaluates the
winner on SWE Verified `astropy__astropy-12907` and TB2.1 `extract-elf`.
The declared model is `gpt-6-astra`, CLI version is `0.162.0-alpha.2`, and reasoning
effort is `medium`. These settings are editable and recorded for each run.

The alternative configurations `tb21_to_swe.json` and `swe_to_tb20.json` show
independent evolve/test selections. Each field accepts one benchmark selection or
a list. Specify task IDs or a fixed SDK manifest; manifest paths are relative to
the configuration file. With multiple evolve sets, frontier keys include the
selection index, benchmark, and version to distinguish shared task names.

Use `run_options` for shared SDK execution settings. Each benchmark selection
can also include `benchmark_runtime`, which replaces the shared
`run_options.benchmark_runtime` for that selection. An empty object clears the
shared settings. This keeps one benchmark's judge configuration out of another
backend. For example, a final test can use its own private judge file:

```json
{
  "run_options": {"timeout": 24000},
  "test": {
    "name": "harvey-lab",
    "version": "public-a15650f-v1",
    "task_ids": ["employment-labor/identify-issues-in-counterparty-motion-brief"],
    "benchmark_runtime": {"judge_env_file": "private/harvey.env"}
  }
}
```

Add these fields to the complete experiment configuration. Runtime paths,
including source directories and judge files, resolve against that file's
directory. The method still controls repetitions and concurrency. Existing
top-level `harbor_executable`, `environment` and `evaluation_timeout` settings
remain supported; explicit `run_options` values take precedence.

The native selection rule counts every `reward > 0` as a pass. By default the
launcher therefore rejects continuous evolve profiles such as DROP,
Frontier-Eng, JobBench, Harvey LAB, and APEX. An explicit `method_policy.score`
can map their measured results to binary outcomes for the native counter:

```json
{
  "method_policy": {
    "variant": "drop-f1-threshold",
    "score": {"kind": "threshold", "metric": "reward", "threshold": 0.8}
  }
}
```

This example defines a method objective and preserves the benchmark's raw
reward. Threshold mappings accept
`direction: "maximize"` or `"minimize"`. For task-specific objectives, use
`{"kind": "python", "path": "objective.py", "function": "score"}`. That
function receives `{"trial": ..., "benchmark": ...}` and must return `0` or `1`.
Missing results and infrastructure failures cannot become valid scores.

An optional `method_policy.selection` has the same Python configuration format.
Its function receives `candidates`, `native_choice`, and `context`. It returns
an eligible candidate ID or `null` to retain the incumbent. Each candidate
contains its mapped score, per-task rates, and raw SDK `evaluations`. The adapter
only offers candidates that passed the native validation and smoke gates and
completed evaluation. A baseline must be selected before search starts. This
hook controls the overall incumbent; the native per-task frontier is retained.

Policy files are trusted experiment code outside the candidate directory. Their
source is frozen in the experiment configuration and identified by SHA-256.
Reports label runs with policies as method variants, and `method-policy.json`
records mapped scores and selections separately. Smoke and final test scores
remain raw and never update the search objective.

Defaults are two rounds, one candidate per round, one trial per task, and
concurrency one. Completion follows native search decisions, including rejected
proposals that retain a measured incumbent. A valid score of zero is allowed;
the baseline can remain the winner.

Smoke tests run each evolve selection's first task as a separate trial. When
using a manifest or full dataset, provide an explicit `smoke` selection. Set
`skip_smoke: true` to disable this stage explicitly. The default example
uses seven solver trials and two proposer sessions. Smoke results are never
reused as scored evaluations. Subscription use has no inferred dollar cost.

## Outputs and recovery

Use an empty output directory for a new experiment. To continue an interrupted
experiment, supply its unchanged configuration and output directory:

```bash
.venv-harbor/bin/python -m integrations.meta_harness.launcher \
  --config integrations/meta_harness/example.json \
  --output-dir runs/meta-example --resume
```

Resume validates the model, CLI settings, template, integration and SDK source,
benchmark catalog, task manifests, saved candidate code, and evaluation evidence.
It reuses completed baseline and
candidate evaluations and committed proposals. Incomplete evaluations delegate
task recovery to the SDK. An interrupted proposer call may have to run again;
its session, known usage, and partial new candidate files are retained under
separate attempt records. Native history and frontier writes use a write-ahead
checkpoint, so a restart does not duplicate history rows or selection decisions.
Only one process can use an experiment directory at a time.

Runs created before durable checkpoints were introduced cannot be resumed with
this launcher. Completed reports are returned unchanged. Changing the experiment
settings or integration source requires a new output directory. Authentication
files may refresh in their configured location; their contents are never copied
into the experiment. The output contains:

- `experiment.json`: configuration and fixed upstream revision.
- `upstream/managed_checkout.json`: source, patches, and code checksums.
- `upstream/reference_examples/terminal_bench_2/agents/`: baseline and candidates.
- `logs/search/` within that example: native frontier, history, and Codex sessions.
- `jobs/search/` within that example: separate SDK reports and native Harbor results.
- `evaluations.json`: results by phase; `result.json`: method status and winner.
- `resume.json`: run identity, durable method state, code and evidence hashes,
  and proposer attempt records.
- `attempts/`: prior failure reports and uncommitted proposer artifacts.
- `method-policy.json`: explicit objective and selection decisions when used.

Evaluation attempt records may contain cumulative SDK recovery results. Use the
SDK's per-attempt usage records to account for retries instead of summing every
entry in `evaluations.json`. Proposer attempts are separate, including failed
calls with known usage. No cost is inferred for an interrupted call with missing
usage.

Final test scores are recorded after search and never update the frontier.
Proposer prompts contain only evolve selections. This data-flow convention is
not a security boundary against a malicious improvement method.

A valid search can reject every proposal and test its measured incumbent,
returning `status: completed`. Measured timeouts and agent errors retain their
original score and trial status during evolve/test. Smoke checks require normal
completion. Authentication, quota, container, and missing-verifier failures
produce a failure report and nonzero exit. Authentication or quota failures stop
the search before another proposer or solver call; completed evaluation records
remain available. The adapter never manufactures a zero score.
