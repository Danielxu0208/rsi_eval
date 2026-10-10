# RSI Eval

A small Python SDK and CLI for evaluating agent self-improvement methods across existing benchmarks. Keep your search loop, choose your own evolve and test tasks, and call the same `evaluate()` function for each candidate.

Optional integrations cover **Meta-Harness**, **RRSI**, **AHE**, and the
public **HarnessX** harness-evolution workflow. Each keeps its own improvement
loop and documents its Codex adaptation. The guides explain setup,
configuration and supported behavior. These
integrations do not reproduce the papers' scores.

Use `rsi-eval status` to inspect benchmark compatibility, prerequisites and
objective requirements. Filter with `--benchmark NAME@VERSION` or
`--method NAME`; use `--json` for structured output.

The [method guide](docs/methods.md) covers setup, explicit objectives and recovery.

## What is included

| Configuration | Tasks | Execution |
| --- | ---: | --- |
| `terminal-bench@2.0` | 89 | Harbor 0.18.0 |
| `terminal-bench@2.1` | 89 | Harbor 0.18.0 |
| `swe-bench-verified@1` | 500 | Harbor 0.18.0 |
| `aider-polyglot@1.0-harbor` | 225 | Harbor port; distinct from native Aider retries |
| `livecodebench@release_v1` | 400 | Python code generation, upstream grader |
| `swe-bench-multilingual@1.0-harbor` | 300 | Harbor port |
| `swe-bench-pro@1.0-harbor` | 731 | Public release, Harbor port |
| `mgsm@1.0-agent` | 2,750 | Pinned TSVs, numeric-answer verifier |
| `engdesign-open@open-c8ec338-v1` | 67 | Complete pinned Open task set, upstream verifiers |
| `drop@1.0-agent` | 86,945 | Public train/dev records, official EM/F1 scorer |
| `frontier-eng@v1-bafa7d5` | 48 | All 47 v1 tasks plus the upstream EngDesign aggregate; native unified runner |
| `mmlu@original-all-splits-v1` | 115,700 | All released splits; 14,042 are official test records |
| `gpqa@main-56686c0-v1` | 448 | Original question release, seeded choice order |
| `gpqa@diamond-56686c0-v1` | 198 | Complete diamond release |
| `gpqa@extended-56686c0-v1` | 546 | Complete extended release; overlaps other GPQA profiles |
| `arc-agi@1-public-3990304-v1` | 800 | All public ARC-AGI-1 train/evaluation tasks, original three-attempt rule |
| `mce-{finer,uspto,symptom2disease,lawbench,aegis}@meta-harness-8123cca-v1` | 400 / 180 / 462 / 350 / 668 | Complete five MCE releases, native prompts and graders |
| `jobbench@d43a665-660b33a-v1` | 128 | All 65 main + 63 easy tasks; original xAI judge requires an API key |
| `harvey-lab@public-a15650f-v1` | 2,010 | All 27 practice areas; native dual judge requires API keys |
| `apex-agents@1.1.1` | 240 | Complete 1.1.1 release, native world MCP and rubric judge; judge API key required |

The catalog contains **24 evaluation configurations and two legacy subsets**. SDK versions identify pinned public configurations, not necessarily upstream release numbers. Catalogs record immutable Harbor task references or source revisions and file hashes. Omitting `task_ids` selects the entire declared configuration. The two-task EngDesign and Frontier profiles are labeled **legacy subsets**; the examples use the complete profiles. Task coverage does not establish a full-dataset score.

See the protocol and setup notes for [Polyglot and the additional SWE variants](docs/harbor-expansion.md), [LiveCodeBench](docs/livecodebench.md), [MGSM](docs/mgsm.md), and [EngDesign](docs/engdesign.md).

No benchmark has a permanent evolve or test role. Those choices belong to your method. TB 2.0 and 2.1 contain related tasks; testing the same task across versions checks compatibility, not unseen-task generalization.

The [Reasoning and MCE guide](docs/reasoning-and-mce.md) describes their task
selection and scoring protocols. [JobBench](docs/jobbench.md),
[Harvey LAB](docs/harvey-lab.md) and [APEX](docs/apex-agents.md) retain their paid
native judges and require separate judge credentials. Publisher terms apply;
APEX is released for
evaluation, not training, fine-tuning or parameter fitting.

