# APEX-Agents 1.1.1

`apex-agents@1.1.1` contains all 240 tasks in the official
[Harbor release](https://hub.harborframework.com/datasets/mercor/apex-agents-1-1/latest):
80 investment banking, 80 law and 80 management consulting tasks across 31 worlds.
The catalog pins the dataset digest, every package digest, archive SHA256 and
source-file hashes. It never resolves `latest` during evaluation.

This is the [1.1 release](https://www.mercor.com/blog/introducing-apex-agents-1-1/),
not the original 480-task 1.0 dataset. That older Hugging Face distribution has
access restrictions and is not claimed here. Publisher terms restrict APEX to
evaluation, not training, fine-tuning or parameter fitting. Review those terms
before using it for method development; the SDK does not assign benchmark roles.

## Execution and scoring

The adapter retains the original world MCP server, tool configuration, agent
sandbox, separate verifier and native judge. The shared agent/verifier images
and all 31 world images are pinned by digest. The 34 file-output tasks retain their world collect hooks and
initial/final snapshots; the other 206 tasks grade final answers. The native
grader converts the agent's ATIF trajectory and raises an error when required
artifacts or a grade are missing.

The judge is `vercel_ai_gateway/deepseek/deepseek-v4-flash-0731`, with the original
temperature, GEPA prompt and rubric definitions. The SDK preserves its raw
`reward`, per-criterion outputs and grade details, and reports an equal-task
mean with domain breakdowns.

The Codex target receives the task's streamable HTTP server through explicit
CLI configuration, including a required-startup check and authorization for
noninteractive benchmark tools. The Codex variant allows 120 seconds for MCP
startup and 300 seconds per tool call, within the native whole-task timeout.
It does not discover host MCP servers. See the
[official Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
This target is a Codex agent variant: it retains task timeouts and grading, but
does not recreate the reference agent's 100-turn truncation controller. Use the
original/custom Harbor target when that controller is part of your experiment.

Three runtime changes are recorded with each prepared task: a Dockerfile
fallback references the unchanged prebuilt image, and the judge gateway key is
removed from the agent environment. The image's root-owned service cache folders
otherwise prevent eight tool services from starting; the adapter sets
`UV_NO_CACHE=1` and `PYTHONDONTWRITEBYTECODE=1` for those services. It preserves
their users, commands, tool permissions and application data, and records the
original and modified configuration hashes. Native grader files, task
instructions, oracle, snapshots and Compose file remain unchanged.

## Run

Complete the main README's Docker and dedicated Codex setup. On Apple Silicon,
use an amd64-capable runtime with Rosetta or equivalent emulation. World images
and the shared agent and verifier require several GB of downloads and more space
during unpacking. Worlds use their own pinned images, sharing layers where
available. Provision disk for the selected images, logs and snapshots.

Provide the native judge credential in the host environment, or place only
`VERCEL_AI_GATEWAY_API_KEY=...` in a private file outside the repository and set
`options.benchmark_runtime.judge_env_file` to its path. Credential contents are
injected into the verifier's environment, not run JSON or the agent container.
Codex subscription login does not supply this judge key. The profile fixes the
judge model; inherited `GRADING_MODEL` cannot silently change it.

```sh
source scripts/mac-env.sh
export DOCKER_DEFAULT_PLATFORM=linux/amd64
.venv/bin/rsi-eval preview --config examples/benchmarks/apex-agents.json
.venv/bin/rsi-eval check --config examples/benchmarks/apex-agents.json
.venv/bin/rsi-eval run --config examples/benchmarks/apex-agents.json \
  --output runs/apex-candidate
```

Omit `task_ids` to select all 240 tasks. Increase/remove the whole-job timeout
for a larger selection. The original oracle is available through `--oracle`,
but even an oracle's answer needs the paid native judge. Missing credentials
stop evaluation before a target call; judge failure is not a measured zero.
