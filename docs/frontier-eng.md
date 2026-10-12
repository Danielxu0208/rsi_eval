# Frontier-Eng

`frontier-eng@v1-bafa7d5` connects every task in the pinned Frontier v1 batch matrix to `evaluate()`. It contains the 47 released v1 tasks and the matrix's additional `EngDesign` entry. EngDesign is one native aggregate of seven engineering problems, identified separately from the 47-task suite.

The adapter calls the original unified task loader and evaluator from [Frontier-Engineering commit bafa7d5](https://github.com/Einsia/Frontier-Engineering/tree/bafa7d53162a3b1f128fec396bbde22fe972bf24). It reads each task's original initial program, prompt files, candidate destination, working directory, evaluation command, copied files, frozen files and artifacts. The downloaded source archive and all 2,665 source files have SHA-256 identities in the catalog. Upstream source is downloaded at runtime, not bundled into RSI Eval.

Every task has an executable native route and a declared runtime recipe. Some require NVIDIA GPUs, an amd64 container runtime or external assets. The old `cpu-subset-bafa7d5-v1` version remains a [legacy profile](frontier-eng-legacy.md) so earlier reports can still be inspected.

## Select and run tasks

Preview resolves task identities and settings without calling a model:

```sh
rsi-eval preview --config examples/benchmarks/frontier-eng.json
rsi-eval check --config examples/benchmarks/frontier-eng.json
rsi-eval run --config examples/benchmarks/frontier-eng.json --oracle --output runs/frontier-reference
rsi-eval run --config examples/benchmarks/frontier-eng.json --output runs/frontier-codex
```

The example selects two tasks. Omit `benchmark.task_ids` to select the complete profile, or supply any combination of its task IDs. The framework does not assign evolution or testing roles to tasks. An oracle run evaluates the published initial program. Its purpose is to check execution and metric transport; it need not score 1 or achieve a medal.

Use `options.benchmark_runtime` to configure native prerequisites. Settings may be applied globally or overridden per task:

```json
{
  "benchmark_runtime": {
    "platform": "linux/amd64",
    "gpu": true,
    "memory_mb": 16384,
    "cpus": 4,
    "build_timeout_sec": 7200,
    "task_overrides": {
      "SingleCellAnalysis/predict_modality": {
        "assets_dir": "/data/frontier-assets"
      }
    }
  }
}
```

The default platform follows the machine running RSI Eval. A remote Docker daemon with a different architecture needs an explicit `platform`. Clear a conflicting `DOCKER_DEFAULT_PLATFORM`; the check command reports the conflict before downloading sources. GPU tasks require `gpu: true`, native `linux/amd64` and an NVIDIA-enabled Docker daemon. Selecting them on an ARM Mac yields an explicit prerequisite failure rather than silently removing them.

`runtime_image` selects a prebuilt per-task image. Such an image must contain the declared Python dependencies, Bash and `/usr/bin/bwrap`, plus the task's system tools. Asset preparation still runs. `env` accepts explicit non-secret runtime variables. Authentication variables are rejected. `assets_dir` accepts a snapshot using upstream `benchmarks/` and `third_party/` paths. The materializer hashes copied files and rejects replacements of frozen upstream source.

Candidate source transport defaults to 4 MiB per proposal. Set `candidate_max_bytes` to a larger positive integer when needed; native task-specific size limits still apply. This is a documented adapter transport setting, not an additional benchmark scoring rule. The legacy profile keeps its original 80 KB limit.

The supplied recipes retain native task requirements and evaluator limits of 300, 600 or 1,200 seconds. The full profile has no 600-second smoke cap. Its agent transport timeout defaults to 604,800 seconds; `agent_timeout_sec` can explicitly lower or raise it. Harbor's outer `options.timeout` must also cover builds and all selected trials. A deliberately shorter transport limit is an experiment setting, not the original benchmark budget.

## Dependencies and assets

CPU task recipes use separate Python environments when their requirements conflict. ReactionOptimisation uses Python 3.9 for Summit, SustainDC uses Python 3.10, and most tasks use Python 3.12. CarAerodynamicsSensing uses Python 3.10 because its pinned NumPy and PyTorch requirements do not support the upstream general-purpose Python 3.12 profile.

| Family or task | Native requirement beyond the common NumPy/SciPy runtime |
|---|---|
| InventoryOptimization | Stockpyl 1.0.2 |
| QuantumComputing | Qiskit 2.3.1 and MQT Bench |
| JobShop | job-shop-lib and its benchmark data |
| Optics | Original Optics requirements, including NumPy below 2, PyTorch and the optics libraries |
| PyPortfolioOpt | Original convex optimization and solver requirements |
| Robotics/QuadrupedGaitOptimization | MuJoCo |
| Robotics/RobotArmCycleTimeOptimization | PyBullet |
| Astrodynamics/MannedLunarLanding | Octave, signal package and plotting tools |
| ReactionOptimisation | Original Summit requirements in Python 3.9 |
| SustainableDataCenterControl | Official pinned SustainDC checkout, native patch and requirements in an amd64 Python 3.10 runtime |
| SingleCellAnalysis | AnnData and four official H5AD assets, about 719 MB compressed on disk |
| Aerodynamics/CarAerodynamicsSensing | NVIDIA CUDA, official PhySense checkout, model checkpoint, pressure and velocity data |
| KernelEngineering | NVIDIA CUDA, PyTorch and Triton |
| ComputerSystems/MallocLab | Linux amd64 runtime, official WASI SDK and Wasmtime bootstrap |
| EngDesign | Original seven-problem aggregate, Octave, Icarus Verilog and original Python requirements |

SustainDC, CarAerodynamicsSensing and MallocLab use the upstream bootstrap scripts. SingleCellAnalysis downloads the same four public files used by its native scorer. Its held-out `test_mod2.h5ad` is stored in the scorer-private `resources_truth` directory, separate from the three candidate inputs. Asset downloads occur during image preparation, before model execution. A failed download is an environment failure.

MallocLab requires `platform: "linux/amd64"`. Configure the container platform explicitly on ARM hosts.

SustainDC also needs `platform: "linux/amd64"` with the supplied recipe: its original `ray==2.24.0` pin has no Linux ARM64 distribution. The adapter retains that dependency pin instead of deleting parts of the upstream environment.

The Optics ARM recipe installs Qt development tools and builds PyQt5 with an explicit non-interactive license confirmation, because its dependency has no ARM wheel. It installs CPU PyTorch from the official PyTorch CPU index before the original requirements. The nine original Optics graders run on CPU; this avoids downloading unused CUDA packages.

The pinned Frontier EngDesign directory omits nine files required by its YJ_03 stiffness calculation. The bootstrap restores the exact original directory from [EngDesign commit c8ec338](https://github.com/AGI4Engineering/EngDesign/tree/c8ec338cc5f846d792d93728b00c9448111139e5/EngDesign-Open/YJ_03), with individual SHA-256 checks and the upstream MIT license. This restores missing inputs without changing the evaluator or candidate. All seven subgraders must execute; a missing asset cannot become a low but valid aggregate score.

The image writes `/opt/frontier/build-manifest.json` with Python identity, resolved packages and hashes of required external assets. This manifest is retained in `frontier.json.runtime_evidence`. External files whose upstream URL is mutable are identified by the bytes actually downloaded. They are not represented as immutable solely because the main source commit is pinned.

## NVIDIA GPU setup

GPU tasks require native Linux amd64, Docker Engine with Buildx and Compose
2.30.0 or newer, a working NVIDIA host driver, and NVIDIA Container Toolkit
configured for Docker. The task images install their own Python, PyTorch and
Triton dependencies; a host-installed PyTorch is not used by the grader.

Follow the [Docker installation guide](https://docs.docker.com/engine/install/)
and [NVIDIA Container Toolkit guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
On an Ubuntu amd64 host with an existing NVIDIA driver, the optional setup tool
can inspect prerequisites and install missing components:

```sh
bash scripts/setup-nvidia-docker.sh
sudo bash scripts/setup-nvidia-docker.sh --install
```

The first command only checks the environment. Installation requires an
administrator session on the host system and restarts Docker, which can
interrupt existing containers. If Docker and GPU support already work, reuse
that installation. The tool does not configure experiment-account permissions;
the account running RSI Eval must be able to access Docker.

Check GPU access from the experiment account before selecting GPU tasks:

```sh
docker info
docker compose version
docker run --rm --runtime=nvidia --gpus all ubuntu:24.04 nvidia-smi
```

Select the desired task IDs and set `options.benchmark_runtime.gpu` to `true`.
The generated grader service configures the required nested bubblewrap
isolation. Namespace and CUDA startup checks must pass inside that service;
GPU visibility on the host alone does not establish grader readiness.

## Feedback, budget and selection

Each trial begins with the original task seed and prompt files. The agent edits the native candidate file and can request feedback:

```sh
python -I /opt/frontier_runtime.py feedback
python -I /opt/frontier_runtime.py feedback /workspace/PATH_FROM_INITIAL_PROGRAM
```

The first command reports the baseline, best measured result and remaining calls. The second evaluates the current candidate in the native runtime. At most 99 feedback proposals are allowed; final verification consumes one further candidate slot. This preserves the released matrix's 100-proposal maximum. The initial baseline is separate. Invalid candidates consume a proposal and are not silently retried.

A private broker records candidate sources, hashes, raw native metrics and failures. It selects the best feasible native `combined_score` observed within budget, including the baseline. A final diagnostic rerun of the selected source does not provide optimization feedback or consume another proposal. Its metric differences remain in `confirmation`; stochastic variation does not erase the best measured discovery score.

`reward` and `native_score` retain native `combined_score`; `valid` remains separate. The summary reports each task independently and never averages unrelated objectives. Native invalid sentinels remain graded results. Missing dependencies, failed namespaces and missing or malformed grader output produce infrastructure errors. Auxiliary non-finite metrics are represented as `null`, with their original `inf`, `-inf` or `nan` identity recorded in `native_nonfinite_fields`; a non-finite reward or validity flag is an error.

## Execution boundary and compatibility fixes

The coding agent sees native prompt files and the initial candidate. The separate grader sidecar holds the frozen evaluator, source snapshots, counter and private verification token. It has a read-only root filesystem, temporary `/tmp` and `/state` filesystems, and a private Docker network. It has no published port, host directory, model credential or Docker socket.

The outer evaluator namespace hides broker files and processes. Native task isolation runs inside that boundary. Nested bubblewrap needs `SYS_ADMIN` and the service security settings `seccomp=unconfined`, `apparmor=unconfined` and `systempaths=unconfined`. This is not Docker privileged mode. Inside the outer user namespace, the trusted driver retains only the mount and user-mapping capabilities needed by nested bubblewrap. A launcher drops capabilities in the actual native candidate namespace.

The outer driver removes three bubblewrap-created `/proc` child mounts so Linux permits the original inner namespace to mount its own proc filesystem. The native inner namespace retains its masks. Startup probes check both namespace levels, verify that broker state is invisible and frozen source cannot be written or remounted, and check that the test candidate has no effective capabilities. These checks run before the service becomes healthy and before a model starts.

Compatibility handling is explicit:

- The unified evaluator's optional OpenEvolve result wrapper otherwise discards artifacts when OpenEvolve is absent. A thin transport wrapper retains its original metrics and artifacts without installing a search algorithm.
- EngDesign's native temporary layout receives a read-only alias to the original shared isolation helper. Its existing local execution mode runs inside the grader container, avoiding nested Docker.
- PyMOTOSIMPCompliance receives a fresh writable scratch directory at the native scorer's source-relative `temp` path. Its direct candidate subprocess drops capabilities before execution. Frozen code remains read-only.
- The outer namespace exposes a read-only `/sys` and gives graders a fresh temporary `HOME` and Matplotlib configuration directory. Startup imports run with the same environment used for grading.
- Native interpreter paths resolve to the per-task Docker environment. Scoring formulas, task data and evaluator time budgets remain native.

This protocol is named `harbor-native-unified-100-v2`. It uses a Harbor coding-agent session and a feedback broker in place of the upstream OpenEvolve search loop.

## Results and artifacts

Each trial preserves the native metrics, scorer artifacts, output and candidate history in `/logs/verifier/frontier.json`, alongside Harbor's original files and the unified report. Materialization records source hashes, copied asset hashes, task metadata, platform and runtime settings. Private verification nonces make execution-context hashes differ between runs; the pinned source and protocol identify the comparable experiment definition.

GPU execution requires native Linux amd64, an NVIDIA-enabled Docker daemon, and working nested bubblewrap namespaces. CUDA and namespace startup probes must pass before a grader runs; host GPU visibility alone does not establish grader readiness.

The pinned upstream tree has no root license file. RSI Eval ships its own adapter and source manifest and fetches official source at runtime. Consult upstream terms before redistributing downloaded benchmark artifacts.