[DROP](docs/drop.md) retains its released train/dev
membership and nine training records with empty reference answers.
[Frontier-Eng](docs/frontier-eng.md) tasks have different dependencies: some require
an NVIDIA GPU, external data, or an x86 Linux runtime. Unmet requirements
must produce an explicit error, never a smaller task selection or a fabricated
zero score.

The core has no third-party runtime dependencies. Harbor and its dependencies are optional. There is no server, database, dashboard, shared leaderboard score, or required method base class.

## Install and run without a model

Use Python 3.12 or newer. The reference Harbor environment uses Python 3.12. Both the wheel and source distribution include the SDK, CLI, Codex template and four optional method integrations. Installing the core does not install the methods' runtime dependencies. From the repository or unpacked source distribution:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/rsi-eval list
.venv/bin/rsi-eval preview --config examples/evaluate.json
.venv/bin/python examples/offline_demo.py
```

The demo starts a labeled fake Harbor process and exercises the actual SDK, candidate snapshot, native-result parser and report. It makes **zero model calls** and returns `mode: offline_fixture`. It does not run a benchmark container.

`preview` resolves exact task hashes, the candidate hash and run parameters without network or model calls. Relative paths in a CLI configuration resolve against that file's directory; `--output` resolves against the current directory.

Additional examples live in `examples/benchmarks/`. For example:

```sh
.venv/bin/rsi-eval preview --config examples/benchmarks/polyglot.json
.venv/bin/rsi-eval preview --config examples/benchmarks/mgsm.json
```

Data-backed adapters prepare standard Harbor task directories at run time. Their
source records and generated task hashes remain in `resolved.json`; native output
remains under `native/`. Preparation can download public benchmark data. Set
`RSI_EVAL_SOURCE_CACHE="$PWD/.runtime/source-cache"` to reuse downloaded sources
explicitly. Each use verifies the file hash. This optional cache stores source
files only; fresh evaluations run in a new output directory. Explicit recovery
continues a recorded evaluation without overwriting its prior attempts.

Each example sets a finite whole-job timeout. Office and MCP tasks use longer
budgets than the small coding examples. Increase or remove
`options.timeout` before using a larger selection. Task-level budgets still come
from the declared benchmark configuration.

Docker execution also [isolates Dockerfile and custom Compose build contexts](docs/harbor-context-isolation.md)
to prevent equal-metadata files from different tasks sharing stale BuildKit
content. It preserves original bytes and records the copied context hash.
Custom Compose configurations preserve runtime paths and record provenance for
each isolated local build context. Unsupported remote build contexts fail with
an explicit error; see the guide for supported context types.

## Prepare real execution

Install the backend separately:

```sh
python3.12 -m venv .venv-harbor
.venv-harbor/bin/python -m pip install -e '.[harbor]'
export PATH="$PWD/.venv-harbor/bin:$PATH"
```

Harbor is pinned to `0.18.0`. The SDK invokes its CLI through a subprocess and checks the version before starting a job. You can instead set `options.harbor_executable` to an absolute path.

### macOS

A Mac can run the benchmark's Linux containers through Docker. On Apple Silicon, x86-only task images also need emulation. The setup below uses Colima with Rosetta; Docker Desktop is another compatible Docker runtime.

```sh
brew install colima docker docker-compose docker-buildx
colima start rsi-eval --cpus 6 --memory 12 --disk 120 \
  --vm-type vz --vz-rosetta --runtime docker --activate=false \
  --ssh-config=false --mount "$PWD:w"
source scripts/mac-env.sh
docker info
docker compose version
```

The profile has its own Docker socket and does not replace your default Docker context. The disk size is a virtual capacity limit. Adjust CPU and memory for your machine. Rosetta must be available for the `--vz-rosetta` option. See [Colima installation](https://colima.run/docs/installation/) and [Docker on Apple Silicon](https://docs.docker.com/desktop/setup/install/mac-install/).

SWE-bench's selected image is x86-only. When a task requires it, set `DOCKER_DEFAULT_PLATFORM=linux/amd64` and record that setting with the run. Buildx must be available to the Docker CLI. The original task image, instructions and verifier stay unchanged.

For x86-only task images on Apple Silicon:

```sh
export DOCKER_DEFAULT_PLATFORM=linux/amd64
```

Frontier-Eng tasks can require different platforms. The complete EngDesign
profile selects platforms per task, including amd64 for its default Webots
images. Before running these examples, use `unset DOCKER_DEFAULT_PLATFORM`. See Frontier's
[runtime notes](docs/frontier-eng.md#execution-boundary-and-compatibility-fixes) for configuration details.

Stop the dedicated VM when finished:

```sh
colima stop rsi-eval
```

On Linux, use a working Docker Engine and Compose installation; no Colima profile is needed.
For NVIDIA/CUDA task setup and troubleshooting, see [the Linux GPU handoff](handsoff.md).

### Codex subscription login

The bundled method variants and Codex target use a Codex subscription. Coding,
reasoning and MCE graders need no model API key. JobBench, Harvey LAB and APEX
also require the separate judge credentials documented in their guides. Install
Codex CLI and create a separate, file-backed ChatGPT login for this experiment:

```sh
mkdir -p "$HOME/.codex-rsi-eval"
chmod 700 "$HOME/.codex-rsi-eval"
CODEX_HOME="$HOME/.codex-rsi-eval" codex \
  -c 'cli_auth_credentials_store="file"' login
