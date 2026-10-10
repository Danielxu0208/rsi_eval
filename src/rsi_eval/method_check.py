"""No-inference prerequisite checks for methods without a native check command."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from . import BenchmarkSelection, RunOptions, TargetSpec, check
from .codex import CodexSettings, codex_version, validate_auth
from .methods import METHODS


def inspect_experiment(root: Path) -> dict:
    from integrations.runtime_options import effective_options
    record = json.loads((root / "method.json").read_text())
    method = record["method"]
    config = json.loads((root / "configuration.json").read_text())
    if method == "rrsi":
        from integrations.rrsi.__main__ import checked_config
        config = checked_config(root / "checkout")
    elif method == "meta-harness":
        from integrations.meta_harness.launcher import normalize_config, validate_evolve_scoring
        config = normalize_config(config, root)
        validate_evolve_scoring(config["evolve"], config.get("method_policy"))
        if config.get("skip_smoke", False):
            config.pop("smoke", None)
        elif "smoke" not in config:
            evolve = config["evolve"] if isinstance(config["evolve"], list) else [config["evolve"]]
            if any(not selection.get("task_ids") for selection in evolve):
                raise ValueError("Specify smoke selection when evolve uses a manifest or all tasks")
            config["smoke"] = [{**selection, "task_ids": selection["task_ids"][:1]} for selection in evolve]
    else:
        raise ValueError("Use the method's native check entry point")
    pin = subprocess.run(["git", "-C", str(root / "upstream-source"), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    if pin != METHODS[method]["commit"]:
        raise ValueError("Upstream source pin changed")
    codex = config["codex"]
    settings = CodexSettings(**codex)
    validate_auth(settings.auth_dir)
    version = codex_version(settings)
    target_config = {
        "codex_auth_dir": str(settings.auth_dir), "codex_version": version,
        "reasoning_effort": settings.reasoning_effort,
    }
    if method == "rrsi":
        target_config.update(config.get("target_config", {}))
    target = TargetSpec(root / "seed", "rsi_eval.harbor_codex:Agent", target_config,
                        config.get("policy_model", settings.model) if method == "rrsi" else settings.model)
    reports = []
    for role in ("evolve", "smoke", "test"):
        selections = config.get(role, [])
        selections = selections if isinstance(selections, list) else [selections]
        for selection in selections:
            values = {k: selection[k] for k in ("name", "version", "task_ids", "manifest") if k in selection}
            if values.get("manifest"):
                values["manifest"] = Path(values["manifest"])
            options = effective_options(config.get("run_options", {}), selection)
            options.setdefault("harbor_executable", config.get("harbor_executable", str(Path(record["environment"]) / "bin/harbor")))
            if method == "meta-harness":
                options.setdefault("environment", config.get("environment", "docker"))
                options.setdefault("timeout", config.get("evaluation_timeout"))
                options.update(repetitions=1, concurrency=1)
            else:
                options["repetitions"] = 1 if role == "smoke" else config["rrsi"]["k"]
                if role == "smoke":
                    options["concurrency"] = 1
            result = check(target=target, benchmark=BenchmarkSelection(**values), options=RunOptions(**options),
                           codex_executable=settings.executable)
            reports.append({"role": role, "benchmark": selection["name"], **result})
    return {"ok": all(r["ok"] for r in reports), "method": method,
            "checks": reports, "codex_version": version, "model_calls": 0}


if __name__ == "__main__":
    report = inspect_experiment(Path(sys.argv[1]).resolve())
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ok"] else 1)
