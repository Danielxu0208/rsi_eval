"""Configuration helpers shared by the optional method examples."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from rsi_eval.config import _path, _runtime_paths


def normalize_run_options(value: dict, base: Path) -> dict:
    if not isinstance(value, dict):
        raise ValueError("run_options must be an object")
    options = deepcopy(value)
    if options.get("runtime_snapshot") is not None:
        options["runtime_snapshot"] = str(_path(options["runtime_snapshot"], base))
    if "benchmark_runtime" in options:
        if not isinstance(options["benchmark_runtime"], dict):
            raise ValueError("benchmark_runtime must be an object")
        options["benchmark_runtime"] = _runtime_paths(options["benchmark_runtime"], base)
    executable = options.get("harbor_executable")
    if executable is not None and "/" in executable:
        options["harbor_executable"] = str(_path(executable, base))
    return options


def normalize_selection(value: dict, base: Path) -> dict:
    selection = deepcopy(value)
    if selection.get("runtime_snapshot") is not None:
        selection["runtime_snapshot"] = str(_path(selection["runtime_snapshot"], base))
    if selection.get("manifest"):
        selection["manifest"] = str(_path(selection["manifest"], base))
    if "benchmark_runtime" in selection:
        if not isinstance(selection["benchmark_runtime"], dict):
            raise ValueError("selection.benchmark_runtime must be an object")
        selection["benchmark_runtime"] = _runtime_paths(selection["benchmark_runtime"], base)
    return selection


def effective_options(defaults: dict, selection: dict | None = None) -> dict:
    options = deepcopy(defaults)
    if selection is not None and "runtime_snapshot" in selection:
        options["runtime_snapshot"] = selection["runtime_snapshot"]
    if selection is not None and "benchmark_runtime" in selection:
        runtime = selection["benchmark_runtime"]
        if not isinstance(runtime, dict):
            raise ValueError("selection.benchmark_runtime must be an object")
        # Replace, rather than merge, unrelated benchmark backend settings.
        options["benchmark_runtime"] = deepcopy(runtime)
    return options
