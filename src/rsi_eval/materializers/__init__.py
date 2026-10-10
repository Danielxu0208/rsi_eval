"""Build immutable Harbor tasks from pinned benchmark data at run time."""
from __future__ import annotations

from importlib import import_module
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from ..artifacts import artifact_hash

MATERIALIZERS = {"livecodebench", "mgsm", "engdesign", "drop", "frontier_eng",
                 "reasoning", "mce", "jobbench", "harvey_lab", "apex_agents"}


def runtime_environment(spec: dict[str, Any], runtime_options: dict[str, Any] | None = None) -> dict[str, str]:
    """Return private backend environment values; callers must never serialize them."""
    module, configured = _configured(spec, runtime_options)
    hook = getattr(module, "runtime_environment", None)
    values = hook(configured) if hook else {}
    if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                           for k, v in values.items()):
        raise ValueError("runtime_environment must return string environment values")
    if values.keys() & {"PATH", "PYTHONPATH", "PYTHONHOME", "HOME", "CODEX_HOME", "DOCKER_HOST", "DOCKER_CONFIG"}:
        raise ValueError("benchmark credentials cannot override runner configuration")
    return values


def verifier_failure(spec: dict[str, Any], trial_dir: Path) -> dict[str, str] | None:
    """Read benchmark-owned verifier diagnostics, never candidate output or logs."""
    name = spec.get("materializer")
    if name not in MATERIALIZERS:
        return None
    module = import_module(f"rsi_eval.materializers.{name}")
    hook = getattr(module, "verifier_failure", None)
    result = hook(spec, trial_dir) if hook else None
    if result is not None and (
        not isinstance(result, dict) or set(result) != {"status", "error"}
        or result.get("status") not in {"auth_error", "quota_exceeded", "infra_error", "incomplete"}
        or not isinstance(result.get("error"), str) or not result["error"]
    ):
        raise ValueError("verifier_failure must return a failure status and error, or None")
    return result


def _configured(spec: dict[str, Any], runtime_options: dict[str, Any] | None):
    name = spec["materializer"]
    if name not in MATERIALIZERS:
        raise ValueError(f"unknown materializer: {name}")
    module = import_module(f"rsi_eval.materializers.{name}")
    configured = deepcopy(spec)
    configured["runtime_options"] = deepcopy(runtime_options or {})
    if runtime_options and not hasattr(module, "preflight"):
        raise ValueError(f"{name} does not support benchmark_runtime options")
    return module, configured


def preflight(spec: dict[str, Any], runtime_options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check task requirements without downloads, model calls or containers."""
    module, configured = _configured(spec, runtime_options)
    hook = getattr(module, "preflight", None)
    checks = hook(configured) if hook else {}
    if not isinstance(checks, dict) or any(
        not isinstance(key, str) or not isinstance(row, dict) or type(row.get("ok")) is not bool
        for key, row in checks.items()
    ):
        raise ValueError("benchmark preflight must return named checks with boolean ok fields")
    try:
        json.dumps(checks, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("benchmark preflight checks must contain finite JSON values") from error
    return checks


def prepare(spec: dict[str, Any], output_dir: Path,
            runtime_options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    module, configured = _configured(spec, runtime_options)
    output_dir.mkdir(parents=True, exist_ok=False)
    tasks = module.prepare(configured, output_dir)
    if [task["id"] for task in tasks] != [task["id"] for task in spec["tasks"]]:
        raise ValueError("prepared tasks do not match the selected IDs and order")
    for task in tasks:
        path = Path(task["path"]).resolve()
        if output_dir.resolve() not in path.parents:
            raise ValueError("prepared task must be inside the run directory")
        for filename in ("task.toml", "instruction.md", "environment/Dockerfile", "tests/test.sh"):
            if not (path / filename).is_file():
                raise ValueError(f"prepared task is missing {filename}: {task['id']}")
        task["path"] = str(path)
        task["sha256"] = artifact_hash(path)
    return tasks
