"""JSON configuration shared by the CLI and Python examples."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .types import BenchmarkSelection, RunOptions, TargetSpec


def _path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _runtime_paths(value: dict, base: Path) -> dict:
    runtime = dict(value)
    for key in ("assets_dir", "runtime_config", "source_dir", "judge_env_file"):
        if runtime.get(key) is not None:
            runtime[key] = str(_path(runtime[key], base))
    if runtime.get("dataset_python") is not None:
        # Resolving a venv's final python symlink selects its base interpreter
        # and loses the dataset dependencies installed in that environment.
        interpreter = Path(os.path.expandvars(runtime["dataset_python"])).expanduser()
        runtime["dataset_python"] = os.path.abspath(interpreter if interpreter.is_absolute() else base / interpreter)
    if "task_overrides" in runtime:
        overrides = runtime["task_overrides"]
        if not isinstance(overrides, dict) or any(not isinstance(item, dict) for item in overrides.values()):
            raise ValueError("options.benchmark_runtime.task_overrides must map task IDs to objects")
        runtime["task_overrides"] = {task_id: _runtime_paths(item, base) for task_id, item in overrides.items()}
    return runtime


def load_config(path: Path | str) -> tuple[TargetSpec, BenchmarkSelection, RunOptions, Path]:
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    unknown = value.keys() - {"target", "benchmark", "options", "output_dir"}
    if unknown:
        raise ValueError(f"unknown run config keys: {sorted(unknown)}")
    target_data = dict(value["target"])
    target_data["artifact_dir"] = _path(target_data["artifact_dir"], path.parent)
    target_data["config"] = dict(target_data.get("config", {}))
    for key in ("codex_auth_dir",):
        if key in target_data["config"]:
            target_data["config"][key] = str(_path(target_data["config"][key], path.parent))
    benchmark_data = dict(value["benchmark"])
    if benchmark_data.get("manifest") is not None:
        benchmark_data["manifest"] = _path(benchmark_data["manifest"], path.parent)
    if benchmark_data.get("task_ids") is not None:
        benchmark_data["task_ids"] = tuple(benchmark_data["task_ids"])
    options_data = dict(value.get("options", {}))
    if options_data.get("runtime_snapshot") is not None:
        options_data["runtime_snapshot"] = str(_path(options_data["runtime_snapshot"], path.parent))
    if "benchmark_runtime" in options_data:
        runtime = options_data["benchmark_runtime"]
        if not isinstance(runtime, dict):
            raise ValueError("options.benchmark_runtime must be an object")
        options_data["benchmark_runtime"] = _runtime_paths(runtime, path.parent)
    executable = options_data.get("harbor_executable", "harbor")
    if "/" in executable:
        options_data["harbor_executable"] = str(_path(executable, path.parent))
    return (TargetSpec(**target_data), BenchmarkSelection(**benchmark_data), RunOptions(**options_data),
            _path(value.get("output_dir", "runs/eval"), path.parent))
