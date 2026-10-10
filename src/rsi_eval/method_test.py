"""Independently evaluate a native method's measured selected artifact."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path

from . import BenchmarkSelection, RunOptions, TargetSpec
from .artifacts import artifact_hash, snapshot
from .evaluation import _json_write
from .resume import evaluation_lock
from integrations.runtime_options import effective_options, normalize_selection


def _read(path: Path) -> dict:
    from .relocation import relocate
    return relocate(json.loads(path.read_text()), path)


def _measured(report: dict, expected: str) -> None:
    if (report.get("status") != "completed" or not report.get("trials")
            or report.get("resolved", {}).get("target", {}).get("sha256") != expected
            or any(t.get("status") not in {"completed", "task_timeout", "agent_error"}
                   or t.get("reward") is None for t in report["trials"])):
        raise ValueError("Selected candidate lacks a complete matching evaluation report")


def selected_target(record: dict) -> tuple[TargetSpec, dict, dict]:
    """Use method-owned selection evidence, never the last editable workspace."""
    root = Path(record["experiment"])
    run, method = root / "run", record["method"]
    config = record["configuration"]
    if method == "rrsi":
        from integrations.rrsi.__main__ import frozen_incumbent
        target, config, evidence = frozen_incumbent(root / "checkout", run)
        return target, config, {"method": method, **evidence}
    if method == "meta-harness":
        from integrations.meta_harness.launcher import EXAMPLE, ENTRYPOINT, _files
        checkpoint = _read(run / "resume.json")
        bindings = checkpoint["bindings"]
        frontier = _read(run / "upstream" / EXAMPLE / "logs/search/frontier_val.json")
        winner = frontier["_best"]["agent"]
        measured = bindings.get("measured", {}).get(winner)
        if not measured or not measured.get("evaluations"):
            raise ValueError("Native frontier has no complete measurement for its selected candidate")
        candidate = Path(measured["artifact_dir"])
        expected_files = (checkpoint["identity"]["template"] if winner == "codex-baseline"
                          else bindings["validated"].get(winner))
        if expected_files is None or _files(candidate) != expected_files:
            raise ValueError("Selected Meta-Harness artifact changed after measurement")
        expected = artifact_hash(candidate)
        for report in measured["evaluations"]:
            _measured(report, expected)
        codex = config["codex"]
        target = TargetSpec(candidate, ENTRYPOINT, {
            "codex_auth_dir": codex["auth_dir"], "codex_version": codex["expected_version"],
            "reasoning_effort": codex.get("reasoning_effort", "medium")}, codex["model"])
        return target, config, {"method": method, "winner": winner,
                                "measurement": str(run / "resume.json")}
    if method == "ahe":
        from integrations.ahe.launcher import _target
        summary = _read(run / "summary.json")
        candidate, expected = Path(summary["selected_candidate"]), summary["selected_sha256"]
        if artifact_hash(candidate) != expected:
            raise ValueError("Selected AHE artifact changed after measurement")
        matches = [row for row in summary["evaluations"] if row["candidate_sha256"] == expected]
        if not matches:
            raise ValueError("Selected AHE artifact has no measured export")
        # Prefer its original evolve report; a same-content KEEP iteration is also valid.
        _measured(_read(Path(matches[0]["output_dir"]) / "integration_result.json"), expected)
        return _target(candidate, config), config, {"method": method, "best": summary["best"],
                                                   "measurement": matches[0]["output_dir"]}
    if method == "harnessx":
        from integrations.harnessx.launcher import runtime_environment, target_for
        state = _read(run / "result.json")
        candidate = Path(state["current_candidate"])
        rows = [state["baseline"], *state["rounds"]]
        row = next((r for r in reversed(rows) if Path(r["candidate"]) == candidate), None)
        if row is None or artifact_hash(candidate) != row["candidate_hash"]:
            raise ValueError("Selected HarnessX bundle differs from its measured candidate")
        _measured(_read(Path(row["report_path"])), row["candidate_hash"])
        runtime_environment(config)
        return target_for(config, candidate), config, {"method": method, "measurement": row["report_path"]}
    raise ValueError(f"Unsupported method: {method}")


def load_selections(path: Path) -> list[dict]:
    value = json.loads(path.read_text())
    selections = value if isinstance(value, list) else [value]
    if not selections or any(not isinstance(item, dict) for item in selections):
        raise ValueError("Selection JSON must contain a benchmark selection object or nonempty list")
    return [normalize_selection(item, path.resolve().parent) for item in selections]


def _options(method: str, config: dict, selected: dict) -> RunOptions:
    options = effective_options(config.get("run_options", {}), selected)
    if method == "meta-harness":
        options.setdefault("harbor_executable", config.get("harbor_executable", "harbor"))
        options.setdefault("environment", config.get("environment", "docker"))
        options.setdefault("timeout", config.get("evaluation_timeout"))
        options.update(repetitions=1, concurrency=1)
    elif method == "rrsi":
        options["repetitions"] = config["rrsi"]["k"]
    elif method == "harnessx" and options.get("concurrency", 1) != 1:
        raise ValueError("Codex subscription integration requires concurrency=1")
    return RunOptions(**options)


def _plans(method: str, config: dict, selections: list[dict], output: Path) -> tuple[list, list]:
    """Parse every request once, retaining errors instead of stopping at the first."""
    plans, rows = [], []
    for index, selected in enumerate(selections):
        row = {"index": index, "benchmark": selected.get("name") if isinstance(selected, dict) else None,
               "version": selected.get("version") if isinstance(selected, dict) else None}
        try:
            if not isinstance(selected, dict):
                raise ValueError("Each test selection must be an object")
            value = {key: value for key, value in selected.items()
                     if key not in {"benchmark_runtime", "runtime_snapshot"}}
            if value.get("task_ids") is not None:
                value["task_ids"] = tuple(value["task_ids"])
            if value.get("manifest"):
                value["manifest"] = Path(value["manifest"])
            plan = {"benchmark": BenchmarkSelection(**value),
                    "options": _options(method, config, selected),
                    "output_dir": output / f"benchmark-{index:03d}"}
        except Exception as exc:
            plan = None
            row.update(ok=False, error_type=type(exc).__name__, detail=str(exc))
        plans.append(plan)
        rows.append(row)
    return plans, rows


def _target_checks(method: str, config: dict) -> dict:
    # Generic check() recognizes the Codex wrapper. HarnessX is a custom Harbor
    # agent, so check its host-side authentication/version explicitly as well.
    if method != "harnessx":
        return {}
    from .codex import CodexSettings, codex_version, validate_auth
    try:
        settings = CodexSettings(**config["codex"])
        validate_auth(settings.auth_dir)
        return {"codex": {"ok": True, "version": codex_version(settings)}}
    except Exception as exc:
        return {"codex": {"ok": False, "error_type": type(exc).__name__, "detail": str(exc)}}


def _preflight(target: TargetSpec, method: str, config: dict, plans: list, rows: list,
               *, resume: bool, checker) -> dict:
    from .evaluation import preview
    from .resume import check_resume
    target_checks = _target_checks(method, config)
    for plan, row in zip(plans, rows):
        if plan is None:
            continue
        try:
            resolved = preview(target=target, **plan)
            row["task_ids"] = resolved["benchmark"]["task_ids"]
            row["expected_trials"] = resolved["expected_trials"]
            result = checker(target=target, benchmark=plan["benchmark"], options=plan["options"],
                             codex_executable=config.get("codex", {}).get("executable", "codex"))
            row.update(result)
            # Fail closed if an extension fails to return a boolean success.
            row["ok"] = result.get("ok") is True
            destination = plan["output_dir"]
            if resume and destination.exists() and any(destination.iterdir()):
                row["resume"] = check_resume(destination, target=target,
                                              benchmark=plan["benchmark"], options=plan["options"])
                row["ok"] = row["ok"] and row["resume"].get("ok") is True
                plan["resume"] = True
        except Exception as exc:
            row.update(ok=False, error_type=type(exc).__name__, detail=str(exc))
    return {"ok": all(row["ok"] for row in rows) and all(row["ok"] for row in target_checks.values()),
            "method": method, "model_calls": 0, "target_checks": target_checks, "checks": rows}


def test_selected(record: dict, output: Path, *, selections: list[dict] | None = None,
                  resume: bool = False, evaluator=None, checker=None) -> dict:
    """Run additional tests without changing native history, gates or selection."""
    from . import check, evaluate
    evaluator = evaluator or evaluate
    checker = checker or check
    output = output.expanduser().resolve()
    root = Path(record["experiment"]).resolve()
    for protected in (root, *(root / name for name in
                             ("run", "runtime", "seed", "checkout", "upstream-source", "environment"))):
        if output == protected or (protected != root and output.is_relative_to(protected)) or protected.is_relative_to(output):
            raise ValueError("Independent test output must not overlap managed experiment inputs or native run state")
    target, config, evidence = selected_target(record)
    if selections is None:
        values = config.get("test")
        selections = values if isinstance(values, list) else [values] if values else []
    if not selections:
        raise ValueError("Configure test selections or provide --selection JSON")
    plans, rows = _plans(record["method"], config, selections, output)
    identity = {"method": record["method"], "candidate_sha256": artifact_hash(target.artifact_dir),
                "source_candidate": str(target.artifact_dir), "evidence": evidence,
                "selections": selections, "target": {"entrypoint": target.entrypoint,
                "config": target.config, "model": target.model},
                "options": [vars(plan["options"]) if plan else None for plan in plans]}
    if not resume and output.exists() and any(output.iterdir()):
        raise FileExistsError("Independent test output exists; use --resume or a new output directory")
    if resume and not (output / "request.json").is_file():
        raise ValueError("Independent test resume requires its recorded request")
    output.mkdir(parents=True, exist_ok=True)
    with evaluation_lock(output):
        if resume:
            if _read(output / "request.json") != identity:
                raise ValueError("Independent test selection, measured candidate or runtime options changed")
            if artifact_hash(output / "candidate") != identity["candidate_sha256"]:
                raise ValueError("Independent test candidate snapshot changed")
        else:
            if snapshot(target.artifact_dir, output / "candidate") != identity["candidate_sha256"]:
                raise ValueError("Selected candidate changed while being snapshotted")
            _json_write(output / "request.json", identity)
        target = replace(target, artifact_dir=output / "candidate")
        preflight = _preflight(target, record["method"], config, plans, rows, resume=resume, checker=checker)
        _json_write(output / "preflight.json", preflight)
        if not preflight["ok"]:
            previous = _read(output / "method-test.json") if resume and (output / "method-test.json").is_file() else {}
            summary = {"status": "preflight_failed", "method": record["method"],
                       "candidate_sha256": identity["candidate_sha256"], "evidence": evidence,
                       "evaluations": previous.get("evaluations", []), "preflight": preflight,
                       "error": "Batch preflight failed; no evaluations started by this invocation"}
            # Failed checks must never overwrite existing scored-run evidence.
            if not previous:
                _json_write(output / "method-test.json", summary)
            return summary
        summary = {"status": "running", "method": record["method"], "candidate_sha256": identity["candidate_sha256"],
                   "evidence": evidence, "preflight": preflight, "evaluations": []}
        try:
            for plan in plans:
                result = evaluator(target=target, **plan)
                summary["evaluations"].append(result.to_dict())
                if result.status != "completed":
                    summary["status"] = result.status
                    break
            else:
                summary["status"] = "completed"
        except BaseException as exc:
            summary.update(status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                           error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            _json_write(output / "method-test.json", summary)
        return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        record = _read(args.experiment / "method.json")
        result = test_selected(record, args.output,
                               selections=load_selections(args.selection) if args.selection else None,
                               resume=args.resume)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "completed" else 1
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"Method test: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