codex --version
```

Use normal browser sign-in or `login --device-auth`. Do not put login credentials in run JSON, candidate directories or the repository. The CLI handles token refresh. The runner serializes uses of the dedicated login and saves refreshed container credentials before cleanup. See [Codex non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode) and [authentication persistence](https://learn.chatgpt.com/docs/auth/ci-cd-auth).

Edit `examples/evaluate.json` to set your accessible model, dedicated auth directory and exact Codex CLI version. The current example uses `gpt-6-astra`, `0.162.0-alpha.2` and medium reasoning. These are explicit experimental settings; the runner does not silently change models or fall back to an API key. Subscription limits still apply.

### Check, validate the environment, and evaluate

```sh
.venv/bin/rsi-eval check --config examples/evaluate.json
.venv/bin/rsi-eval run --config examples/evaluate.json \
  --oracle --output runs/oracle-tb20
.venv/bin/rsi-eval run --config examples/evaluate.json \
  --output runs/codex-tb20
```

The oracle command runs the task's declared solution to validate its environment and verifier. It does not call Codex. Original Harbor tasks use their bundled solutions. Materialized tasks document whether they use a reviewed reference program, a dataset answer, or a published initial program. Binary benchmark references require reward 1; DROP requires EM/F1 1. Frontier-Eng's initial programs require a valid finite native score, which need not be 1. An agent evaluation that finishes with reward 0 can be a valid measured failure.

Use a new output directory for each evaluation. To recover an interrupted evaluation, explicitly run `rsi-eval resume --output runs/codex-tb20`. It retries missing task repetitions from the recorded candidate and preserves completed measurements and prior attempts. There are no automatic retries or result caching. Docker and Harbor may cache downloaded images and immutable task files. See [recovery details](docs/methods.md#recover-work).

## Call from your own RSI method

```python
from pathlib import Path
from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate

target = TargetSpec(
    artifact_dir=Path("my_candidate"),
    entrypoint="rsi_eval.harbor_codex:Agent",
    model="gpt-6-astra",
    config={
        "codex_auth_dir": str(Path.home() / ".codex-rsi-eval"),
        "codex_version": "0.162.0-alpha.2",
        "reasoning_effort": "medium",
    },
)
result = evaluate(
    target=target,
    benchmark=BenchmarkSelection(
        "terminal-bench", "2.1", task_ids=("extract-elf",)
    ),
    options=RunOptions(repetitions=1, concurrency=1),
    output_dir=Path("runs/candidate-001"),
)
if result.status != "completed":
    raise RuntimeError(result.error or result.report_path)
