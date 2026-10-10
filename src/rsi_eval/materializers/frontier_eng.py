"""Prepare two pinned Frontier-Eng CPU tasks with a separate feedback grader."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil

from .common import download

SOURCE_COMMIT = "bafa7d53162a3b1f128fec396bbde22fe972bf24"
REPOSITORY = "https://github.com/Einsia/Frontier-Engineering"
PROTOCOL = "harbor-feedback-100-v1"
TASKS = {
    "InventoryOptimization/disruption_eoqd": "baseline/init.py",
    "Robotics/PIDTuning": "scripts/init.py",
}


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def grader_platform():
    configured = os.environ.get("RSI_EVAL_FRONTIER_PLATFORM")
    if configured:
        if configured not in ("linux/amd64", "linux/arm64"):
            raise ValueError("RSI_EVAL_FRONTIER_PLATFORM must be linux/amd64 or linux/arm64")
        return configured
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "linux/arm64"
    if machine in ("x86_64", "amd64"):
        return "linux/amd64"
    raise ValueError("set RSI_EVAL_FRONTIER_PLATFORM to the native Docker Linux architecture")


def _source(spec, destination):
    files = spec["source"]["files"]
    for relative, expected in files.items():
        if relative.startswith("/") or ".." in Path(relative).parts or not re.fullmatch("[0-9a-f]{64}", expected):
            raise ValueError("invalid pinned Frontier source manifest")
        download(f"https://raw.githubusercontent.com/Einsia/Frontier-Engineering/{SOURCE_COMMIT}/{relative}", destination / relative, expected)
    return files


def _task(spec, task, upstream, output, platform_name):
    task_id = task["id"]
    candidate = TASKS[task_id]
    prefix = "benchmarks/" + task_id + "/"
    relevant = {key: value for key, value in spec["source"]["files"].items()
                if key.startswith(prefix) or key == "benchmarks/_shared/candidate_sandbox.py"}
    # Namespaced context basenames also avoid BuildKit's basename sharing issue.
    source_hash = hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()
    runtime = Path(__file__).with_name("frontier_eng_runtime.py")
    token = secrets.token_hex(32)
    context_hash = hashlib.sha256((source_hash + hashlib.sha256(runtime.read_bytes()).hexdigest()
                                   + token + platform_name).encode()).hexdigest()
    environment = output / "environment"
    agent_dir = environment / ("agent-" + context_hash)
    grader_dir = environment / ("grader-" + context_hash)
    root = grader_dir / "upstream"
    for relative in relevant:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(upstream / relative, destination)
    (root / "frontier_eval").mkdir()
    public_names = ["README.md", "Task.md", "frontier_eval/constraints.txt", candidate]
    if task_id.startswith("Robotics"):
        public_names += ["references/pid_config.json"]
    else:
        public_names += ["verification/evaluate.py"]  # Explicit upstream agent_files entry.
    for name in public_names:
        destination = agent_dir / "public" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(upstream / prefix / name, destination)
    agent_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(runtime, agent_dir / "frontier_runtime.py")
    shutil.copyfile(runtime, grader_dir / "frontier_runtime.py")
    broker = {"task_id": task_id, "source_root": "/opt/frontier/upstream",
              "files": relevant, "candidate_path": candidate,
              "protocol": PROTOCOL, "candidate_budget": 100,
              "verifier_token": token, "state_dir": "/state"}
    write(grader_dir / "broker.json", json.dumps(broker, indent=2) + "\n")
    write(agent_dir / "Dockerfile", '''FROM python:3.12.9-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git procps \\
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy==2.2.6 scipy==1.15.3 stockpyl==1.0.2
COPY public /workspace
COPY frontier_runtime.py /opt/frontier_runtime.py
WORKDIR /workspace
''')
    write(grader_dir / "Dockerfile", '''FROM python:3.12.9-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy==2.2.6 scipy==1.15.3 stockpyl==1.0.2
COPY upstream /opt/frontier/upstream
COPY frontier_runtime.py /opt/frontier_runtime.py
COPY broker.json /opt/frontier/broker.json
RUN chmod -R a-w /opt/frontier && mkdir -m 700 /state
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-I", "/opt/frontier_runtime.py", "serve", "/opt/frontier/broker.json"]
''')
    # Harbor requires its standard Dockerfile even when Compose replaces context.
    shutil.copyfile(agent_dir / "Dockerfile", environment / "Dockerfile")
    write(environment / "docker-compose.yaml", f'''services:
  main:
    build:
      context: ${{CONTEXT_DIR}}/{agent_dir.name}
    networks: [default, frontier-private]
    depends_on:
      frontier-grader:
        condition: service_healthy
  frontier-grader:
    build:
      context: ${{CONTEXT_DIR}}/{grader_dir.name}
    platform: {platform_name}
    networks: [frontier-private]
    cap_add: [SYS_ADMIN]
    security_opt:
      - seccomp=unconfined
      - apparmor=unconfined
      - systempaths=unconfined
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]
      interval: 2s
      timeout: 5s
      retries: 30
    mem_limit: 2g
    cpus: 1
networks:
  frontier-private:
    internal: true
''')
    original_prompt = (upstream / prefix / "Task.md").read_text()
    instruction = original_prompt + f'''

## RSI Eval execution protocol

Optimize `/workspace/{candidate}`. Follow the original constraints in
`/workspace/frontier_eval/constraints.txt` and preserve the task's output contract.
The file starts with the published baseline. The grader executes your source in
its own sandbox. Files that your optimizer needs are staged by the native grader.

Ask for the initial baseline result and remaining budget:

```sh
python -I /opt/frontier_runtime.py feedback
```

Evaluate the current program and read native scores and errors:

```sh
python -I /opt/frontier_runtime.py feedback /workspace/{candidate}
```

You may make at most 99 feedback calls. The final file uses the remaining candidate
slot, for a maximum of 100 proposals plus the initial baseline. Failures consume
calls. The grader preserves every source version and selects the best feasible
score seen within this budget, including the baseline. Higher combined_score is
better. Final verification reruns that selected source and records any score difference.
The reported discovery score remains the best observed value within budget. Local computations are
allowed inside the task's original execution limits. The agent session has a
600-second wall-clock limit; stop when you have a useful candidate.

Use the feedback command at least once after a change so the recorded trajectory
shows the candidate and its outcome. Leave your final source at the original
path. Report which changes you made and the observed scores.
'''
    write(output / "instruction.md", instruction)
    write(output / "task.toml", f'''version = "1.0"
[metadata]
benchmark = "frontier-eng"
record_id = "{task_id}"
source_commit = "{SOURCE_COMMIT}"
protocol = "{PROTOCOL}"
grader_platform = "{platform_name}"
primary_metric = "native_combined_score"
optimization_direction = "maximize"
[agent]
timeout_sec = 600.0
[verifier]
timeout_sec = 1100.0
[environment]
build_timeout_sec = 1200.0
cpus = 1
memory_mb = 2048
storage_mb = 4096
''')
    write(output / "tests" / "verifier.json", json.dumps({"verifier_token": token}) + "\n")
    shutil.copyfile(runtime, output / "tests" / "frontier_runtime.py")
    write(output / "tests" / "test.sh", f"#!/bin/bash\nset -euo pipefail\npython -I /tests/frontier_runtime.py verify /tests/verifier.json /workspace/{candidate} /logs/verifier\n")
    write(output / "solution" / "solve.sh", "#!/bin/bash\nset -euo pipefail\n# Reference validation uses the unchanged published baseline.\npython -I /opt/frontier_runtime.py feedback\n")
    for name in ("tests/test.sh", "solution/solve.sh"):
        (output / name).chmod(0o755)
    provenance = {"repository": REPOSITORY, "commit": SOURCE_COMMIT, "task_id": task_id,
                  "files": relevant, "grader_platform": platform_name, "protocol": spec["protocol"],
                  "stable_source_sha256": source_hash,
                  "protocol_sha256": hashlib.sha256(json.dumps(spec["protocol"], sort_keys=True).encode()).hexdigest(),
                  "execution_nonce": "private verifier token differs per materialization; generated task hash includes it"}
    write(output / "provenance.json", json.dumps(provenance, indent=2) + "\n")


def preflight(spec):
    if spec.get("version") == "v1-bafa7d5":
        from .frontier_eng_full import preflight as check_full
        return check_full(spec)
    if spec.get("runtime_options"):
        raise ValueError("benchmark_runtime options require frontier-eng@v1-bafa7d5")
    return {}


def prepare(spec, output_dir):
    if spec.get("version") == "v1-bafa7d5":
        from .frontier_eng_full import prepare as prepare_full
        return prepare_full(spec, output_dir)
    if (spec.get("materializer") != "frontier_eng" or spec.get("source", {}).get("repository") != REPOSITORY
            or spec.get("source", {}).get("commit") != SOURCE_COMMIT
            or spec.get("protocol", {}).get("version") != PROTOCOL):
        raise ValueError("Frontier requires its pinned source and reviewed feedback protocol")
    tasks = spec.get("tasks", [])
    if not tasks or len({task.get("id") for task in tasks}) != len(tasks):
        raise ValueError("Frontier task selection must be nonempty and unique")
    for task in tasks:
        if task.get("id") not in TASKS or task.get("record_id") != task["id"]:
            raise ValueError("unsupported Frontier task: " + str(task.get("id")))
    selected_platform = grader_platform()
    docker_default = os.environ.get("DOCKER_DEFAULT_PLATFORM")
    if docker_default and docker_default != selected_platform:
        raise ValueError("DOCKER_DEFAULT_PLATFORM conflicts with the native Frontier grader platform; "
                         "unset DOCKER_DEFAULT_PLATFORM before this run, or set RSI_EVAL_FRONTIER_PLATFORM "
                         "to the native Docker daemon architecture. Emulated bubblewrap is unsupported.")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    upstream = root / "source" / SOURCE_COMMIT
    _source(spec, upstream)
    result = []
    for task in tasks:
        name = "frontier-" + task["id"].lower().replace("/", "-").replace("_", "-")
        destination = root / "tasks" / name
        destination.mkdir(parents=True, exist_ok=False)
        _task(spec, task, upstream, destination, selected_platform)
        result.append({"id": task["id"], "path": str(destination)})
    return result
