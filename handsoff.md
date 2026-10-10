# Linux NVIDIA/CUDA handoff

Use this guide to install RSI Eval on a Linux NVIDIA machine and finish the GPU
execution paths in Frontier-Eng. The goal is to run each remaining task through
its native evaluator and the unified SDK. Full-suite scores, improvements over
the baseline, and paper reproduction are outside this work.

The framework remains a lightweight SDK and CLI. Researchers choose their own
methods, evolve sets and test sets. Preserve each benchmark's scoring rules and
report environment failures separately from measured candidate failures. Follow
[AGENTS.md](AGENTS.md) when changing code or documentation.

## Scope and fixed inputs

The active configuration is `frontier-eng@v1-bafa7d5`, using upstream commit
`bafa7d53162a3b1f128fec396bbde22fe972bf24` and protocol
`harbor-native-unified-100-v2`. Its catalog contains 47 released v1 tasks plus the
native EngDesign aggregate. Four entries require GPU execution:

| Exact task ID | Grader Python | Native dependencies | Native evaluator timeout |
| --- | --- | --- | ---: |
| `KernelEngineering/FlashAttention` | 3.12.9 | `torch>=2.0`, `triton>=2.0`, NumPy; adapter adds `omegaconf==2.3.0` | 1,200 seconds |
| `KernelEngineering/MLA` | 3.12.9 | Full upstream requirements, including `torch==2.7.1`, `triton==3.3.1`, `numpy==2.3.5`; adapter adds `omegaconf==2.3.0` | 1,200 seconds |
| `KernelEngineering/TriMul` | 3.12.9 | Full upstream requirements, including `torch==2.7.1`, `triton==3.3.1`, `numpy==2.3.5`; adapter adds `omegaconf==2.3.0` | 1,200 seconds |
| `Aerodynamics/CarAerodynamicsSensing` | 3.10.18 | `numpy==1.24.4`, `torch==2.1.0`, `timm==1.0.21`, `einops==0.8.1`; adapter adds `omegaconf==2.3.0` and SciPy | 600 seconds |

All four recipes require `linux/amd64` and an NVIDIA-enabled Docker daemon.
Their native GPU graders still need execution verification. The source mapping
and dependency declarations do not establish that CUDA, assets or kernels work
on a particular machine.

The catalog does not establish a minimum GPU model or VRAM capacity. Check the
native tensor shapes, dtype support and memory use on the available device. A
successful `nvidia-smi` command alone cannot establish task compatibility. Keep
the original test cases and thresholds when diagnosing memory or kernel errors.

Harbor stays pinned to `0.18.0`. Host SDK and method environments use Python 3.12
or newer; use 3.12 for this setup. Task-specific Python and CUDA packages install
inside grader images, separately from the host environment. FlashAttention's
lower-bound requirements and Car's external assets are not fully immutable;
the runtime records the actual resolved packages and asset hashes.

## Start from a fresh Linux checkout