feedback = [(trial.task_id, trial.reward) for trial in result.trials]
```

Copy `templates/codex_agent` to create the candidate directory. Change its Python hooks, prompt or tools, then submit the resulting artifact. The built-in target runs one Codex session per task and records the exact candidate hash it loaded. Candidate Python hooks execute in the trusted Harbor worker process; commands submitted through the environment execute inside the task container. This is not a sandbox for hostile improvement code.

The Codex target loads MCP servers explicitly declared by the task and records
them with the candidate evidence. HTTP streaming and stdio are supported;
legacy SSE fails explicitly. Task tools are authorized for noninteractive use
inside the benchmark environment. The target ignores host Codex configuration.
Set `target.config.web_search_mode` to `live` or `cached` when the selected
benchmark and agent protocol permit it; the default is `disabled`.

You can also use an existing Harbor agent as `entrypoint="your_module:YourAgent"`, with its arguments in `config`. That path can implement other control loops, models or tool protocols. The bundled Codex seed's single-session contract does not restrict the core SDK.

For a custom agent that loads candidate files, accept an `artifact_dir` constructor
argument and include that key in `target.config`. The SDK replaces its value with
the immutable candidate snapshot and checks the path in the returned trial
configuration. Load code and configuration from that directory so the executed
artifact matches the hash in the report. Keep generated logs and mutable runtime
state outside the snapshot.

Your method decides how to generate candidates and consume feedback. There is no required `propose`, `select`, memory format or optimizer lifecycle. See [a minimal user-owned loop](examples/custom_method.py).

To select a full dataset, omit `task_ids`. To use a fixed task manifest, pass `manifest=Path("tasks.json")`, where the file contains a JSON list of task IDs or `{"task_ids": [...]}`. Unknown, duplicate or empty selections fail before execution. Change benchmark selections between calls to choose your own evolve/test arrangement.

For tasks that need a specific execution environment, pass an explicit
`RunOptions(benchmark_runtime={...})` or set `options.benchmark_runtime` in JSON.
The [EngDesign](docs/engdesign.md) and [Frontier](docs/frontier-eng.md) guides list
supported settings. `check` reports prerequisites for every selected task before
execution; `preview` records settings without downloading assets or calling a
model. Configuration-file asset paths, including per-task overrides, resolve
relative to that file. Missing requirements never silently filter the selection.

## Run the bundled methods

Save the model, dedicated login location and CLI version once. This probes the
CLI version and makes no model call:

```sh
rsi-eval configure --model gpt-6-astra \
  --auth-dir "$HOME/.codex-rsi-eval" --output profile.json
rsi-eval method list
rsi-eval method prepare --method meta-harness \
  --profile profile.json --experiment runs/meta-example
