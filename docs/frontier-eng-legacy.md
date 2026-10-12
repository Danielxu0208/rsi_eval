# Frontier-Eng CPU subset

`frontier-eng@cpu-subset-bafa7d5-v1` exposes two engineering optimization tasks through the existing `evaluate()` interface. Users choose these tasks for evolution, testing, or both. The adapter leaves the search method outside the SDK.

The source is the official [Frontier-Engineering repository](https://github.com/Einsia/Frontier-Engineering/tree/bafa7d53162a3b1f128fec396bbde22fe972bf24), pinned at `bafa7d53162a3b1f128fec396bbde22fe972bf24`. Every downloaded file has a SHA-256 digest in the catalog. The package contains our adapter and source manifest; it fetches upstream code at runtime.

## Supported tasks

| Task ID | Native objective | Direction | Native invalid value |
|---|---|---|---|
| `InventoryOptimization/disruption_eoqd` | Weighted inventory cost, service, risk and capital score | Maximize | `0.0`, with `valid=0` |
| `Robotics/PIDTuning` | Geometric mean of inverse time-weighted tracking error across four quadrotor scenarios | Maximize | `-1e18`, with `valid=0`, from the native unified wrapper |

These are 2 of the 47 tasks in the released v1 matrix, excluding its additional EngDesign entry. This configuration is a reviewed subset. It is neither the full v1 collection nor the 10-task v1-lite suite. Unsupported IDs and the coverage boundary remain in each resolved report. Selecting every supported task yields `full_supported_subset`.

The pinned `evaluate.py`, `evaluator.py`, reference implementation, simulator, configuration and native sandbox helper retain their original bytes. NumPy `2.2.6`, SciPy `1.15.3` and Stockpyl `1.0.2` run inside Docker. The core Python package has no new dependency.

## Run

Start the documented Docker environment and log in with the existing dedicated Codex subscription state. Preview requires neither Docker nor a model call:

```sh
rsi-eval preview --config examples/benchmarks/frontier-eng.json
```

Validate the published baseline before evaluating an agent:

```sh
rsi-eval run --config examples/benchmarks/frontier-eng.json --oracle --output runs/frontier-reference
rsi-eval run --config examples/benchmarks/frontier-eng.json --output runs/frontier-codex
```

The baseline checks the source, simulation, isolation and metric transport. Its native score remains in the report.

The example runs both tasks once, serially. Larger repetition counts remain a user choice. The example's 3,600-second outer job limit includes builds and both trials; increase it for larger runs.

## Feedback and budget

Each trial starts from the upstream initial program. The agent edits the allowed source file and submits it to a separate grader container:

```sh
# Initial baseline, current best and remaining calls:
python -I /opt/frontier_runtime.py feedback

# Inventory candidate:
python -I /opt/frontier_runtime.py feedback /workspace/baseline/init.py

# PID candidate:
python -I /opt/frontier_runtime.py feedback /workspace/scripts/init.py
```

The budget follows the released matrix's maximum of 100 proposed candidates. At most 99 feedback calls are available during the agent session. Final evaluation consumes the remaining candidate slot. The initial baseline is separate. Each evaluated candidate, including a rejected one, consumes a call. Requests beyond the cap do not run a grader.

The broker keeps source snapshots, hashes, native metrics and failures outside the agent filesystem. It selects the best feasible result across the initial program and every evaluated proposal. An earlier design can win even when the final source is worse. The instruction asks the agent to use feedback after a modification, and the report records whether it did so.

The final verifier also reruns the selected source as a diagnostic. This extra evaluation supplies no optimization feedback and does not consume a proposal slot. Its score, feasibility and difference from the selected result remain in `confirmation`. Randomness in an optimizer can change that diagnostic. The discovery reward remains the best value actually observed within budget.

The agent receives native scores and candidate diagnostics. It does not receive the Inventory reference policy or the private evaluator artifacts during optimization. Those records become available in the final experiment report.

## Runtime boundary

The main container holds the agent, task instructions and initial code. A separate grader sidecar owns the evaluator, baseline, call counter, history and private final-verification token. It receives source text over a private Docker network. No grader port is published to the host; no host directory, credential file or Docker socket is mounted into that sidecar.

The scorer runs each candidate through upstream bubblewrap with its own process namespace. Inventory already restricts filesystem access to staged inputs and the Python runtime. The adapter applies the same restriction to PID's compatibility mode by wrapping the native namespace constructor. Candidate code cannot access the broker state or scorer files. Frozen source checks before and after evaluation detect changes.

Nested bubblewrap requires the grader service to use `SYS_ADMIN`, `seccomp=unconfined`, `apparmor=unconfined` and `systempaths=unconfined`. These settings apply to the isolated grader service. The service does not use Docker privileged mode. A failed namespace preflight stops scoring and produces a verifier error.

The grader defaults to the architecture of the machine running RSI Eval. On ARM hosts, it uses native `linux/arm64`. Clear a conflicting `DOCKER_DEFAULT_PLATFORM` so Docker Compose builds the intended architecture. A remote Docker daemon with another architecture requires an explicit setting:

```sh
unset DOCKER_DEFAULT_PLATFORM
export RSI_EVAL_FRONTIER_PLATFORM=linux/amd64
```

For an ARM host with an ARM Docker daemon, use `unset DOCKER_DEFAULT_PLATFORM` and leave `RSI_EVAL_FRONTIER_PLATFORM` unset. The materializer rejects conflicting settings before downloading sources.

The chosen platform appears in `task.toml` and `provenance.json`. Docker source contexts have unique content-derived names. Each materialization also creates a private verification token, so generated task hashes differ across runs. Compare `stable_source_sha256` and `protocol_sha256` when checking whether two runs used the same source and protocol.

## Results and experimental differences

`reward` and `native_score` equal the selected native `combined_score`. `valid` remains separate from score. A negative sentinel is a graded invalid candidate, not an infrastructure failure. The normalized summary keeps results per task and never averages Inventory utility with inverse tracking error. It does not calculate medal scores.

`/logs/verifier/frontier.json` contains the complete sequence, candidate sources, hashes, outputs, best index, actual calls and diagnostic rerun. Harbor's original trial files remain available alongside the unified report. Missing dependencies, broken namespaces, corrupted frozen files and unreadable native metrics receive no invented score.

This is the explicitly named `harbor-feedback-100-v1` protocol. The native task definitions and numerical graders are preserved. Execution uses a Codex coding-agent session with a 600-second wall-clock cap rather than the upstream OpenEvolve proposer loop. That cap can stop a run before its 100-call allowance is exhausted. The separate feedback broker and stricter PID namespace are also adapter changes.

The upstream tree has no root license file at this revision. RSI Eval does not redistribute those sources in its wheel or offline fixtures. Users obtain them directly from the pinned public repository and should consult its terms before redistributing benchmark artifacts.
