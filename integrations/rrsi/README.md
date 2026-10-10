# RRSI with a Codex policy

This adapter runs upstream RRSI at `be50316e1db05914068a973f322770ef08ed7ba1` with a new `coding_codex` domain. Its analyst, trajectory digesters, proposer actions, critic, edit accounting, history, cost gate and default incumbent selection remain upstream code. The seed wraps one Codex CLI session. Candidates can change `agent.py`, `prompt.md` and supporting tools.

The shared evaluation service runs the selected tasks and returns rewards, token usage and native traces. The caller chooses evolve, smoke and test selections independently. A benchmark has no permanent role.

The native selection and cost rules require comparable rewards in `[0, 1]`. DROP fits this scale: RRSI selects on mean F1, while the SDK also reports exact match. This combination has offline coverage only. Frontier-Eng requires an explicit `method_policy.score` mapping before evolution because its tasks use different native objectives. The callback receives the raw trial and benchmark selection and must return a finite score in `[0, 1]`. Smoke checks and final tests retain their native scores.

Evolve and smoke selections accept explicit `task_ids`, a JSON `manifest`, or neither to select the full pinned catalog. Preparation resolves the choice once and records ordered IDs, source identities and hashes in `selection_provenance`. Candidate worktrees inherit the frozen list. Later edits to a manifest do not change an experiment, and catalog content drift stops the run. If smoke is omitted, it uses the first frozen evolve task.

An optional `method_policy.selection` callback can choose among candidates that passed all native gates. It cannot bypass the critic, smoke, complete measurements, noise floor or token cost rules. The callback receives `candidates`, `native_choice` and `context`; it returns an eligible candidate ID or `null` to keep the incumbent. Both callback types use `{ "kind": "python", "path": "objective.py", "function": "choose" }`. Preparation embeds the standalone callback source and its SHA256 in trusted experiment configuration. Keep callbacks outside the seed and candidate directories. Scores are mapped into a separate `method_scores.json`; original evaluation results remain unchanged. Any explicit policy is recorded as a method variant.

## Run the small example

For the managed setup, use `rsi-eval method prepare --method rrsi` with your
profile and experiment directory, then `method check` and `method run`.
The [shared method guide](../../docs/methods.md) gives the complete commands.
The native workflow below lets you manage the backend and source clone yourself.

