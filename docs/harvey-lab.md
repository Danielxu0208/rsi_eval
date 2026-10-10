# Harvey LAB

`harvey-lab@public-a15650f-v1` exposes all **2,010 public tasks** from
[Harvey LAB commit a15650f](https://github.com/harveyai/harvey-labs/tree/a15650fe74bb20b86f160aaabdad75b87a91b6e8).
The release covers 27 practice areas and 114,437 rubric criteria. Its 60,971
input files remain intact, including the shared document corpus used by all
250 firm-knowledge tasks. Omitting `task_ids` selects the complete pinned set.

## Scoring and access

The adapter runs the original `lab_core.evaluation.run_eval` and document
extractors. The default judges remain `claude-sonnet-4-6` and `gpt-5.5`.
Each grades every criterion independently. A judge awards a task 1 only when
all criteria pass. The default reward is the native mean of these two task
verdicts: 0, 0.5, or 1. Criterion pass rates remain diagnostics.

The target may use your Codex subscription. **The native judges require
Anthropic and OpenAI API access.** Subscription authentication does not supply
these API credentials. Preflight stops before target execution when keys are
missing. We do not substitute Codex for either judge or convert a judge error
into a zero score.

A custom list of one or two distinct native judge models is supported through
`benchmark_runtime.judges`, matching upstream `--judges`. The report labels it
as a custom profile. Anthropic access remains necessary because the native
filename-matching fallback calls Claude even with another scoring model.
Additional judge calls for this fallback are part of upstream behavior.

The official release provides no gold deliverable files. `oracle` mode is
therefore unavailable for this profile. A synthetic scoring fixture validates
transport and arithmetic without claiming a benchmark result.

## Run

Inspect the example without calling a model or downloading documents:

```sh
rsi-eval preview --config examples/benchmarks/harvey-lab.json
```

Create `.runtime/harvey-judge.env` locally with `ANTHROPIC_API_KEY` and
`OPENAI_API_KEY`, one `KEY=value` assignment per line. The repository ignores
`.runtime`. Keep this file private. Values enter only the Harbor child process
and its separate verifier environment; saved configuration contains variable
names and the file path. They are absent from the agent image and task files.
You can instead set these variables in your shell and omit `judge_env_file`.

```sh
rsi-eval check --config examples/benchmarks/harvey-lab.json
rsi-eval run --config examples/benchmarks/harvey-lab.json
```

The example selects one task. Delete `benchmark.task_ids` to select all 2,010.
Choose evolve and test sets in your own loop; this adapter assigns no roles.

Preparation downloads the pinned official Git commit, approximately 3 GB of
documents. To reuse a local checkout, set:

```json
{
  "benchmark_runtime": {
    "source_dir": "../../.runtime/harvey-labs",
    "judge_env_file": "../../.runtime/harvey-judge.env",
    "judges": ["claude-sonnet-4-6", "gpt-5.5"],
    "judge_parallel": 6
  }
}
```

These fields belong inside `options`. Relative paths resolve against the
configuration file. Source reuse still checks every selected rubric, native
code file, and document against the bundled hashes. Modified input is rejected.
Shared documents are snapshotted once per run, not once per referencing task.

Other runtime options are `agent_timeout_sec`, `verifier_timeout_sec`,
`build_timeout_sec`, `cpus`, `memory_mb`, and `platform`. Defaults are 7,200
seconds for the target, 14,400 for grading, 7,200 for builds, 2 CPUs and
4,096 MiB. The optional platform is `linux/amd64` or `linux/arm64`.

## Execution and artifacts

The target receives the original task instructions, complete read-only
`/workspace/documents`, and upstream document skill manuals and scripts.
It writes final files to `/workspace/output`. The original sandbox Dockerfile
supplies document tools. The selected Harbor target controls its tool loop;
the upstream reference agent's 200-turn default and network isolation are not
imposed on an arbitrary target. Record its configuration when comparing runs.
This is the native data and grading protocol with a Harbor target interface,
not a reproduction of the upstream reference agent.

Harbor transfers output artifacts into a separate verifier container.
Rubrics and judge credentials are inaccessible to the target. The verifier
uses the frozen upstream `uv.lock`, the official Pandoc 3.11 installer, and
unchanged scoring code. Runtime dependencies are saved with the results.

The verifier keeps per-judge `scores_*.json`, `scores_dual.json`, the native
HTML report, `harvey-lab.json`, output hashes, and numeric `reward.json`.
Single-judge runs preserve `scores.json`. Native judge API usage is unknown
because the pinned class does not expose token accounting. It is not reported
as zero and is not mixed into target token usage.

Missing artifacts, invalid judge results, provider failures, and failed
conversion setup produce an error without a reward. A valid candidate that
fails rubric criteria receives the native zero score. Raw diagnostics remain
available.

## Resume an interrupted evaluation

Restore the required credentials or services, then resume in the original output
directory. The example configuration writes to `runs/harvey-lab-example`:

```sh
rsi-eval resume --output runs/harvey-lab-example
```

Recovery validates the saved candidate, benchmark, runtime settings and evaluator
identity. It retains completed measurements, including valid zero scores, and
retries only unfinished task repetitions. Each explicit invocation makes at most
one new attempt per missing repetition. Prior artifacts and known usage remain
recorded; a retry can make new target and judge calls.

Use a new output directory when changing the candidate, selection or scoring
configuration. Older runs without the required execution identity cannot use
this recovery path. Preserve those records and start a separate evaluation of
the unfinished task IDs. See [the shared recovery guide](methods.md#recover-work)
for SDK calls and managed method experiments.

## Validation

[The machine-readable audit](validation-harvey-lab.json) records the full task
inventory, native loader checks, document hashes, and a deterministic native
scorer fixture. The focused regression suite also checks shared-corpus reuse,
private rubric placement, secret-free task files, invalid grading results,
and Harbor's separate-verifier schema.

To repeat the source and scorer audit after cloning the pinned checkout:

```sh
UV_PROJECT_ENVIRONMENT="$PWD/.runtime/harvey-verify" \
  uv sync --project .runtime/harvey-labs --frozen --no-dev --python python3.12
PYTHONPATH=src .runtime/harvey-verify/bin/python scripts/harvey_lab_native_audit.py \
  --source .runtime/harvey-labs --output .runtime/harvey-audit.json
```

This command makes no model calls. All 2,010 tasks materialize and their
generated Harbor configurations validate. Both the original agent sandbox
and the generated verifier image build on Linux ARM64. A native Harbor
environment probe checks document hashes, read-only inputs, writable output,
document parsing, the three native skill manuals, Pandoc, LibreOffice, and
the Node document libraries. The verifier's dual scorer executes with a
deterministic fixture. Both probes run with networking disabled and make no
model calls.

Actual provider grading remains unverified without the required API
credentials. Full-set interface coverage does not claim full-set model
evaluation or reproduction of published scores.
