# EngDesign-Open

`engdesign-open@open-c8ec338-v1` exposes all **67 released EngDesign-Open tasks**
from [the official repository](https://github.com/AGI4Engineering/EngDesign/tree/c8ec338cc5f846d792d93728b00c9448111139e5).
Every task has its original output schema, grader, required inputs, private
reference files, and an executable runtime. The catalog pins each downloaded
file to SHA-256 and commit `c8ec338cc5f846d792d93728b00c9448111139e5`.

The two-task `open-subset-c8ec338-v1` profile remains available as a **legacy
subset** so older experiment configurations retain their meaning. Use the new
profile for full coverage. A small acceptance run does not restrict which tasks
a researcher can select.

| Runtime | Tasks | Included dependencies |
| --- | ---: | --- |
| Python scientific and submitted-code graders | 52 | NumPy, SciPy, OpenCV, scikit-image, scikit-learn, pandas, control, CVXOPT, SymPy, FFmpeg and task helpers |
| Icarus Verilog | 9 | `NS_PA_SS_02` through `NS_PA_SS_10`; original SystemVerilog testbenches |
| Octave | 4 | `LX_02`, `ZC_02`, `ZC_03`, `Ziheng_02`; Oct2Py and Octave control package |
| Webots R2025a | 2 | `XZ_03`, `XZ_04`; worlds, controllers, PROTOs and textures |

No task uses a model judge or requires an API key. A Codex target uses the
subscription setup in the main README. The verifier has no subscription login
or other model credentials. Scientific packages stay in the generated Docker
images; the core SDK still has no runtime dependencies.

## Run a selection

Start with [the example configuration](../examples/benchmarks/engdesign.json).
Its two task IDs are a small acceptance selection. From the repository root:

```bash
.venv/bin/python -m rsi_eval check --config examples/benchmarks/engdesign.json
.venv/bin/python -m rsi_eval preview --config examples/benchmarks/engdesign.json
.venv/bin/python -m rsi_eval run --config examples/benchmarks/engdesign.json --output runs/engdesign-selected
```

Use a fresh output directory for each new evaluation. Set `benchmark.task_ids` to
any desired IDs, or omit `task_ids` to select all 67. For example:

```json
{
  "name": "engdesign-open",
  "version": "open-c8ec338-v1",
  "task_ids": ["AB_02", "NS_PA_SS_02", "LX_02", "XZ_03"]
}
```

The same selection works through `evaluate()` and in a user's own improvement
loop. The framework assigns no evolve or test role to these tasks. Full model
evaluation is available, but has not been run as an acceptance requirement.

`--oracle` is available only for `AB_01` and `XY_01`. These are adapter reference
solutions for checking the verifier. Other tasks remain selectable with a normal
agent; a full-set oracle request is rejected instead of silently skipping them.

## Runtime and input contract

The agent receives the original `LLM_prompt.txt`, `output_structure.py`, and the
listed public inputs. It writes a JSON object to `/workspace/output.json`.
Private grading code, references and model credentials stay in different
containers. The verifier validates the submission with the original
`Response_structure`, then calls the unchanged `evaluate_llm_response`.

There are two explicit input/runtime differences from the upstream model caller:

- For `AB_02` and `AB_03`, the upstream generic image collector also sends the
  ground-truth mask or visualization to the model. This adapter supplies the
  task input images to the agent and keeps those reference images in the grader.
- For `XZ_03` and `XZ_04`, two world-file PROTO URLs resolve to local copies of
  the same Webots R2025a assets. This lets the full world load in an offline
  verifier. The adapter records a separate patch and the original and runtime
  hashes. It does not change world geometry, controller code or scoring rules.

The Webots assets come from
[Cyberbotics commit `c6793d8`](https://github.com/cyberbotics/webots/tree/c6793d8f7230a311c4bc2a3101d9f1a8bc0aa01b)
and retain their [Webots assets license](https://cyberbotics.com/webots_assets_license).
They are downloaded during task preparation and used only with Webots.

Docker builds need network access. During grading, the sidecar has an internal
network, no host mounts or Docker socket, and no model authentication files.
Each invocation runs as an unprivileged worker with a fresh source copy. A
startup probe checks hashes, native imports and required executables before
Harbor starts the agent. Webots also receives a display and software rendering.
The checked configuration ran on an Apple Silicon Docker VM without a GPU.

The default scientific image is `python:3.12.9-slim-bookworm`, using the host's
ARM64 or AMD64 architecture. The default Webots image is
`cyberbotics/webots:R2025a-ubuntu22.04`, using `linux/amd64`, including on Apple
Silicon. Leave `DOCKER_DEFAULT_PLATFORM` unset when a selection uses both
architectures. Optional settings go under `options.benchmark_runtime`:

```json
{
  "verifier_timeout_sec": 840,
  "runtime_image": "python:3.12.9-slim-bookworm",
  "webots_image": "cyberbotics/webots:R2025a-ubuntu22.04"
}
```

`runtime_image` must provide Debian-compatible package installation and Python
3.12. `webots_image` must provide a compatible Webots installation at
`/usr/local/webots` and Python 3. An explicit `platform` accepts `linux/arm64` or
`linux/amd64`; an ARM64 Webots selection needs a user-supplied ARM64 image. These
options do not accept environment secrets or judge credentials.

The agent budget is 600 seconds. Harbor allows 900 seconds for verification;
the native worker budget defaults to 840 seconds and is configurable from 30
to 840. The sidecar is limited to 2 CPUs, 4 GiB of memory, 256 processes and
1 GiB of temporary storage. Package versions are pinned in generated Dockerfiles;
OS repositories and transitive packages can still change between builds. To
preserve a new evaluation's resolved dependencies, set
`options.retain_runtime_images` to `true` before running it. After every selected
task environment has started, capture its prepared files and observed service
images:

```sh
rsi-eval runtime capture --run runs/engdesign-selected --output snapshots/engdesign-selected
rsi-eval runtime check --snapshot snapshots/engdesign-selected
```

For a later evaluation, set `options.runtime_snapshot` to the snapshot directory.
Replay uses those exact task files and image content IDs without rebuilding.
This includes the grader sidecar's installed packages and copied assets. The
archive requires recorded image evidence and images still available in Docker;
it cannot reconstruct an old run's missing dependencies. CPU architecture and
other runtime requirements still apply. See [runtime snapshots](runtime-snapshots.md)
for restore commands, retention and compatibility limits.

To continue an interrupted evaluation with the same candidate and configuration:

```sh
rsi-eval resume --output runs/engdesign-selected
```

Recovery validates the saved execution identity and retries unfinished task
repetitions while preserving completed measurements and prior artifacts. Older
runs without the required records need a separate evaluation. See
[the shared recovery guide](methods.md#recover-work).

## Results and native limitations

`reward.json` uses the original pass flag as `reward`, converted to 0 or 1.
`native_score` and `confidence` preserve known numeric values. Their scales vary
by task; confidence is not a normalization denominator. `verdict.json` preserves
the native four-field return, detailed feedback, source hashes, submission hash,
subprocess evidence and whether schema validation and native execution completed.
Do not compare native scores across tasks as if they shared a unit.

Missing, malformed, nonfinite or oversized JSON is `invalid_submission` with
reward zero and an unknown native score. The transport limit is 4 MiB. Some
native graders return `(False, details, None, None)` for a candidate execution
error. That is a failed native verdict: reward remains zero, while the unknown
score and confidence stay null in the detailed report and are omitted from
Harbor's numeric reward object. Setup failures, failed dependency probes,
checksum mismatches, timeouts and malformed grader returns produce no reward.
Known Webots launch failures are treated as verifier errors. The adapter clears
old reward files before verification.

The released graders have limits that this profile preserves:

- `AB_01` uses pixel centers at index plus 0.5; its prose and reference text
  disagree on the ROI boundary interpretation.
- `XY_01` scores the configuration supplied by the candidate rather than
  separately enforcing the prompt's requested tetromino and rotation.
- The nine `NS_PA_SS_*` graders look for failure markers after simulation and
  do not check the simulator return code or require every success marker.
- `YX_01` accepts the submitted tolerance values when checking path lengths.
- `AB_02`, `AB_03`, `CY_03`, `WJ_01` and `XW_01` through `XW_04` execute submitted
  Python in the scorer's interpreter. That code can inspect or alter scoring
  state. The separate container protects agent credentials and other trials;
  it does **not** make these native graders resistant to reward hacking.

Broad exception handlers in upstream code can also obscure the origin of an
error. Startup checks catch the declared environment requirements, while the
native return remains available for inspection. Changing these scoring rules
would define another protocol, so this profile does not silently repair them.

## Validation

The [per-task validation record](data/engdesign-native-validation.json) covers all
67 tasks. Each was replayed with a response already published in the pinned
upstream logs, inside a credential-free container with network access disabled.
Every startup probe, schema validation and native grader invocation completed.
The record includes input-log hashes, runtime image IDs, numeric outputs and
execution evidence hashes. Webots tracing additionally confirmed controller
execution and complete resource loading. These are environment and interface
checks with **zero model calls**, not new benchmark performance measurements.

The full profile also completed Harbor oracle trials for `AB_01` and `XY_01`,
and a real Codex trial for `XY_01`. The Codex trial returned native pass, score
100 and confidence 100 after the startup probe. Full-set model evaluation and
paper-score reproduction were not run. See [the coverage report](full-coverage.md)
for the experiment paths and the status of other benchmarks.

Offline tests exercise the original passing and partial-credit graders, native
candidate execution failure with null scores, missing results, JSON overflow,
checksums, complete selection and resource separation:

```bash
.venv/bin/python -m pytest -q tests/test_engdesign.py tests/test_engdesign_full.py
```

The independent scientific environment ran 32 tests successfully; one test
requiring the Harbor SDK was skipped there. Tests requiring optional scientific
packages skip when those packages are absent from the test interpreter.

For a fresh no-model replay, check out the source commit above and prepare an
audit directory with the supplied maintenance script:

```bash
.venv/bin/python scripts/engdesign_native_audit.py prepare \
  --upstream /path/to/EngDesign --output .runtime/engdesign-audit

docker build -t engdesign-audit-scientific .runtime/engdesign-audit/scientific-image
docker build --platform linux/amd64 -t engdesign-audit-webots .runtime/engdesign-audit/webots-image

mkdir -p .runtime/engdesign-audit/results-scientific .runtime/engdesign-audit/results-webots
docker run --rm --network none --cpus 2 --memory 4g --pids-limit 256 \
  --mount "type=bind,src=$PWD/.runtime/engdesign-audit,dst=/audit,readonly" \
  --mount "type=bind,src=$PWD/.runtime/engdesign-audit/results-scientific,dst=/results" \
  engdesign-audit-scientific python /audit/audit.py replay --family scientific

docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4g --pids-limit 256 \
  --mount "type=bind,src=$PWD/.runtime/engdesign-audit,dst=/audit,readonly" \
  --mount "type=bind,src=$PWD/.runtime/engdesign-audit/results-webots,dst=/results" \
  engdesign-audit-webots python3 /audit/audit.py replay --family webots
```

The script reads saved response fields as data without evaluating their Python
representations. Candidate code runs only inside the Docker replay. A replayed
response may pass or fail a task; the audit requires a completed native verdict,
not a positive score. The published responses are fixtures, not official oracles.