Use the Python 3.12 Harbor environment, working Docker installation and dedicated ChatGPT Codex login described in the [root setup instructions](../../README.md#prepare-real-execution). The core-only `.venv` does not install Harbor. These commands use `.venv-harbor` explicitly and run from the toolkit root. The example pins CLI `0.162.0-alpha.2` and model `gpt-6-astra`; change those explicit values if your installed version or subscription differs.

Install the backend and obtain the pinned upstream code:

```sh
python3.12 -m venv .venv-harbor
.venv-harbor/bin/python -m pip install -e '.[harbor]'
git clone https://github.com/google-research/rrsi.git rrsi
git -C rrsi checkout --detach be50316e1db05914068a973f322770ef08ed7ba1
```

Reuse the backend if already installed. If the clone already contains the pinned
commit, skip the Git commands. Preparation creates a separate managed checkout
and leaves the source unchanged.

Put the separate backend on `PATH` before launching RRSI. Its native child processes also need to find the `harbor` executable:

```sh
export PATH="$PWD/.venv-harbor/bin:$PATH"
harbor --version
```

For the dedicated macOS Colima profile from the root instructions, use its Docker configuration and socket. The SWE acceptance task requires the AMD64 platform:

```sh
source scripts/mac-env.sh
export DOCKER_DEFAULT_PLATFORM=linux/amd64
docker info
```

The script sets `DOCKER_HOST` to `unix://$HOME/.colima/rsi-eval/docker.sock` and `DOCKER_CONFIG` to `$PWD/.runtime/docker`. On Linux, use your existing Docker endpoint and the platform supported by the selected tasks.

Copy `example.json` to a local experiment file and set `codex.auth_dir` to the dedicated login directory. Keep credentials outside managed checkouts, candidate artifacts and run outputs. Relative authentication and task-manifest paths resolve against the configuration's containing directory. Leave `run_options.harbor_executable` as `harbor` with the `PATH` above, or supply an absolute path.

`run_options` accepts the SDK's execution settings. A benchmark selection may
include its own `benchmark_runtime` object. This replaces the shared
`run_options.benchmark_runtime` for that call; an empty object clears it.
For example, add `"benchmark_runtime": {"judge_env_file": "private/harvey.env"}`
to a Harvey LAB selection, and supply a different file for an APEX selection.
Source, judge, asset and interpreter paths resolve before checkout preparation,
relative to the experiment configuration. Candidate worktrees and subprocesses
inherit these absolute paths. The method retains control of trial repetitions
and serial smoke execution.

```sh
cp integrations/rrsi/example.json rrsi-experiment.json
.venv-harbor/bin/python -m integrations.rrsi prepare \
  --source rrsi \
  --checkout .managed/rrsi-codex \
  --config rrsi-experiment.json

.venv-harbor/bin/python -m integrations.rrsi run \
  --checkout .managed/rrsi-codex \
  --runs runs/rrsi-codex

.venv-harbor/bin/python -m integrations.rrsi test \
  --checkout .managed/rrsi-codex \
  --runs runs/rrsi-codex \
  --output runs/rrsi-codex-test-swe

.venv-harbor/bin/python -m integrations.rrsi test \
  --checkout .managed/rrsi-codex \
  --runs runs/rrsi-codex \
  --selection integrations/rrsi/test_terminal_bench_2_0.json \
  --output runs/rrsi-codex-test-tb20
```

The example evolves on Terminal-Bench 2.1 `extract-elf` for two rounds, with one candidate and one trial per round. Each candidate first runs an independent task smoke evaluation, then its scored evaluation. Testing freezes the final incumbent and submits it to SWE-bench Verified `astropy__astropy-12907` or the supplied Terminal-Bench 2.0 selection.

`test --selection PATH` accepts an independent benchmark selection JSON file.
Its `manifest` and optional `benchmark_runtime` paths resolve against the
selection file's directory, regardless of the shell's working directory.

A clean successful example uses seven policy task trials: one baseline, two smoke trials, two scored candidate trials and two final tests. Analyst, digester, proposer and critic turns consume additional Codex usage. Manual smoke commands and retries add work. Acceptance and score increases are not required. One task and one repetition verify integration behavior; they do not estimate general improvement.

## Preparation and resume

`prepare` clones the fixed upstream commit locally, adds the tracked domain and seed, and replaces `rrsi.llm.generate`'s body with the Codex transport. If an explicit selection callback is configured, preparation also routes the selection call through a wrapper that runs all native gates first. It commits these additions before RRSI creates candidate worktrees. The original checkout stays untouched. Pass `--seed PATH` to use another compatible wrapper.

The launcher sets import paths and the role log directory for the native driver and all child processes. Repeating `prepare` with identical configuration, template and bridge reuses the checkout. Changed inputs require a new destination. Repeating `run` resumes upstream state; `--start N` retains the native start-round option. Configuration is fixed for each prepared experiment.

Authentication or subscription quota failures save `fatal_error.json` and the native `STOP` marker under `runs/rrsi-codex/coding_codex/`. The current round exits without advancing the frontier. Queued role calls and subsequent rounds do not issue further model requests. The launcher refuses another `run`, `baseline` or `smoke` while the marker exists.

Inspect the error report, renew the dedicated login or wait for quota recovery, then explicitly resume:

```sh
cat runs/rrsi-codex/coding_codex/fatal_error.json
# After resolving the recorded authentication or quota failure:
.venv-harbor/bin/python -m integrations.rrsi resume \
  --checkout .managed/rrsi-codex --runs runs/rrsi-codex
```

The explicit `resume` command validates the fixed configuration, archives recognized authentication or quota stop records under `resume_history`, and infers the first unsettled round. An unknown manual STOP marker requires inspection. A repeated failure creates a fresh stop record. Keep the existing checkout and run directory so upstream can reuse completed work. The error report contains failure metadata, never credentials. Ordinary measured task failures still follow the native evaluation and selection rules.

```sh
.venv-harbor/bin/python -m integrations.rrsi status --checkout .managed/rrsi-codex --runs runs/rrsi-codex
.venv-harbor/bin/python -m integrations.rrsi audit --checkout .managed/rrsi-codex --runs runs/rrsi-codex
```

`run` records native completion separately from strict integration acceptance.
A settled round can reject every proposal and keep its incumbent. That can
complete normally; missing evaluation evidence, usage, authentication or
infrastructure failures still fail the run. `acceptance_passed` and the explicit
`audit` command check the stronger requirement: each requested round contains a
changed candidate, an accepting critic, a successful real smoke run, complete
scored usage and a native selection decision. A round containing only invalid
proposals fails that strict audit. The audit never forces candidate selection.

Managed experiments also support `rsi-eval method test --experiment EXPERIMENT
--output TEST_RUN`, with an optional `--selection selections.json`, using the
verified native incumbent and frozen runtime. See the [shared method guide](../../docs/methods.md).

RRSI also supports `baseline` and `smoke` through this launcher. A smoke trial checks Python compilation and the wrapper interface, then runs the configured task through the shared service. Its verifier reward may be zero; the execution must complete. Smoke results never replace formal measurements.

## Feedback and selection

Results live under `runs/rrsi-codex/coding_codex`:

| Path | Contents |
| --- | --- |
| `jobs/<job>/evaluation/result.json` | Shared result and trial statuses |
| `jobs/<job>/evaluation/native/eval/` | Native Harbor output and trajectories |
| `r<N>/analysis/` | Rendered evidence and digester feedback |
| `r<N>/A/` | Proposal, critic, smoke, patch and scored evaluation |
| `r<N>/decisions.json` | Upstream selection decisions |
| `frontier.json` | Incumbent commit and round trajectory |
| `fatal_error.json`, `STOP` | Authentication or quota stop reason and resume gate |
| `codex_roles/` | CLI requests, event streams, responses and usage |
| `final_artifacts/<commit>/` | Frozen incumbent used by independent tests |

Policy cost `C` is mean input plus output tokens per trial. Missing usage fails rather than becoming zero. A completed SDK job can contain measured task timeouts or agent errors; their real verifier rewards and token counts remain valid observations. Absent rewards count as missing. A partial or failed job invalidates all its trials, even if some succeeded, so the native loop retries infrastructure once before rejecting the candidate. Authentication or quota failures stop evaluation. Subscription dollar cost remains unknown. The native coding domain's score and cost parameters remain defaults, including `delta=0.017`; callers can override `rrsi` fields. These values are not calibrated for the small example.

The transport uses tool-disabled Codex role calls and returns text to RRSI's existing JSON action executor. Its original JSON cleanup and malformed-action repair remain active. CLI transport failures surface immediately. The API's prefix caching and strict generation-token cap have no CLI equivalent: prefix text is retained, and a completed response exceeding the requested output budget is rejected. The model, backend and seed differ from the paper, so this is a functional integration rather than score reproduction.

## Verify the adapter

```sh
.venv-harbor/bin/python -m pip install -e '.[test]'
.venv-harbor/bin/python -m pytest -q tests/test_rrsi_integration.py
```

These tests execute pinned upstream control flow while replacing only external model responses and benchmark execution with deterministic fixtures. They cover native edits, raw-trace reading, critic screening, accepted and cost-rejected candidates, frozen-artifact testing, missing-token failure and backend loading in fresh subprocesses. They do not establish that real Codex authentication or benchmark containers work; the commands above provide that evidence.

The recorded [live validation](../../docs/validation-rrsi.json) completed two native rounds and seven solver trials using the dedicated Codex subscription and Docker. The first candidate was rejected; the second was accepted. The final artifact scored 1 on the selected SWE task and 0 on the TB 2.0 task. Its new comparison tool executed naturally in both second-round trials and the final TB test. These single-task results validate the integration, not a general improvement claim.
