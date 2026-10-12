# HarnessX public evolution integration

This optional integration runs the public HarnessX TB2 evolution workflow with a Codex subscription. It pins [Darwin-Agent/HarnessX](https://github.com/Darwin-Agent/HarnessX) at `915520584c92cc35ed24aef1af17eec1d60c0b01`.

The implementation preserves `MetaAgent.evolve`, its analysis and journal instructions, generated tools and processors, contract checks, novelty and evidence gates, and synthetic replay. A custom Harbor agent loads each candidate's native `HarnessConfig` and executes the HarnessX run loop. Its sandbox provider sends commands and file operations into the Harbor task environment.

Codex supplies structured model responses. Native tools with fixed named arguments use JSON objects, so commands containing quotes and newlines do not require a second JSON-encoding layer. The adapter recognizes the pinned native tools by exact schema hashes. Generated tools use this object route when their schemas explicitly close each object. Custom open objects, references, optional nullable fields, and other complex schemas retain a JSON-string fallback that preserves extra keys and distinguishes omission from an explicit null. Both routes validate the response and the original native argument schema before creating any tool call. A serialization error receives one explicit model-visible retry, and both attempts count toward usage; malformed arguments are never repaired or executed silently. The prompt separates the local read-only Codex transport from the external task workspace: Codex returns intents while HarnessX authorizes and executes any writes. Codex role mode and disabled local tools remain unchanged. HarnessX owns tool execution and processor hooks. This applies to the target, proposer, reflection workers, compaction, and synthetic smoke. The integration never switches to an API backend.

## Scope and selection

The pinned public TB2 recipe promotes the configuration that passes its post-flight gates. It does not compare candidate scores before promotion. This integration keeps that default and records the next benchmark score in the journal. An explicit method policy can add score mapping and candidate selection after those checks. The GAIA recipe's regression gate is not part of this public TB2 workflow.

This release covers harness evolution. It excludes model-weight training, reinforcement learning recipes, the gateway, and the browser UI.

The default seed is the upstream TB2 configuration, including its actual processors and Bash tool. Set `seed` to a native HarnessConfig YAML file to start from another runtime. The common evaluator accepts the same benchmark selections as other integrations. Task-provided MCP tools are available, including APEX's world service. Benchmark-specific GPU and judge-key requirements still apply.

## Task tools

The task bridge loads only MCP servers declared by Harbor. A persistent broker runs inside the trial container, preserving service hostnames, stdio commands and network rules. It supports stdio, streamable HTTP and SSE. The client discovers all pages of tool definitions and registers them in the native HarnessX runtime with names that cannot shadow builtins. Original argument schemas, errors and complete responses remain in the trial logs.

The task environment needs Python 3.10 or newer for the pinned MCP SDK. If `mcp==1.30.0` is already available, the broker uses it. Otherwise setup creates a separate per-trial environment with `uv` or `venv` and installs that version. This installation needs package access; it leaves the task's Python packages unchanged. Teardown closes the sessions and their stdio processes, including after failed startup or interrupted execution.

The Codex role transport sends text and native image attachments. It extracts
static PNG, JPEG and WebP blocks from HarnessX messages and tool results, then
passes their bytes through `codex exec --image`. Numbered references preserve
the original message, block order and tool-call ID. The model receives image
inputs rather than base64 text. Native task MCP images use the same route,
including embedded image resources and images saved separately by the native
MCP formatter. Raw responses and task-readable files remain available.

Custom tools can return native `ToolResult.content_blocks` with an image
`source` containing `type: base64`, `media_type` and `data`. Image data URLs are
also supported. Remote URLs and local-path content blocks are rejected instead
of being fetched from the host. Each call records attachment hashes, sizes,
relative paths and positions in `transport.json`; `images.json` records the
exact files supplied to Codex. Serialization retries retain the same images.

The transport limits each image to 20 MiB and the complete turn to 64 MiB.
HarnessX's original context rules also apply: its pinned tool-result budget is
200,000 characters, including base64. That native stage can remove oversized
media with an explicit context-budget notice before the provider sees it.
Use smaller images or an explicit task-side preprocessing tool when needed.
The adapter does not bypass these native limits.

Audio, video, GIF and animated PNG/WebP inputs are unsupported. Inputs that reach
the provider in these forms stop before a new model call and produce an explicit
error. The raw MCP record remains available. The installed Codex CLI and its
[official reference](https://learn.chatgpt.com/docs/cli/reference) expose image
attachments; this integration does not add an audio API or transcription
fallback. A successful tool connection or image input still needs the original
benchmark verifier and any judge credentials to produce a score.

## Explicit method policies

HarnessX accepts finite measured rewards without requiring binary outcomes. Its default promotion does not rank those values. To add a ranking rule, place `method_policy` in the experiment configuration:

```json
{
  "method_policy": {
    "variant": "drop-f1-threshold-selection",
    "score": {"kind": "threshold", "metric": "reward", "threshold": 0.8},
    "selection": {"kind": "python", "path": "objective.py", "function": "choose"}
  }
}
```

This example defines an explicit objective for DROP. It keeps raw F1 scores in the benchmark reports and maps them to threshold outcomes for selection. A score mapping by itself records the objective without changing native promotion. The following `objective.py` retains the incumbent unless the candidate has a higher mean mapped score:

```python
def choose(payload):
    rows = {row["id"]: row for row in payload["candidates"]}
    def mean(row):
        values = row["mapped_scores"]
        return sum(values) / len(values)
    if mean(rows["candidate"]) > mean(rows["incumbent"]):
        return "candidate"
    return "incumbent"
```

The callback receives `candidates`, `native_choice` and `context`. Each eligible row includes its candidate hash, complete raw `evaluation`, report path and `mapped_scores`. It returns `"candidate"`, `"incumbent"` or `null`; the last two retain the current artifact. The hook runs only after the proposed configuration passes the native post-flight gates and completes benchmark evaluation. It cannot rescue a failed gate or a missing result.

Shared mappings support `threshold`, bounded `linear` scaling and a Python `score` callback. Python policies are trusted standalone experiment code with standard-library imports. Their source and SHA-256 enter the frozen configuration; keep these files outside evolvable candidate directories. Reports label configured policies as method variants. Final tests retain raw scores and do not update the evolve objective.

## Setup

Run these commands from the RSI Eval repository root. The optional environment uses Python 3.12 and leaves the core SDK dependency-free.
Installed users can use `rsi-eval method prepare`, `check`, `run` and `resume` through the [shared method commands](../README.md). The commands below expose the same integration directly for source-based workflows.
The commands below require `uv` on `PATH`. A standard-library alternative is `python3.12 -m venv .runtime/harnessx-venv`, followed by `.runtime/harnessx-venv/bin/python -m pip install -r integrations/harnessx/requirements.txt`.

```bash
uv venv --python 3.12 .runtime/harnessx-venv
uv pip install --python .runtime/harnessx-venv/bin/python -r integrations/harnessx/requirements.txt
PYTHONPATH=src .runtime/harnessx-venv/bin/python -m integrations.harnessx prepare
```

`prepare` creates `.runtime/method-sources/harnessx`, checks out the fixed commit, and applies two compatibility changes:

- Remove the TB2 recipe's unsupported `require_evidence` constructor argument. The native validator's evidence gate remains enabled.
- Honor the caller's synthetic-smoke timeout. Upstream caps it at 20 seconds; the example permits 300 seconds for Codex CLI startup and response time.

Preparation records patch hashes and refuses unrelated tracked modifications. Supply `--source /path/to/existing/HarnessX` to clone an existing local repository without downloading it again.

Install the normal Codex CLI and complete ChatGPT login in a dedicated auth directory outside the repository and experiment output. Follow the subscription setup in the [main README](../README.md). The example uses `~/.codex-rsi-eval`; credentials remain there because model calls run on the host. Only task commands execute in containers.

Edit `integrations/harnessx/example.json` to set `codex.model`, `codex.expected_version`, `codex.auth_dir`, and any absolute executable paths for your machine. These values are explicit, verified configuration, not portable account defaults. Paths resolve relative to the configuration file.

## Check and run

```bash
PYTHONPATH=src .runtime/harnessx-venv/bin/python -m integrations.harnessx check integrations/harnessx/example.json
PYTHONPATH=src .runtime/harnessx-venv/bin/python -m integrations.harnessx run integrations/harnessx/example.json
```

`check` makes no model call and starts no container. It checks the source pin, installed dependencies, Codex login shape and CLI version, Harbor, Docker, Compose, and benchmark prerequisites. A successful check cannot guarantee remaining subscription quota.

The example runs a baseline and two evolution rounds on TB 2.0 `extract-elf` and `log-summary-date-ranges`, then evaluates the promoted candidate on `extract-elf` in TB 2.1. It writes `runs/harnessx-example`. Native analysis requires evidence from at least two distinct tasks before proposing a systemic change. A native no-op remains valid when the available feedback does not support a change.

Change `evolve` and `test` freely. `test` is a list of benchmark selections. A selection accepts `name`, `version`, `task_ids` or `manifest`, and optional `benchmark_runtime`. Omit both task selectors to use the whole fixed configuration. Per-selection runtime settings replace the default `run_options.benchmark_runtime`.

The subscription example requires `concurrency: 1`. The method uses step and wall-clock limits, since subscription dollar cost is unknown. Additional model calls from reflection workers, compaction and native smoke appear in the accounting records.

## Outputs and recovery

The configured output directory contains:

| Path | Contents |
| --- | --- |
| `result.json` | Status, source pin, model and CLI version, candidate hashes, gate decisions, raw report paths, and method usage |
| `candidates/` | Frozen native YAML and generated dependencies |
| `evolution/R*/` | Native candidate files, analysis, evidence, changeset, contract findings, and replay report |
| `learnings.md` | Native multi-round journal with observed task rewards |
| `feedback/` | Actual evaluation records and copied agent trajectories for the following round |
| `evaluations/` and `test/` | Unmodified Harbor results and unified SDK reports |
| `method-calls/` | Codex events and accounting for proposer, reflection, compaction, and smoke calls |

Each Harbor trial's `agent/task-mcp/` directory contains declared servers, discovered schemas, raw requests and responses, spill files, and lifecycle status when the task provides MCP tools. Explicit selection decisions and mapped scores appear in the method's `result.json`; benchmark reports remain unchanged.

The evaluator snapshots each bundle and rebinds its file references before loading. Modifying the original YAML, processor or prompt cannot change an already snapshotted trial. The runtime checkout remains fixed and verified; generated candidate code is trusted research code that runs in the host HarnessX process.

Bundles copy only explicitly referenced code and templates. Generated components must use `file://` references within the current candidate or its source directory. Symlinks, ambient custom imports, and plugin-directory references are rejected. Pinned `harnessx`, `benchmarks`, and `recipe` imports remain available. Custom modules should be self-contained or use those runtime APIs; arbitrary sibling files are not copied. The agent checks its expected candidate hash before and after execution.

Method and target usage stay separate. Public reports use actual CLI token counts and `cost_usd: null`. Native HarnessX journals retain the upstream arithmetic cost estimate for compatibility; those numbers are not subscription charges and do not drive this integration's budget or promotion policy.

Resume an interrupted run with the same configuration:

```bash
PYTHONPATH=src .runtime/harnessx-venv/bin/python -m integrations.harnessx run integrations/harnessx/example.json --resume
```

Completed rounds and final evaluations are reused only from this run's recorded state. An unfinished round starts a new attempt with its own directory. Failed calls retain their accounting. The launcher rejects a changed configuration during resume. A rejected native gate stops the run and preserves the findings; it does not silently replace the candidate or select another algorithm.

Authentication and quota failures stop subsequent model calls. If a wall-clock cancellation arrives during a Codex call, the provider waits for that bounded CLI invocation to settle before writing final accounting. Shutdown can therefore extend to the configured model timeout. Round reports distinguish a generated context file from an approved native Read or Bash call that returned its history. Bash evidence requires matching actual history content; merely echoing a path does not count.