Install Git, Python 3.12 with venv support, and a working Docker Engine with
Buildx and the Compose plugin. Use the
[Docker installation guide](https://docs.docker.com/engine/install/) for the
Linux distribution. The generated GPU service uses `gpus: all`, which requires
[Docker Compose 2.30.0 or later](https://docs.docker.com/reference/compose-file/services/#gpus).

The GitHub repository is currently private. Authenticate an account with access
before cloning, using existing Git credentials or `gh auth login` followed by
`gh auth setup-git`. Keep tokens out of clone URLs and configuration files.

Run these commands from a normal user account that can access Docker:

```sh
git clone https://github.com/Danielxu0208/test_rsi_eval.git
cd test_rsi_eval
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
python3.12 -m venv .venv-harbor
.venv-harbor/bin/python -m pip install -e '.[harbor]'
export PATH="$PWD/.venv/bin:$PWD/.venv-harbor/bin:$PATH"
export RSI_EVAL_SOURCE_CACHE="$PWD/.runtime/source-cache"
unset DOCKER_DEFAULT_PLATFORM
harbor --version
rsi-eval list
rsi-eval status --benchmark frontier-eng@v1-bafa7d5
.venv/bin/python examples/offline_demo.py
```

The demo calls no model and starts no benchmark container. The source cache is
optional and verifies downloaded bytes against their declared hashes.

Create new environments and run directories on Linux. Do not copy a macOS venv,
Docker socket setting, or Colima environment script. Managed experiment import
requires the same host platform and package inventory; it does not migrate a
frozen macOS experiment into Linux. Reuse editable candidate source if needed,
then prepare a new experiment. See [method setup and recovery](docs/methods.md).

## Verify the NVIDIA container boundary

Install a Linux NVIDIA driver that supports the chosen GPU. Install the NVIDIA
Container Toolkit using its
[distribution-specific guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
For a standard system Docker daemon, configure the toolkit after installation:

```sh
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
nvidia-smi
docker info
docker compose version
docker buildx version
docker run --rm --runtime=nvidia --gpus all ubuntu:24.04 nvidia-smi
```

The configuration command changes the host Docker daemon settings, and restarting
Docker can interrupt other containers. The last command follows NVIDIA's
[sample workload](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html).
Use a dedicated machine or coordinate the restart with its administrator.

Use the system daemon for the initial debugging path. Rootless Docker has
different NVIDIA configuration and namespace restrictions; compatibility with
this grader's nested bubblewrap boundary is not established. The framework
needs Docker access from the same account that runs `rsi-eval`; do not run the
whole experiment as root to work around a missing socket permission.

Inspect the GPU driver against the CUDA runtime actually installed in each task
image. Car uses an older PyTorch pin than MLA and TriMul. Do not upgrade them
together or infer compatibility from a host-installed PyTorch. The image build
manifest records each environment independently.

## Create one-task configurations

The following script creates four oracle configurations under an ignored run
directory. They need no Codex login. Each selects exactly one task, one
repetition and one worker, making failures easier to localize.

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path

root = Path.cwd().resolve()
output = root / "runs/linux-gpu/configs"
output.mkdir(parents=True, exist_ok=True)
tasks = {
    "flashattention": "KernelEngineering/FlashAttention",
    "mla": "KernelEngineering/MLA",
    "trimul": "KernelEngineering/TriMul",
    "car": "Aerodynamics/CarAerodynamicsSensing",
}
for name, task in tasks.items():
    config = {
        "target": {
            "artifact_dir": str(root / "templates/codex_agent"),
            "entrypoint": "oracle",
            "config": {},
        },
        "benchmark": {
            "name": "frontier-eng",
            "version": "v1-bafa7d5",
            "task_ids": [task],
        },
        "options": {
            "harbor_executable": str(root / ".venv-harbor/bin/harbor"),
            "repetitions": 1,
            "concurrency": 1,
            "timeout": 21600,
            "benchmark_runtime": {
                "platform": "linux/amd64",
                "gpu": True,
                "cpus": 4,
                "memory_mb": 16384,
                "build_timeout_sec": 7200,
                "agent_timeout_sec": 3600,
            },
        },
        "output_dir": str(root / "runs/linux-gpu" / f"{name}-oracle-01"),
    }
    path = output / f"{name}-oracle.json"
    if path.exists():
        raise FileExistsError(path)
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(path)
PY
```

The CPU, RAM and outer time limits above are initial debugging settings, not
verified minimum requirements. `memory_mb` controls system RAM, not VRAM. The
one-hour agent limit applies to the later Codex run and explicitly bounds its
session. Native evaluator limits and the 100-proposal maximum remain unchanged.
Increase transport or build limits when the actual workload requires it, using
a new run directory after changing configuration.

## Check and run native references first

Resolve every configuration before spending time building images:

```sh
(
  set -e
  for task in flashattention mla trimul car; do
    rsi-eval preview --config "runs/linux-gpu/configs/$task-oracle.json" \
      > "runs/linux-gpu/configs/$task-preview.json"
    rsi-eval check --config "runs/linux-gpu/configs/$task-oracle.json"
  done
  .venv/bin/python -m pytest -q tests/test_frontier_eng.py \
    tests/test_frontier_eng_full.py
)
```

`preview` resolves task IDs and configuration without downloads. `check` probes
the declared requirements and local tools without launching a grader. Neither
proves that the GPU is visible inside the evaluator namespace.

Run the references one at a time. Begin with FlashAttention, then use the other
three commands after examining each result:

```sh
rsi-eval run --config runs/linux-gpu/configs/flashattention-oracle.json --oracle
rsi-eval run --config runs/linux-gpu/configs/mla-oracle.json --oracle
rsi-eval run --config runs/linux-gpu/configs/trimul-oracle.json --oracle
rsi-eval run --config runs/linux-gpu/configs/car-oracle.json --oracle
```

These runs can download large packages and assets but make no model calls.
Frontier's oracle evaluates the published initial program through the same
broker and native verifier. It is not an optimal solution. Inspect both run
status and native validity; a finished process alone is insufficient. If the
native seed is invalid, determine whether the grader completed correctly or
misclassified a missing dependency, CUDA error or timeout.

### Car assets

Car's image bootstrap obtains PhySense code, 101 pressure files, 101 velocity
files, a checkpoint and generated surface reference points. The upstream
PhySense clone has no fixed checkout revision, and its Google Drive downloads
have no declared content hashes. Network restrictions, quotas and upstream
changes can therefore block preparation independently of CUDA.

Let the default bootstrap run first. If a prepared asset snapshot is needed,
set `options.benchmark_runtime.assets_dir` in the Car configuration to a local
directory with this layout:

```text
frontier-assets/
  third_party/PhySense/
  benchmarks/Aerodynamics/CarAerodynamicsSensing/
    data/physense_car_data/pressure_files/
    data/physense_car_data/velocity_files/
    data/physense_car_ckpt/
    references/car_surface_points.npy
```

The adapter copies and hashes these files, then runs upstream asset preparation.
It rejects files that replace frozen benchmark source and rejects symlinks.
Read the pinned `scripts/bootstrap/assets_manifest.json` and
`scripts/bootstrap/fetch_task_assets.py` in the downloaded source for the exact
download and validation rules. Do not replace missing inputs with placeholders.

## Add the Codex subscription target

After native references work, install Codex for Linux using the
[official CLI instructions](https://learn.chatgpt.com/docs/codex/cli).
The npm installation route is:

```sh
npm install --global @openai/codex
codex --version
mkdir -p "$HOME/.codex-rsi-eval"
chmod 700 "$HOME/.codex-rsi-eval"
CODEX_HOME="$HOME/.codex-rsi-eval" codex \
  -c 'cli_auth_credentials_store="file"' login --device-auth
CODEX_HOME="$HOME/.codex-rsi-eval" codex login status
```

Complete the one-time sign-in from a browser. Device code sign-in must be enabled
for the account or workspace. Keep this dedicated authentication directory
outside the repository and use fresh Linux login state. See
[official authentication guidance](https://learn.chatgpt.com/docs/auth).

Choose a model available to the account. This example uses the repository's
configured model; replace it explicitly if needed. `configure` records the
installed CLI version without making an inference request:

```sh
rsi-eval configure --model gpt-6-astra \
  --auth-dir "$HOME/.codex-rsi-eval" \
  --output runs/linux-gpu/profile.json
```

The repository examples currently pin CLI `0.162.0-alpha.2`. The commands here
record the Linux installation's actual version and pass that exact version to
Harbor's container installer. A different CLI version needs its own smoke
check; do not claim compatibility solely because installation succeeds. There
is no automatic API fallback. These Frontier graders need no paid model judge.

Create target configurations from the checked oracle inputs:

```sh
(
set -e
.venv/bin/python - <<'PY'
import json
from pathlib import Path

root = Path.cwd().resolve()
settings = json.loads((root / "runs/linux-gpu/profile.json").read_text())["codex"]
for name in ("flashattention", "mla", "trimul", "car"):
    path = root / "runs/linux-gpu/configs" / f"{name}-oracle.json"
    config = json.loads(path.read_text())
    config["target"].update({
        "entrypoint": "rsi_eval.harbor_codex:Agent",
        "model": settings["model"],
        "config": {
            "codex_auth_dir": settings["auth_dir"],
            "codex_version": settings["expected_version"],
            "reasoning_effort": settings["reasoning_effort"],
        },
    })
    config["output_dir"] = str(root / "runs/linux-gpu" / f"{name}-codex-01")
    output = path.with_name(f"{name}-codex.json")
    if output.exists():
        raise FileExistsError(output)
    output.write_text(json.dumps(config, indent=2) + "\n")
PY
for task in flashattention mla trimul car; do
  rsi-eval check --config "runs/linux-gpu/configs/$task-codex.json"
done
rsi-eval run --config runs/linux-gpu/configs/flashattention-codex.json
)
```

This last command consumes subscription usage. Examine its feedback path and
final result before running the other three configurations. Keep concurrency at
one and serialize proposer and benchmark calls that share the login. Inspect
the retained candidate hash, actual modification, feedback response and native
metrics. A candidate may score worse or be rejected; it must still execute the
intended path. Do not require a score increase to accept the integration.

## Diagnose the failing boundary

For a run such as `runs/linux-gpu/flashattention-oracle-01`, inspect:

| Output | What it answers |
| --- | --- |
| `result.json` | Run and trial status, rewards, metrics and error classification |
| `resolved.json` | Actual task IDs, source identity, candidate and runtime configuration |
| `harbor.log` and `native/` | Build, container, agent and verifier failures |
| `tasks/tasks/*/environment/docker-compose.yaml` | Generated GPU allocation, platform and isolation settings |
| `tasks/tasks/*/environment/grader-*/Dockerfile` | Exact image build and dependency steps |
| `native/**/verifier/frontier.json` | Native metrics, feedback history, selected source and runtime evidence |

Use filesystem search for variable trial names. Generated files are diagnostic
artifacts; fixes belong in the source adapter so a fresh run can reproduce them.

Check these boundaries in order:

1. **Host driver and Docker allocation.** If the sample container cannot see the
   device, fix the driver/toolkit/daemon before changing the benchmark adapter.
2. **Image build and assets.** Separate package-resolution failures, downloads,
   disk exhaustion and checkpoint errors from GPU execution. Read the complete
   upstream requirements, not only the abbreviated dependency table above.
3. **Grader container.** Confirm its PyTorch and Triton import and CUDA device
   support. Inspect the resolved CUDA package versions and the actual device.
4. **Outer evaluator namespace.** The adapter runs imports and
   `torch.cuda.is_available()` inside bubblewrap. Docker-level visibility may
   disappear through device mounts, library paths or environment filtering.
5. **Native candidate namespace.** Test the original grader end to end. Its
   nested isolation must expose GPU devices and libraries while keeping broker
   state, credentials and frozen evaluator files protected.
6. **Scoring and SDK result transport.** Preserve raw `combined_score`, native
   validity and errors. Missing output or infrastructure failure must not become
   a fabricated zero reward or an omitted task.

The sidecar needs nested user/mount namespaces, `SYS_ADMIN`, and the existing
service security options. Diagnose host policy failures explicitly. Do not
replace the boundary with Docker privileged mode, mount the Docker socket, or
disable source-integrity checks to obtain a passing run.

### Code map

| File | Responsibility |
| --- | --- |
| [catalog_frontier_eng.json](src/rsi_eval/data/catalog_frontier_eng.json) | Task IDs, native metadata, source hashes and per-task runtime recipes |
| [frontier_eng_full.py](src/rsi_eval/materializers/frontier_eng_full.py) | Runtime option validation, prerequisites, source extraction, Dockerfile and Compose generation |
| [frontier_eng_full_runtime.py](src/rsi_eval/materializers/frontier_eng_full_runtime.py) | Asset bootstrap, package manifest, native imports, namespace probes and evaluator invocation |
| [frontier_eng_runtime.py](src/rsi_eval/materializers/frontier_eng_runtime.py) | Feedback broker, candidate budget, selection and verifier transport |
| [evaluation.py](src/rsi_eval/evaluation.py) | SDK resolution, Harbor invocation and result classification |
| [harbor_docker.py](src/rsi_eval/harbor_docker.py) | Docker environment and build-context isolation |
| [harbor_codex.py](src/rsi_eval/harbor_codex.py) | Candidate loading, container Codex execution, authentication persistence and usage |
| [test_frontier_eng_full.py](tests/test_frontier_eng_full.py) | Complete task mappings, generated environments, assets and namespace behavior |
| [test_frontier_eng.py](tests/test_frontier_eng.py) | Shared feedback, selection and error handling |

The downloaded upstream source also contains
`benchmarks/_shared/candidate_sandbox.py`,
`frontier_eval/tasks/unified/evaluator/python.py`, and each task's
`verification/` directory. Inspect these to understand the original protocol.
Keep upstream source frozen. If a compatibility fix is needed, make it explicit
in the adapter and test that it preserves data, scoring and candidate limits.

## Recovery and completion

For an interrupted run with unchanged configuration and implementation:

```sh
rsi-eval resume --output runs/linux-gpu/flashattention-codex-01
```

Resume preserves completed measurements and retries missing repetitions. After
editing the adapter, dependencies, candidate or runtime settings, create a new
output directory instead. Frozen method experiments also retain their prepared
SDK snapshot; prepare a new Linux experiment to pick up adapter fixes.

Complete this GPU work when:

- All four exact task IDs resolve without filtering or substitution.
- Each original reference reaches the native grader with CUDA available through
  both isolation layers, using the required assets and dependencies.
- Every task produces an interpretable native result or a clearly diagnosed
  native seed failure; unresolved infrastructure failures remain blockers.
- A Codex target run reaches candidate modification, feedback and final
  verification for each GPU task. Scores need not improve, and a valid rejection
  remains a result.
- New compatibility fixes have focused offline coverage, followed by the
  repository's offline regression checks.
- User documentation states the working prerequisites and any remaining device,
  package or asset limits without claiming untested hardware support.

If a task cannot run with its original rules, report the exact failing layer and
available repair options. Keep it visible as blocked until the repository owner
decides whether to retain it. Do not silently remove it from the full profile.

Users retain their own run outputs for debugging and recovery. Public guidance
should explain how to use the framework and understand its limits; it does not
require access to a maintainer's private experiments or operation history.
