"""Full released EngDesign-Open task preparation; original graders stay external."""
from __future__ import annotations
import hashlib
import difflib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
from .common import download

COMMIT = "c8ec338cc5f846d792d93728b00c9448111139e5"
VERSION = "open-c8ec338-v1"
REPOSITORY = "https://github.com/AGI4Engineering/EngDesign"
BASE_IMAGE = "python:3.12.9-slim-bookworm"
WEBOTS_IMAGE = "cyberbotics/webots:R2025a-ubuntu22.04"
REQUIREMENTS = (
    "pydantic==2.11.3", "numpy==1.26.4", "scipy==1.12.0", "matplotlib==3.10.1",
    "opencv-python-headless==4.10.0.84", "scikit-image==0.24.0", "scikit-learn==1.5.2",
    "pandas==2.2.3", "control==0.10.1", "cvxopt==1.3.2", "sympy==1.13.3",
    "linear-tree==0.3.5", "ujson==5.10.0", "tqdm==4.67.1", "IPython==8.35.0",
    "instructor==1.7.9", "openai==1.76.0", "oct2py==5.8.0",
)


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _options(spec):
    opts = spec.get("runtime_options", {})
    unknown = set(opts) - {"runtime_image", "webots_image", "platform", "verifier_timeout_sec"}
    if unknown:
        raise ValueError("unknown EngDesign runtime options: " + ", ".join(sorted(unknown)))
    for key in ("runtime_image", "webots_image"):
        if key in opts and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/@-]*", opts[key]):
            raise ValueError(key + " must be a Docker image reference")
    if opts.get("platform") not in (None, "linux/arm64", "linux/amd64"):
        raise ValueError("EngDesign platform must be linux/arm64 or linux/amd64")
    timeout = opts.get("verifier_timeout_sec", 840)
    if type(timeout) is not int or not 30 <= timeout <= 840:
        raise ValueError("verifier_timeout_sec must be an integer from 30 through 840")
    return opts


def _platform(opts, family):
    if opts.get("platform"):
        return opts["platform"]
    if family == "webots":
        return "linux/amd64"
    return "linux/arm64" if platform.machine().lower() in ("arm64", "aarch64") else "linux/amd64"


def preflight(spec):
    try:
        opts = _options(spec)
        families = sorted({task["runtime_family"] for task in spec["tasks"]})
        default = os.environ.get("DOCKER_DEFAULT_PLATFORM")
        selected = {_platform(opts, family) for family in families}
        if default and selected != {default}:
            raise ValueError("unset DOCKER_DEFAULT_PLATFORM so each EngDesign task can select its declared runtime platform")
        if "webots" in families and _platform(opts, "webots") != "linux/amd64" and not opts.get("webots_image"):
            raise ValueError("the default Webots R2025a image requires linux/amd64; provide webots_image for another architecture")
        return {"engdesign_runtime": {"ok": True, "detail": "runtime requirements declared; images and task imports are checked at container startup",
                "runtime_families": families, "platforms": sorted(selected),
                "python_image": opts.get("runtime_image", BASE_IMAGE), "webots_image": opts.get("webots_image", WEBOTS_IMAGE),
                "requires_api_key": False}}
    except (ValueError, TypeError) as exc:
        return {"engdesign_runtime": {"ok": False, "detail": str(exc)}}


def dockerfile(family, base=None):
    webots = family == "webots"
    base = base or (WEBOTS_IMAGE if webots else BASE_IMAGE)
    apt = "bash ca-certificates curl git procps iverilog octave octave-control gnuplot-nox ffmpeg libsuitesparse-dev libblas-dev liblapack-dev gcc g++"
    if webots:
        apt = "bash ca-certificates python3 python3-pip xvfb xauth libgl1-mesa-dri"
    pip = "python3 -m pip"
    requirements = REQUIREMENTS if not webots else ("pydantic==2.11.3", "numpy==1.26.4")
    return f'''FROM {base}
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {apt} \\
    && rm -rf /var/lib/apt/lists/*
RUN {pip} install --no-cache-dir {' '.join(requirements)}
ENV MPLBACKEND=Agg OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
'''


def _copy(source, destination, names):
    for name in names:
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("invalid relative EngDesign source path")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)