rsi-eval method check --experiment runs/meta-example
rsi-eval method run --experiment runs/meta-example
```

Use the `rsi-eval` executable from your core environment (for example,
`.venv/bin/rsi-eval`). Choose a model available to your account; `--codex PATH`
supports a CLI executable outside `PATH`. Keep the Docker environment from the
setup section active. `prepare` creates a separate dependency environment,
obtains pinned upstream code, and freezes the configuration, SDK, bridge and
seed. It downloads packages and source but calls no model. `check` probes the
recorded prerequisites; `run` makes the model calls.

Change `--method` to `rrsi`, `ahe` or `harnessx`. Pass `--config experiment.json`
to choose your own tasks and method settings. Use `--source LOCAL_CLONE` and
`--environment EXISTING_VENV --no-install` to reuse prepared dependencies.
The wheel contains everything needed for this command layer; a repository
checkout is optional. See the [method guide](docs/methods.md) for configuration,
explicit scoring policies, recovery and native commands.

All four methods support `rsi-eval method test --experiment EXPERIMENT --output
TEST_RUN` for independent evaluation of their measured selected artifact. Add
`--selection selections.json` to choose other benchmarks without rerunning
evolution. The command checks all selected benchmarks before starting any
evaluation and records the checks in `preflight.json`. New recorded experiments
also support verified export/import and
path rebinding. See the [method guide](docs/methods.md#move-a-recorded-experiment).
Optional [runtime snapshots](docs/runtime-snapshots.md) preserve prepared task
files and observed Docker images; platform-specific host dependency locks are
provided for reproducible method setup.

| Method | Preserved workflow | Detailed guide |
| --- | --- | --- |
| Meta-Harness | Candidate generation, smoke and scored evaluation, frontier and history feedback | [Meta-Harness](integrations/meta_harness/README.md) |
| RRSI | Analyst, proposer, critic, edit accounting, repair and constrained selection | [RRSI](integrations/rrsi/README.md) |
| AHE | Scored execution, debugger reports, edit predictions, delayed attribution and best-candidate history | [AHE](docs/ahe.md) |
| HarnessX | Native configuration, processors and tools, MetaAgent evolution, evidence and smoke gates, journal history | [HarnessX](docs/harnessx.md) |

Meta-Harness, RRSI and AHE evolve the editable Codex wrapper. AHE uses an adapted
public debugger. HarnessX retains its native agent runtime; its reusable bridge
connects task-provided MCP services. It does not implement the paper's full
AEGIS procedure or model-weight training. Each method preserves its default
selection behavior unless the user supplies an explicit policy.

See the [HarnessX guide](docs/harnessx.md) for configuration, tool integration,
supported inputs and limitations.

## Results and failures

Each SDK run writes:

| Path | Contents |
| --- | --- |
| `resolved.json` | Exact tasks, hashes, model, options and source candidate identity |
| `candidate/` | The candidate snapshot actually submitted |
| `harbor.json`, `command.json` | Native configuration and CLI invocation |
| `harbor.log` | Backend process output |
| `native/eval/` | Unmodified Harbor job, trials, verifier results and trajectories |
| `result.json` | Unified status, native metrics, per-task feedback and usage |

Native verifier rewards keep their original meaning. Separate benchmark calls retain separate scores. Harbor 0.18 groups explicit tasks under its default `adhoc` Mean metric; the report retains the full benchmark provenance separately. That native mean is not an official suite score for benchmarks with different task objectives.

LiveCodeBench also reports `metrics.benchmark_metrics.pass_at_k`; the default is
pass@1 across independent repetitions. MGSM adds per-language accuracy. These
summaries accompany the native results and remain unknown for incomplete runs.
EngDesign preserves its original numerical score separately from the pass flag.
DROP reports exact match and F1 on a 0-to-1 scale, both overall and by source
split. Its per-trial reward is F1. Frontier-Eng preserves continuous native scores,
validity and per-task objectives. It does not average unrelated engineering scores
or invent a Medal Score. Calling methods choose their own selection objective.
Reasoning and MCE add accuracy by their declared split/subject. Rubric profiles
retain continuous rewards and add equal-task means with domain or split
breakdowns. Combined training/test selections are never labeled official test
scores. Missing trials leave aggregate scores unknown.

The methods preserve their native objectives by default. For other objectives,
set `method_policy.score` to a threshold, a bounded linear mapping, or a Python
callback. Set `method_policy.selection` to a Python callback to choose among
eligible measured candidates. This changes the declared method variant and
keeps raw benchmark rewards intact. Native validity, critic, smoke and cost
gates still apply. See [policy examples and method constraints](docs/methods.md#choose-an-objective).

`completed` means all requested trials have completed evidence, including valid zero rewards. A timed-out or failed agent can still have a real score when the native verifier completed; the report retains both the error status and that score. `partial` and `failed` retain available results and explain missing work. Authentication, quota and infrastructure failures never become fabricated policy scores. Unknown token counts stay unknown. Subscription dollar cost is `null`, not an API-rate estimate.

Role and proposer logs record elapsed time, known token usage and
`codex_cli_calls`. That field counts CLI invocations, not the model requests
inside an agent session. SDK benchmark usage is separate from method-role usage.

Authentication or quota failures stop the affected call without switching
providers. After resolving the cause, use `rsi-eval method resume --experiment
runs/meta-example` for a managed method or `rsi-eval resume --output RUN_DIR`
for a core evaluation. Recovery validates recorded identities and retains prior
attempts. Method-specific rollback can repeat an interrupted round and incur
additional calls. Old experiments without the new checkpoint metadata are not
silently upgraded. Neither retry nor score improvement is implicit.

## Extend and verify

For another Harbor-compatible benchmark, register a spec with `register_benchmark(spec)`. It contains `name`, `version`, `dataset_ref` and `tasks`; each task needs an ID and either a package name plus immutable SHA-256 ref, or a Git URL, full commit and task path. See [the original catalog](src/rsi_eval/data/catalog.json) and [additional Harbor catalogs](src/rsi_eval/data/catalog_harbor_expansion.json). Registration affects the current process; bundle a catalog to expose it through separate CLI processes.

Data-backed benchmarks use a small materializer that downloads checksummed
sources and emits standard Harbor tasks. Add one module, register its name in
`materializers`, and bundle its task IDs and protocol in `data/catalog_*.json`.
Keep private answers out of the image and agent instruction. Reuse the original
grader, preserve raw metrics, and declare any changed interaction protocol.
Use an existing aggregation or implement its native reporting rule. Registering
task names alone does not supply missing environments or evaluators.

Run the offline suite in a test environment containing the optional Harbor dependency:

```sh
.venv-harbor/bin/python -m pip install -e '.[test,harbor]'
.venv-harbor/bin/python -m pytest -q
```

Tests cover candidate identity, CLI/SDK agreement, native result semantics,
authentication lifecycle, upstream method loops with deterministic external
fixtures, and subprocess/worktree behavior. Adapter checks cover official
graders, hidden-test placement, source checksums and benchmark-specific metrics.
