"""Pinned task catalogs. Task roles belong to the calling method."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any

from .types import BenchmarkSelection

_EXTRA: dict[tuple[str, str], dict[str, Any]] = {}


def register_benchmark(spec: dict[str, Any]) -> None:
    """Register a pinned benchmark configuration in this process.

    A spec contains name, version, dataset_ref and tasks. Each task has an id
    and either a package name/ref or a git_url/git_commit_id/path. Bundled
    materializers also accept pinned data records with an explicit protocol.
    """
    _validate_spec(spec)
    key = (spec["name"], spec["version"])
    if key in {(s["name"], s["version"]) for s in list_benchmarks()}:
        raise ValueError(f"benchmark already registered: {key}")
    _EXTRA[key] = deepcopy(spec)


def list_benchmarks() -> list[dict[str, Any]]:
    root = files("rsi_eval").joinpath("data")
    data = []
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        if path.name.startswith("catalog") and path.name.endswith(".json"):
            data.extend(json.loads(path.read_text()))
    return data + list(deepcopy(_EXTRA).values())


def _validate_spec(spec: dict[str, Any]) -> None:
    for key in ("name", "version", "dataset_ref", "tasks"):
        if not spec.get(key):
            raise ValueError(f"benchmark spec requires {key}")
    ids = [t["id"] for t in spec["tasks"]]
    if len(set(ids)) != len(ids):
        raise ValueError("benchmark contains duplicate task IDs")
    aggregation = spec.get("aggregation")
    if aggregation:
        if aggregation.get("type") not in {"pass_at_k", "accuracy_by_language", "drop_em_f1", "frontier_native", "accuracy_by_group", "rubric_mean"}:
            raise ValueError("unknown benchmark aggregation")
        if aggregation.get("type") in {"accuracy_by_group", "rubric_mean"}:
            groups = aggregation.get("group_by", [])
            if not isinstance(groups, list) or any(not isinstance(k, str) or not k for k in groups):
                raise ValueError("group_by must contain metadata field names")
            if any(any(k not in task for k in groups) for task in spec["tasks"]):
                raise ValueError("group_by fields must exist on every task")
        if aggregation["type"] == "pass_at_k" and any(
            not isinstance(k, int) or isinstance(k, bool) or k < 1 for k in aggregation.get("k", [1])):
            raise ValueError("pass@k requires positive integer k")
    if spec.get("materializer"):
        from .materializers import MATERIALIZERS
        if spec["materializer"] not in MATERIALIZERS:
            raise ValueError(f"unknown materializer: {spec['materializer']}")
        source = spec.get("source", {})
        pinned = any(re.fullmatch(r"[0-9a-f]{40}", str(source.get(key, "")))
                     for key in ("commit", "revision", "git_commit_id"))
        pinned = pinned or bool(re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))))
        if not pinned:
            raise ValueError("materialized benchmarks require an immutable source revision or SHA256")
        if not spec.get("protocol"):
            raise ValueError("materialized benchmarks require an explicit protocol")
        for task in spec["tasks"]:
            if not isinstance(task["id"], str) or not task["id"]:
                raise ValueError("task IDs must be nonempty strings")
        return
    for task in spec["tasks"]:
        if "name" in task:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", task.get("ref", "")):
                raise ValueError("package tasks require an immutable sha256 ref")
        elif "git_url" in task:
            if not re.fullmatch(r"[0-9a-f]{40}", task.get("git_commit_id", "")):
                raise ValueError("git tasks require a full commit ID")
            if not task.get("path"):
                raise ValueError("git tasks require a repository-relative path")
        else:
            raise ValueError("task must have a pinned package or git source")


def resolve(selection: BenchmarkSelection) -> dict[str, Any]:
    matches = [s for s in list_benchmarks()
               if s["name"] == selection.name and s["version"] == selection.version]
    if len(matches) != 1:
        raise ValueError(f"unknown benchmark: {selection.name}@{selection.version}")
    spec = deepcopy(matches[0])
    _validate_spec(spec)
    ids = selection.task_ids
    if selection.manifest is not None:
        if ids is not None:
            raise ValueError("choose task_ids or manifest, not both")
        value = json.loads(Path(selection.manifest).read_text())
        ids = value.get("task_ids") if isinstance(value, dict) else value
        if not isinstance(ids, list) or not all(isinstance(t, str) for t in ids):
            raise ValueError("manifest must be a JSON list or {task_ids: [...]} of IDs")
    if ids is not None:
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("task selection must be nonempty with no duplicate IDs")
        tasks = {t["id"]: t for t in spec["tasks"]}
        missing = sorted(set(ids) - tasks.keys())
        if missing:
            raise ValueError(f"unknown task IDs for {selection.name}@{selection.version}: {missing}")
        spec["tasks"] = [tasks[t] for t in ids]
    spec["selection"] = "full" if ids is None else "subset"
    if ids is None and spec.get("coverage", {}).get("scope") in {"reviewed-open-subset", "reviewed-cpu-subset"}:
        spec["selection"] = "full_supported_subset"
    spec["task_ids"] = [t["id"] for t in spec["tasks"]]
    return spec
