"""Full pinned Harvey LAB tasks with the unchanged native rubric evaluator."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Any

COMMIT = "a15650fe74bb20b86f160aaabdad75b87a91b6e8"
VERSION = "public-a15650f-v1"
REPOSITORY = "https://github.com/harveyai/harvey-labs"
DEFAULT_JUDGES = ["claude-sonnet-4-6", "gpt-5.5"]
ENV_KEYS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY",
            "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"}
ALLOWED = {"source_dir", "judge_env_file", "judges", "judge_parallel", "agent_timeout_sec",
           "verifier_timeout_sec", "build_timeout_sec", "memory_mb", "cpus", "platform"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_manifest(root: Path) -> dict[str, str]:
    """Hash every public document, including hidden files; reject links."""
    result = {}
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Harvey document root must be a regular directory")
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("Harvey documents contain a non-regular entry")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha256(path)
    return result


def tree_hash(manifest: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def options(spec: dict) -> dict:
    result = dict(spec.get("runtime_options") or {})
    if set(result) - ALLOWED:
        raise ValueError("unknown Harvey benchmark_runtime options: " + ", ".join(sorted(set(result) - ALLOWED)))
    judges = result.setdefault("judges", list(DEFAULT_JUDGES))
    if (not isinstance(judges, list) or not 1 <= len(judges) <= 2
            or any(not isinstance(m, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", m) for m in judges)
            or len(set(judges)) != len(judges)):
        raise ValueError("Harvey judges must contain one or two distinct native model IDs")
    for model in judges:
        provider_key(model)
    for key in ("judge_parallel", "agent_timeout_sec", "verifier_timeout_sec", "build_timeout_sec", "memory_mb", "cpus"):
        if key in result and (type(result[key]) is not int or result[key] <= 0):
            raise ValueError(key + " must be a positive integer")
    if result.get("platform") not in (None, "linux/amd64", "linux/arm64"):
        raise ValueError("platform must be linux/amd64 or linux/arm64")
    return result


def provider_key(model: str) -> str:
    name = model.lower()
    for prefixes, key in ((('claude',), 'ANTHROPIC_API_KEY'), (('gemini',), 'GOOGLE_API_KEY'),
                          (('gpt', 'o1', 'o3', 'o4', 'o5'), 'OPENAI_API_KEY'), (('mistral',), 'MISTRAL_API_KEY')):
        if name.startswith(prefixes):
            return key
    raise ValueError("unknown native Harvey judge provider: " + model)


def runtime_environment(spec: dict) -> dict[str, str]:
    from .judge_env import load_judge_env
    return load_judge_env(options(spec), ENV_KEYS)


def preflight(spec: dict) -> dict:
    opts = options(spec)
    environment = runtime_environment(spec)
    required = {provider_key(model) for model in opts["judges"]} | {"ANTHROPIC_API_KEY"}
    checks = {}
    for key in sorted(required):
        present = bool(environment.get(key))
        if key == "GOOGLE_API_KEY":
            present = present or bool(environment.get("GEMINI_API_KEY"))
        checks["judge:" + key] = {"ok": present, "detail": "Native LAB grading requires " + key
            + ". Codex subscription authentication does not supply judge API access. Anthropic also supplies the native filename-matching fallback."}
    if opts.get("source_dir"):
        root = Path(opts["source_dir"]).expanduser()
        checks["source_directory"] = {"ok": root.is_dir(), "detail": "Local upstream source is verified against pinned code, rubric and complete document hashes during preparation."}
    else:
        checks["git"] = {"ok": bool(shutil.which("git")), "detail": "Git downloads the complete pinned official source. The public documents occupy about 3 GB."}
    checks["native_judge_profile"] = {"ok": True, "detail": "Standard dual" if set(opts["judges"]) == set(DEFAULT_JUDGES) else "Explicit custom native judge selection", "judges": opts["judges"]}
    return checks


def _safe(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("unsafe Harvey source path")
    result = root.joinpath(*path.parts)
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("Harvey source escapes its root")
    return result


def _copy_file(source: Path, destination: Path, expected: str) -> None:
    if source.is_symlink() or not source.is_file() or sha256(source) != expected:
        raise ValueError("Harvey pinned source checksum mismatch: " + source.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if sha256(destination) != expected:
        raise ValueError("Harvey source changed during snapshot")


def snapshot_source(spec: dict, destination: Path) -> Path:
    opts = options(spec)
    if spec["source"]["commit"] != COMMIT or spec["source"]["repository"] != REPOSITORY:
        raise ValueError("unexpected Harvey source identity")
    original = Path(opts["source_dir"]).expanduser().resolve() if opts.get("source_dir") else destination.parent / "checkout"
    if not opts.get("source_dir"):
        original.mkdir(parents=True)
        commands = (("init", "--quiet"), ("remote", "add", "origin", REPOSITORY + ".git"),
                    ("fetch", "--quiet", "--depth", "1", "origin", COMMIT), ("checkout", "--quiet", "--detach", "FETCH_HEAD"))
        for args in commands:
            completed = subprocess.run(["git", "-C", str(original), *args], text=True, capture_output=True, timeout=1800)
            if completed.returncode:
                raise RuntimeError("Harvey pinned source checkout failed: " + completed.stderr[-2000:])
    destination.mkdir(parents=True)
    for relative, digest in spec["source"]["files"].items():
        _copy_file(_safe(original, relative), _safe(destination, relative), digest)
    copied = set()
    for task in spec["tasks"]:
        relative = "tasks/" + task["id"] + "/task.json"
        _copy_file(_safe(original, relative), _safe(destination, relative), task["task_sha256"])
        config = json.loads((destination / relative).read_text())
        if len(config["criteria"]) != task["n_criteria"] or not config.get("instructions"):
            raise ValueError("Harvey native task metadata mismatch")
        expected_docs = (original / relative).parent / config.get("docs_dir", "documents")
        if expected_docs.resolve() != _safe(original, task["documents_path"]).resolve():
            raise ValueError("Harvey documents mapping differs from native task loader")
        document_path = task["documents_path"]
        if document_path not in copied:
            docs = _safe(original, document_path)
            manifest = tree_manifest(docs)
            expected = spec["source"]["document_trees"][document_path]
            if tree_hash(manifest) != expected["sha256"] or len(manifest) != expected["file_count"]:
                raise ValueError("Harvey document tree checksum mismatch: " + document_path)
            target = _safe(destination, document_path)
            shutil.copytree(docs, target)
            if tree_hash(tree_manifest(target)) != expected["sha256"]:
                raise ValueError("Harvey documents changed during snapshot")
            copied.add(document_path)
    return destination


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _link(source: Path, destination: Path) -> None:
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)


def _prepare_task(spec: dict, task: dict, upstream: Path, destination: Path) -> None:
    opts = options(spec)
    env = destination / "environment"
    tests = destination / "tests"
    env.mkdir(parents=True)
    tests.mkdir()
    # Custom Compose contexts bypass Harbor's generic context relocation.
    # Give each distinct document set a content-specific basename so BuildKit
    # cannot confuse different tasks' identically named documents/ directories.
    documents_hash = spec["source"]["document_trees"][task["documents_path"]]["sha256"]
    docs = upstream.parent / "document-contexts" / documents_hash
    if not docs.exists():
        shutil.copytree(upstream / task["documents_path"], docs, copy_function=_link)
    task_config = json.loads((upstream / "tasks" / task["id"] / "task.json").read_text())
    # Build the original sandbox once per run; shared DMS tasks reuse one document context.
    sandbox = upstream / "lab_core/sandbox"
    _write(env / "Dockerfile", 'FROM harvey-sandbox\nCOPY . /workspace/documents\nWORKDIR /workspace\n')
    compose = {"services": {
        "harvey-sandbox": {"build": {"context": str(sandbox)}, "command": ["sleep", "infinity"]},
        "main": {"build": {"context": str(docs), "dockerfile": str((env / "Dockerfile").resolve()),
                           "additional_contexts": {"harvey-sandbox": "service:harvey-sandbox"}},
                 "volumes": ["harvey-documents:/workspace/documents:ro"],
                 "environment": {"WORKSPACE_DIR": "/workspace", "DOCUMENTS_DIR": "/workspace/documents", "OUTPUT_DIR": "/workspace/output"}}
    }, "volumes": {"harvey-documents": {}}}
    if opts.get("platform"):
        for service in compose["services"].values():
            service["platform"] = opts["platform"]
    _write(env / "docker-compose.yaml", json.dumps(compose, indent=2) + "\n")
    # No rubric or API secret is included in the agent build contexts.
    for relative in spec["source"]["files"]:
        _link(upstream / relative, tests / "native" / relative)
    _link(upstream / "tasks" / task["id"] / "task.json", tests / "native/tasks" / task["id"] / "task.json")
    shutil.copyfile(Path(__file__).with_name("harvey_lab_verifier.py"), tests / "verify.py")
    extra = " --extra mistral" if any(m.lower().startswith("mistral") for m in opts["judges"]) else ""
    _write(tests / "Dockerfile", '''FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl tar && rm -rf /var/lib/apt/lists/*
COPY native /opt/harvey
RUN sh /opt/harvey/lab_core/sandbox/install_pandoc.sh
RUN pip install --no-cache-dir uv==0.12.24
WORKDIR /opt/harvey
RUN uv sync --frozen --no-dev''' + extra + '''
RUN uv pip freeze --python .venv/bin/python > /opt/harvey-runtime-packages.txt
COPY verify.py /tests/verify.py
COPY config.json /tests/config.json
COPY test.sh /tests/test.sh
ENV LAB_ROOT=/opt/harvey
WORKDIR /workspace
''')
    if opts.get('platform'):
        dockerfile = tests / 'Dockerfile'
        dockerfile.write_text(dockerfile.read_text().replace('FROM python:', 'FROM --platform=' + opts['platform'] + ' python:', 1))
    cfg = {"task_id": task["id"], "judges": opts["judges"], "parallel": opts.get("judge_parallel", 6),
           "source_commit": COMMIT, "expected_n_criteria": task["n_criteria"],
           "task_sha256": task["task_sha256"], "core_sha256": spec["source"]["files"],
           "document_tree_sha256": spec["source"]["document_trees"][task["documents_path"]]["sha256"]}
    _write(tests / "config.json", json.dumps(cfg, indent=2) + "\n")
    _write(tests / "test.sh", '#!/bin/bash\nset -euo pipefail\n/opt/harvey/.venv/bin/python -I /tests/verify.py\n')
    (tests / "test.sh").chmod(0o755)
    # Unchanged user instructions; conventions describe this SDK's target interface.
    _write(destination / "instruction.md", task_config["instructions"] + "\n\n"
           "Task documents are available read-only under `/workspace/documents`. "
           "Write all final deliverables to `/workspace/output`, preserving the requested filenames and formats. "
           "The evaluator reads saved files, not the final chat message. The original LAB document tools are installed. Read binary files with `parse-doc {docx|pdf|pptx|xlsx} PATH`; this is the command behind the original harness read tool.\n")
    # Native skill manuals/scripts are agent-facing context, never task.json.
    skills = upstream / "lab_core/harness/skills"
    if skills.is_dir():
        _write(destination / "instruction.md", (destination / "instruction.md").read_text() + "\n"
               + "\n\n".join(p.read_text() for p in sorted(skills.glob("*/SKILL.md"))))
        # Tools include companion scripts; mount the immutable shared snapshot read-only
        # through an additional build context, not a host volume.
        main_build = compose["services"]["main"]["build"]
        main_build["additional_contexts"]["harvey-skills"] = str(skills)
        _write(env / "Dockerfile", (env / "Dockerfile").read_text() + "COPY --from=harvey-skills . /workspace/skills\n")
        _write(env / "docker-compose.yaml", json.dumps(compose, indent=2) + "\n")
    env_keys = sorted(runtime_environment(spec))
    toml_env = "\n".join(json.dumps(key) + " = " + json.dumps("${" + key + "}") for key in env_keys)
    _write(destination / "task.toml", f'''version = "1.0"
artifacts = ["/workspace/output"]
[metadata]
benchmark = "harvey-lab"
record_id = {json.dumps(task['id'])}
source_commit = "{COMMIT}"
[agent]
timeout_sec = {float(opts.get('agent_timeout_sec', 7200))}
[environment]
build_timeout_sec = {float(opts.get('build_timeout_sec', 7200))}
cpus = {opts.get('cpus', 2)}
memory_mb = {opts.get('memory_mb', 4096)}
storage_mb = 16384
[verifier]
environment_mode = "separate"
timeout_sec = {float(opts.get('verifier_timeout_sec', 14400))}
[verifier.environment]
build_timeout_sec = {float(opts.get('build_timeout_sec', 7200))}
cpus = {opts.get('cpus', 2)}
memory_mb = {opts.get('memory_mb', 4096)}
storage_mb = 8192
[verifier.env]
{toml_env}
''')
    _write(destination / "provenance.json", json.dumps({"source_commit": COMMIT, "task": task,
           "judge_models": opts["judges"], "judge_profile": "lab-standard-dual-v1" if set(opts["judges"]) == set(DEFAULT_JUDGES) else "custom",
           "documents": spec["source"]["document_trees"][task["documents_path"]],
           "source_files": spec["source"]["files"], "oracle": "not available: no native gold deliverables"}, indent=2) + "\n")


def prepare(spec: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    upstream = snapshot_source(spec, output_dir / ".sources/upstream")
    result = []
    for task in spec["tasks"]:
        # Stable unique leaf names avoid scenario-01 name collisions in Harbor logs.
        name = hashlib.sha256(task["id"].encode()).hexdigest()[:16] + "-" + task["id"].split("/")[-1]
        root = output_dir / name
        _prepare_task(spec, task, upstream, root)
        result.append({"id": task["id"], "path": str(root.resolve())})
    return result


def verifier_failure(spec: dict, trial_dir: Path) -> dict | None:
    """Classify trusted, post-agent native judge failures when no reward exists."""
    directory = Path(trial_dir) / "verifier"
    path = directory / "grading-error.json"
    if not path.is_file():
        return None
    try:
        failure = json.loads(path.read_text())
        if failure.get("source_commit") != COMMIT or failure.get("reward_emitted") is not False:
            return None
        messages = [str(failure["error"])]
        # Dual evaluation raises a summary. Native per-judge error verdicts retain
        # provider diagnostics that distinguish rejected credentials from quota.
        for score_path in sorted(directory.glob("scores*.json")):
            score = json.loads(score_path.read_text())
            for result in score.get("criteria_results", []):
                if result.get("verdict") == "error":
                    messages.append(str(result.get("reasoning", "")))
        detail = "; ".join(dict.fromkeys(messages))[:6000]
    except (ValueError, KeyError, TypeError, OSError):
        return {"status": "infra_error", "error": "Malformed private Harvey grading failure report"}
    lower = detail.lower()
    if any(token in lower for token in ("rate limit", "ratelimit", "quota", "429", "resource_exhausted")):
        status = "quota_exceeded"
    elif any(token in lower for token in ("authentication", "unauthorized", "invalid api key", "invalid_api_key", "401")):
        status = "auth_error"
    else:
        status = "infra_error"
    return {"status": status, "error": detail}
