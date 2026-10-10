"""Launch the pinned upstream algorithm and evaluate its frozen artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from . import DOMAIN_NAME, UPSTREAM_COMMIT
from .prepare import TOOLKIT_ROOT, git, normalize_config, prepare, verify_frozen_selections
from integrations.runtime_options import normalize_selection


def checked_config(checkout: Path) -> dict:
    marker = json.loads((checkout / ".rsi-eval-integration.json").read_text())
    if marker.get("upstream_commit") != UPSTREAM_COMMIT:
        raise ValueError("Not a supported, pinned RRSI integration checkout")
    config = json.loads((checkout / "domains" / DOMAIN_NAME / "experiment.json").read_text())
    if hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest() != marker.get("config_sha256"):
        raise ValueError("Experiment configuration changed after preparation; prepare a new checkout")
    if git(checkout, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Managed checkout has tracked changes; prepare a clean experiment")
    from rsi_eval.relocation import relocate
    config = relocate(config, checkout)
    verify_frozen_selections(config)
    return config


def child_environment(checkout: Path, runs: Path) -> dict:
    paths = [str(checkout), str(TOOLKIT_ROOT / "src"), str(TOOLKIT_ROOT)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    return {**os.environ, "PYTHONPATH": os.pathsep.join(paths),
            "RRSI_RUN_DIR": str(runs / DOMAIN_NAME),
            "RRSI_CODEX_LOG_DIR": str(runs / DOMAIN_NAME / "codex_roles"),
            "PYTHONDONTWRITEBYTECODE": "1"}


def load_selection(path: Path) -> dict:
    path = path.expanduser().resolve()
    selection = json.loads(path.read_text())
    if not isinstance(selection, dict):
        raise ValueError("Selection file must contain a benchmark selection object")
    if selection.get("manifest"):
        manifest = Path(selection["manifest"]).expanduser()
        selection["manifest"] = str((path.parent / manifest).resolve())
    return normalize_selection(selection, path.parent)


def run_native(checkout: Path, runs: Path, command: str, extra: list[str] | None = None) -> int:
    checked_config(checkout)
    stop = runs / DOMAIN_NAME / "STOP"
    if command in {"run", "baseline", "smoke"} and stop.exists():
        raise RuntimeError(f"STOP is present at {stop}; resolve the recorded failure before removing it to resume")
    code = subprocess.call(
        [sys.executable, str(checkout / "rrsi.py"), "--domain", DOMAIN_NAME,
         "--runs", str(runs), command, *(extra or [])],
        cwd=checkout, env=child_environment(checkout, runs),
    )
    if command in {"run", "baseline", "smoke"} and stop.exists():
        raise RuntimeError(f"RRSI stopped at {stop}; inspect {stop.parent / 'fatal_error.json'} before resuming")
    return code


def resume_native(checkout: Path, runs: Path) -> int:
    """Explicitly retry an interrupted run, preserving its stop and error records."""
    from datetime import datetime, timezone
    import shutil

    config = checked_config(checkout)
    directory = runs / DOMAIN_NAME
    if not directory.is_dir():
        raise ValueError("Resume requires an existing managed RRSI run")
    frontier_file = directory / "frontier.json"
    frontier = json.loads(frontier_file.read_text()) if frontier_file.is_file() else {}
    start = max(0, len(frontier.get("trajectory", [])) - 1)
    if start >= config["rrsi"]["T"]:
        raise ValueError("RRSI run already completed its configured rounds")
    stop = directory / "STOP"
    if stop.exists():
        error = directory / "fatal_error.json"
        if not error.is_file() or json.loads(error.read_text()).get("reason") not in {"auth_error", "quota_exceeded"}:
            raise ValueError("Unknown STOP marker: inspect its cause before explicitly resolving it")
        archive = directory / "resume_history" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        archive.mkdir(parents=True)
        shutil.move(str(stop), str(archive / "STOP"))
        shutil.move(str(error), str(archive / "fatal_error.json"))
        (archive / "resume.json").write_text(json.dumps({"requested": "explicit resume", "start": start}, indent=2))
    return run_native(checkout, runs, "run", ["--start", str(start)])


def audit(checkout: Path, runs: Path, rounds: int | None = None) -> dict:
    config = checked_config(checkout)
    required = rounds if rounds is not None else config["rrsi"]["T"]
    run = runs / DOMAIN_NAME
    frontier = json.loads((run / "frontier.json").read_text())
    if len(frontier["trajectory"]) < required + 1:
        raise ValueError(f"Only {len(frontier['trajectory']) - 1}/{required} rounds settled")
    evaluated = []
    for index in range(required):
        rdir = run / f"r{index}"
        decisions = json.loads((rdir / "decisions.json").read_text())
        candidates = []
        for path in sorted(rdir.glob("*/eval.json")):
            record = json.loads(path.read_text())
            diff = path.parent / "diff.patch"
            if not diff.exists() or not diff.read_text().strip():
                continue
            if record.get("missing") or record.get("C") is None:
                continue
            critic_path, smoke_path = path.parent / "critic.json", path.parent / "smoke.json"
            if not critic_path.is_file() or not smoke_path.is_file():
                continue
            critic, smoke = json.loads(critic_path.read_text()), json.loads(smoke_path.read_text())
            if critic.get("verdict") != "accept" or not smoke.get("ok") or smoke.get("stage") != "real_task_liveness":
                continue
            smoke_report = Path(smoke.get("report", ""))
            if not smoke_report.is_file() or json.loads(smoke_report.read_text()).get("status") != "completed":
                continue
            if not any(d.get("variant") == path.parent.name and d.get("S") is not None for d in decisions):
                continue
            candidates.append({"variant": path.parent.name, "evaluation": str(path),
                               "score": record["S"], "policy_tokens": record["C"]})
        if not candidates:
            raise ValueError(f"Round {index} has no real, changed, fully measured candidate with a selection decision")
        evaluated.append({"round": index, "candidates": candidates})
    return {"upstream_commit": UPSTREAM_COMMIT, "rounds": evaluated,
            "incumbent": frontier["incumbent"],
            "note": "Acceptance or improved scores are not required."}


def completion_report(checkout: Path, runs: Path, rounds: int | None = None) -> dict:
    """Separate a settled native search from the stronger integration audit."""
    config = checked_config(checkout)
    required = rounds if rounds is not None else config["rrsi"]["T"]
    directory = runs / DOMAIN_NAME
    frontier = json.loads((directory / "frontier.json").read_text())
    if (directory / "STOP").exists() or len(frontier["trajectory"]) != required + 1:
        raise ValueError("RRSI has not settled every configured round")
    baseline = json.loads((directory / "jobs" / frontier["trajectory"][0]["job"] / "eval.json").read_text())
    if baseline.get("missing") or baseline.get("C") is None:
        raise ValueError("RRSI baseline has missing measurements or usage")
    outcomes = []
    for index in range(required):
        rdir = directory / f"r{index}"
        decisions = json.loads((rdir / "decisions.json").read_text())
        if len(decisions) != config["rrsi"]["m"]:
            raise ValueError(f"Round {index} has incomplete selection decisions")
        for decision in decisions:
            if decision.get("reason") in {"eval_invalid", "not evaluated"}:
                raise ValueError(f"Round {index} has an unresolved evaluation failure: {decision}")
            variant = rdir / decision["variant"]
            if decision.get("S") is not None:
                evaluation = json.loads((variant / "eval.json").read_text())
                if evaluation.get("missing") or evaluation.get("C") is None:
                    raise ValueError(f"Round {index} has missing measurements or policy usage")
            smoke_path = variant / "smoke.json"
            if smoke_path.is_file():
                smoke = json.loads(smoke_path.read_text())
                if smoke.get("stage") == "real_task_liveness":
                    report = json.loads(Path(smoke["report"]).read_text())
                    if report.get("status") != "completed" or not report.get("trials") or any(
                        trial.get("status") not in {"completed", "task_timeout", "agent_error"}
                        for trial in report["trials"]):
                        raise ValueError(f"Round {index} has an unresolved smoke infrastructure failure")
        outcomes.append({"round": index, "decisions": decisions})
    try:
        acceptance = {"passed": True, **audit(checkout, runs, required)}
    except ValueError as exc:
        acceptance = {"passed": False, "detail": str(exc)}
    return {"status": "completed", "rounds": outcomes, "incumbent": frontier["incumbent"],
            "acceptance": acceptance, "acceptance_passed": acceptance["passed"]}


def frozen_incumbent(checkout: Path, runs: Path):
    """Resolve the native selected commit and prove it is the measured artifact."""
    # Loading this bridge needs the pinned upstream Domain base class.
    sys.path.insert(0, str(checkout))
    from .domain_bridge import tree_digest, target_from_config
    config = checked_config(checkout)
    fr = json.loads((runs / DOMAIN_NAME / "frontier.json").read_text())
    commit = git(checkout, "rev-parse", fr["incumbent"]["commit"])
    harness_tree = git(checkout, "rev-parse", f"{commit}:domains/{DOMAIN_NAME}/harness")
    if harness_tree[:12] != fr["incumbent"]["harness_tree"]:
        raise ValueError("Incumbent commit no longer matches its recorded harness tree")
    worktree = runs / DOMAIN_NAME / "final_artifacts" / commit
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git(checkout, "worktree", "add", "--detach", str(worktree), commit)
    elif git(worktree, "rev-parse", "HEAD") != commit or git(worktree, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Frozen test worktree no longer matches its incumbent commit")
    artifact = worktree / "domains" / DOMAIN_NAME / "harness"
    job = runs / DOMAIN_NAME / "jobs" / fr["incumbent"]["job"]
    request = json.loads((job / "request.json").read_text())
    measured = json.loads((job / "eval.json").read_text())
    if tree_digest(artifact) != request["candidate_sha256"] or measured.get("missing") or measured.get("C") is None:
        raise ValueError("Incumbent artifact does not match a complete measured candidate")
    return target_from_config(artifact, config), config, {"commit": commit, "harness_tree": harness_tree,
                                                        "measurement": str(job / "eval.json")}


def test_incumbent(checkout: Path, runs: Path, output: Path, selection: dict | None = None):
    from rsi_eval import evaluate
    target, config, evidence = frozen_incumbent(checkout, runs)
    from .domain_bridge import options_from_dict, selection_from_dict
    selection = selection or config.get("test")
    if not selection:
        raise ValueError("Provide a test benchmark selection in the experiment config or --selection")
    result = evaluate(
        target=target, benchmark=selection_from_dict(selection),
        options=options_from_dict(config.get("run_options", {}), config["rrsi"]["k"], selection), output_dir=output,
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "incumbent.json").write_text(json.dumps({**evidence, "selection": selection}, indent=2))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--source", type=Path, default=TOOLKIT_ROOT / "rrsi")
    p.add_argument("--checkout", type=Path, required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--seed", type=Path)
    for name in ("run", "resume", "baseline", "smoke", "status", "audit", "test"):
        p = sub.add_parser(name)
        p.add_argument("--checkout", type=Path, required=True)
        p.add_argument("--runs", type=Path, required=True)
        if name == "run":
            p.add_argument("--start", type=int, default=0)
        if name == "audit":
            p.add_argument("--rounds", type=int)
        if name == "test":
            p.add_argument("--output", type=Path, required=True)
            p.add_argument("--selection", type=Path, help="JSON BenchmarkSelection, independent of evolve selection")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            config = normalize_config(json.loads(args.config.read_text()), args.config.resolve().parent)
            print(json.dumps(prepare(args.source, args.checkout, config, seed=args.seed), indent=2))
            return 0
        checkout, runs = args.checkout.resolve(), args.runs.resolve()
        if args.command == "audit":
            print(json.dumps(audit(checkout, runs, args.rounds), indent=2))
            return 0
        if args.command == "test":
            selection = load_selection(args.selection) if args.selection else None
            result = test_incumbent(checkout, runs, args.output.resolve(), selection)
            print(json.dumps(result.to_dict(), indent=2))
            return 0 if result.status == "completed" else 1
        if args.command == "resume":
            code = resume_native(checkout, runs)
            if code == 0:
                print(json.dumps(completion_report(checkout, runs), indent=2))
            return code
        extra = ["--start", str(args.start)] if args.command == "run" else []
        code = run_native(checkout, runs, args.command, extra)
        if code == 0 and args.command == "run":
            print(json.dumps(completion_report(checkout, runs), indent=2))
        return code
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"RRSI integration: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