def _local_webots_world(source):
    """Resolve unchanged R2025a assets locally; preserve all world node values."""
    path = source / "worlds/construction_site.wbt"
    before = path.read_bytes()
    old = b"https://raw.githubusercontent.com/cyberbotics/webots/R2025a/projects/objects/backgrounds/protos/"
    if before.count(old) != 2:
        raise ValueError("unexpected Webots world PROTO references")
    after = before.replace(old, b"webots://projects/objects/backgrounds/protos/")
    path.write_bytes(after)
    patch_text = "".join(difflib.unified_diff(before.decode().splitlines(keepends=True),
                         after.decode().splitlines(keepends=True),
                         fromfile="upstream/worlds/construction_site.wbt",
                         tofile="runtime/worlds/construction_site.wbt"))
    return {"path": "worlds/construction_site.wbt", "kind": "offline_resource_uri",
            "reason": "Resolve exact R2025a PROTO assets locally in the network-isolated verifier",
            "original_sha256": hashlib.sha256(before).hexdigest(), "runtime_sha256": hashlib.sha256(after).hexdigest(),
            "patch_sha256": hashlib.sha256(patch_text.encode()).hexdigest(), "patch_text": patch_text}


def prepare(spec, output_dir):
    if spec.get("version") != VERSION or spec["source"].get("commit") != COMMIT or spec["source"].get("repository") != REPOSITORY:
        raise ValueError("full EngDesign adapter requires its exact released catalog")
    catalog = json.loads((Path(__file__).parent.parent / "data" / "catalog_engdesign.json").read_text())
    released = next(item for item in catalog if item["version"] == VERSION)
    if not spec["tasks"] or len({task.get("id") for task in spec["tasks"]}) != len(spec["tasks"]):
        raise ValueError("EngDesign task selection must be nonempty and unique")
    tasks_by_id = {task["id"]: task for task in released["tasks"]}
    if spec["source"] != released["source"] or any(tasks_by_id.get(task.get("id")) != task for task in spec["tasks"]):
        raise ValueError("full EngDesign tasks and source hashes must match the released catalog")
    checks = preflight(spec)
    if not all(item["ok"] for item in checks.values()):
        raise ValueError(checks["engdesign_runtime"]["detail"])
    opts = _options(spec)
    root = Path(output_dir)
    src = root / "source" / COMMIT
    files = spec["source"]["files"]
    selected = {"LICENSE"}
    for task in spec["tasks"]:
        selected.update("EngDesign-Open/" + task["id"] + "/" + name for name in task["source_files"])
    for name in sorted(selected):
        download(f"https://raw.githubusercontent.com/AGI4Engineering/EngDesign/{COMMIT}/{name}", src / name, files[name])
    webots_assets = spec["source"].get("simulator_assets", {})
    asset_root = root / "source" / "webots" / webots_assets.get("commit", "unused")
    if any(task["runtime_family"] == "webots" for task in spec["tasks"]):
        for name, digest in webots_assets["files"].items():
            download(f"https://raw.githubusercontent.com/cyberbotics/webots/{webots_assets['commit']}/{name}", asset_root / name, digest)
    results = []
    runtime = Path(__file__).with_name("engdesign_runtime.py")
    for task in spec["tasks"]:
        task_id, family = task["id"], task["runtime_family"]
        destination = root / "tasks" / ("engdesign-" + task_id.lower().replace("_", "-"))
        destination.mkdir(parents=True, exist_ok=False)
        source = src / "EngDesign-Open" / task_id
        platform_name = _platform(opts, family)
        token = secrets.token_hex(32)
        context = hashlib.sha256((task_id + token + runtime.read_text()).encode()).hexdigest()
        environment = destination / "environment"
        agent = environment / ("agent-" + context)
        grader = environment / ("grader-" + context)
        _copy(source, agent / "public", task["public_files"])
        _copy(source, grader / "native", task["grader_files"])
        shutil.copyfile(src / "LICENSE", grader / "LICENSE")
        metadata = {"record_id": task_id, "runtime_family": family,
                    "files": {name: files[f"EngDesign-Open/{task_id}/{name}"] for name in task["grader_files"]}}
        metadata["original_files"] = dict(metadata["files"])
        metadata["compatibility_patches"] = []
        if family == "webots":
            patch = _local_webots_world(grader / "native")
            _write(destination / "compatibility/offline-webots-resources.patch", patch.pop("patch_text"))
            metadata["compatibility_patches"].append(patch)
            metadata["files"][patch["path"]] = patch["runtime_sha256"]
            _copy(asset_root, grader / "webots", webots_assets["files"])
            metadata["runtime_files"] = {"/usr/local/webots/" + name: digest for name, digest in webots_assets["files"].items()}
            _write(grader / "webots/LICENSE.engdesign-assets.txt", "Official Cyberbotics R2025a assets; for use with Webots.\n" + webots_assets["license_url"] + "\n")
        config = {**metadata, "source_dir": "/opt/engdesign/native", "verifier_token": token,
                  "timeout_sec": opts.get("verifier_timeout_sec", 840)}
        _write(grader / "config.json", json.dumps(config, indent=2) + "\n")
        shutil.copyfile(runtime, grader / "runtime.py")
        _write(agent / "Dockerfile", dockerfile("python", opts.get("runtime_image")) +
               "COPY public /workspace\nWORKDIR /workspace\n")
        image = opts.get("webots_image") if family == "webots" else opts.get("runtime_image")
        grader_base = dockerfile(family, image)
        if family == "webots":
            grader_base += "COPY webots /usr/local/webots\n"
        _write(grader / "Dockerfile", grader_base + '''COPY LICENSE /opt/engdesign/LICENSE
COPY native /opt/engdesign/native
COPY config.json /opt/engdesign/config.json
COPY runtime.py /opt/engdesign/runtime.py
RUN chmod 600 /opt/engdesign/config.json
CMD ["python3", "-I", "/opt/engdesign/runtime.py", "serve", "/opt/engdesign/config.json"]
''')
        shutil.copyfile(agent / "Dockerfile", environment / "Dockerfile")
        # Agent credentials never enter the grader's filesystem or environment.
        _write(environment / "docker-compose.yaml", f'''services:
  main:
    build:
      context: ${{CONTEXT_DIR}}/{agent.name}
    networks: [default, engdesign-private]
    depends_on:
      engdesign-grader:
        condition: service_healthy
  engdesign-grader:
    build:
      context: ${{CONTEXT_DIR}}/{grader.name}
    platform: {platform_name}
    networks: [engdesign-private]
    healthcheck:
      test: [CMD, python3, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]
      interval: 2s
      timeout: 5s
      retries: 30
    mem_limit: 4g
    cpus: 2
    pids_limit: 256
    tmpfs:
      - /tmp:rw,size=1g,mode=1777
networks:
  engdesign-private:
    internal: true
''')
        instruction = (source / "LLM_prompt.txt").read_text()
        instruction += "\n\n## Submission\n\nWrite your final JSON object to /workspace/output.json. " \
                       "It must match Response_structure in /workspace/output_structure.py. " \
                       "Use the provided task assets in /workspace. The original grader runs after the agent finishes.\n"
        if len(task["public_files"]) > 1:
            instruction += "\nAvailable input files: " + ", ".join(task["public_files"]) + ".\n"
        _write(destination / "instruction.md", instruction)
        _write(destination / "task.toml", f'''version = "1.0"
[metadata]
benchmark = "engdesign-open"
scope = "full-upstream-open"
record_id = "{task_id}"
source_commit = "{COMMIT}"
runtime_family = "{family}"
grader_platform = "{platform_name}"
[agent]
timeout_sec = 600.0
[verifier]
timeout_sec = 900.0
[environment]
build_timeout_sec = 1800.0
cpus = 2
memory_mb = 4096
storage_mb = 8192
''')
        _write(destination / "tests" / "verifier.json", json.dumps({"verifier_token": token}) + "\n")
        shutil.copyfile(runtime, destination / "tests" / "runtime.py")
        _write(destination / "tests" / "test.sh", "#!/bin/bash\nset -euo pipefail\npython -I /tests/runtime.py verify /tests/verifier.json /workspace/output.json /logs/verifier\n")
        (destination / "tests" / "test.sh").chmod(0o755)
        if task_id in ("AB_01", "XY_01"):
            from .engdesign import _ORACLE_AB, _ORACLE_XY
            _write(destination / "solution" / "solve.py", _ORACLE_AB if task_id == "AB_01" else _ORACLE_XY)
            _write(destination / "solution" / "solve.sh", "#!/bin/bash\nset -euo pipefail\npython -I /solution/solve.py\n")
            (destination / "solution" / "solve.sh").chmod(0o755)
        _write(destination / "provenance.json", json.dumps({**metadata, "repository": REPOSITORY,
               "commit": COMMIT, "protocol": spec["protocol"], "runtime_options": opts,
               "platform": platform_name, "worker_image": image or (WEBOTS_IMAGE if family == "webots" else BASE_IMAGE),
               "source_sha256": hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()}, indent=2) + "\n")
        shutil.copyfile(src / "LICENSE", destination / "LICENSE")
        results.append({"id": task_id, "path": str(destination.resolve())})
    return results
