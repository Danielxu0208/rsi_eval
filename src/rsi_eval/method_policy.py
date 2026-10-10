"""Explicit, trusted method objectives without altering benchmark results.

Callbacks are experiment code, not sandboxed plugins. They must be standalone
Python files with standard-library imports only. Their exact bytes are embedded
in the normalized configuration and verified on every call, so candidates and
later edits to the original file cannot change a running experiment's policy.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def normalize_policy(value: dict | None, base: Path, *, candidate_roots=()) -> dict:
    """Freeze callback source and objective settings in trusted run identity."""
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict) or set(value) - {"score", "selection", "variant"}:
        raise ValueError("method_policy accepts score, selection and variant")
    result = deepcopy(value)
    result.setdefault("variant", "explicit-method-policy")
    if not isinstance(result["variant"], str) or not result["variant"].strip():
        raise ValueError("method_policy.variant must name the method variant")
    for name in ("score", "selection"):
        if name not in result:
            continue
        spec = result[name]
        if not isinstance(spec, dict):
            raise ValueError(f"method_policy.{name} must be an object")
        kind = spec.get("kind")
        allowed = {"python"} if name == "selection" else {"python", "threshold", "linear"}
        if kind not in allowed:
            raise ValueError(f"unsupported {name} policy kind: {kind}")
        if kind == "python":
            if not isinstance(spec.get("function"), str) or not spec["function"].isidentifier():
                raise ValueError("Python policies require a function name")
            path = Path(spec["path"]).expanduser()
            path = (base / path).resolve() if not path.is_absolute() else path.resolve()
            if any(path.is_relative_to(Path(root).resolve()) for root in candidate_roots):
                raise ValueError("Method policy must be outside evolvable candidate directories")
            source = spec.get("source")
            if source is None:
                source = path.read_text()
            if not isinstance(source, str):
                raise ValueError("Python policy source must be text")
            digest = hashlib.sha256(source.encode()).hexdigest()
            if spec.get("sha256") and spec["sha256"] != digest:
                raise ValueError("Method policy source hash mismatch")
            # Compile now; do not execute user code during config preparation.
            compile(source, str(path), "exec")
            spec.update(path=str(path), source=source, sha256=digest)
        else:
            if spec.get("direction", "maximize") not in {"maximize", "minimize"}:
                raise ValueError("score direction must be maximize or minimize")
            spec.setdefault("direction", "maximize")
            spec.setdefault("metric", "reward")
            if not isinstance(spec["metric"], str) or not spec["metric"]:
                raise ValueError("score metric must be a dotted trial field")
            if kind == "threshold":
                _number(spec.get("threshold"), "threshold")
            else:
                low = _number(spec.get("min"), "linear min")
                high = _number(spec.get("max"), "linear max")
                if low >= high:
                    raise ValueError("linear min must be less than max")
    return result


def _call(spec: dict, payload: dict):
    source = spec.get("source")
    if not isinstance(source, str) or hashlib.sha256(source.encode()).hexdigest() != spec.get("sha256"):
        raise ValueError("Method policy must be normalized with a matching pinned source hash")
    namespace = {"__name__": "rsi_eval_trusted_method_policy", "__file__": spec["path"]}
    exec(compile(source, spec["path"], "exec"), namespace)
    callback = namespace.get(spec["function"])
    if not callable(callback):
        raise ValueError("Method policy function is not callable")
    return callback(deepcopy(payload))


def policy_identity(policy: dict | None) -> dict:
    """Small identity for reports; full frozen source stays in run config."""
    if not policy:
        return {}
    return {**{key: {k: v for k, v in spec.items() if k != "source"}
               for key, spec in policy.items() if key in {"score", "selection"}},
            "variant": policy.get("variant", "explicit-method-policy"),
            "sha256": hashlib.sha256(json.dumps(policy, sort_keys=True, allow_nan=False).encode()).hexdigest()}


def score_trial(policy: dict | None, trial: dict, benchmark: dict, *,
                require_binary: bool = False, unit_interval: bool = False) -> float:
    """Map a complete measured trial; missing/infrastructure outcomes stay invalid."""
    if trial.get("status") not in {"completed", "task_timeout", "agent_error"}:
        raise ValueError("Cannot map a missing or infrastructure-failed trial")
    raw = _number(trial.get("reward"), "raw reward")
    spec = (policy or {}).get("score")
    value = raw
    if spec:
        if spec["kind"] == "python":
            value = _call(spec, {"trial": trial, "benchmark": benchmark})
        else:
            value = trial
            for key in spec.get("metric", "reward").split("."):
                if not isinstance(value, dict) or key not in value:
                    raise ValueError(f"Missing policy metric {spec['metric']}")
                value = value[key]
            value = _number(value, "policy metric")
            maximize = spec.get("direction", "maximize") == "maximize"
            if spec["kind"] == "threshold":
                value = float(value >= spec["threshold"] if maximize else value <= spec["threshold"])
            elif spec["kind"] == "linear":
                value = min(1.0, max(0.0, (value - spec["min"]) / (spec["max"] - spec["min"])))
                if not maximize:
                    value = 1.0 - value
            else:
                raise ValueError("Unknown score policy")
    value = _number(value, "mapped score")
    if require_binary and value not in (0.0, 1.0):
        raise ValueError("Method objective requires binary mapped scores")
    if unit_interval and not 0 <= value <= 1:
        raise ValueError("Method objective requires mapped scores in [0, 1]")
    return value


def select_candidate(policy: dict | None, candidates: list[dict], native_choice: str | None,
                     context: dict | None = None) -> str | None:
    """Choose only among candidates whose method-specific gates already passed.

    Returning None means retain the incumbent. A caller exporting a best result
    without an incumbent must reject None itself.
    """
    ids = [c.get("id") for c in candidates]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("Eligible candidate IDs must be nonempty and unique")
    if native_choice is not None and native_choice not in ids:
        raise ValueError("Native selection is outside eligible candidates")
    spec = (policy or {}).get("selection")
    selected = (_call(spec, {"candidates": candidates, "native_choice": native_choice,
                            "context": context or {}}) if spec else native_choice)
    if selected is not None and (not isinstance(selected, str) or selected not in ids):
        raise ValueError("Selection policy cannot select a candidate outside native gates")
    return selected
