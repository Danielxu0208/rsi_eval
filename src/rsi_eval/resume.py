"""Explicit continuation of measured trials, with immutable attempt evidence."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import fcntl
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .artifacts import artifact_hash
from .types import BenchmarkSelection, EvaluationResult, RunOptions, TargetSpec, TrialResult

MEASURED = {"completed", "task_timeout", "agent_error"}


def implementation_identity() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    paths = [root / name for name in ("evaluation.py", "resume.py", "artifacts.py", "metrics.py",
             "types.py", "benchmarks.py", "harbor_codex.py", "harbor_docker.py", "codex.py",
             "compose_context.py", "runtime_snapshot.py", "runtime_docker.py", "relocation.py")]
    paths.extend(sorted((root / "materializers").glob("*.py")))
    for path in paths:
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


@contextmanager
def evaluation_lock(output: Path):
    """Serialize writers to an experiment without holding credentials."""
    with (output / ".evaluation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Evaluation is already running: {output}") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _scoped(path: Path, root: Path) -> Path:
    """Reject evidence redirected outside its owning run through any symlink."""
    root = root.resolve()
    path = path.absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError(f"Evaluation evidence escapes its owning run: {path}")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError(f"Native evaluation evidence must not be a symlink: {path}")
        current = current.parent
    return path


def _receipts(directory: Path) -> dict[str, str]:
    paths = [directory / "resolved.json", directory / "harbor.json", directory / "command.json"]
    paths.extend((directory / "native/eval").rglob("*"))
    receipts = {}
    for path in paths:
        _scoped(path, directory)
        if path.is_file():
            receipts[str(path.relative_to(directory))] = _sha(path)
    return receipts


def _verify_receipts(directory: Path, receipts: dict[str, str]) -> None:
    # Comparing the complete file set also catches added trials or verifier logs.
    current = _receipts(directory)
    if current != receipts:
        changed = next((key for key in sorted(set(current) | set(receipts))
                        if current.get(key) != receipts.get(key)), "unknown")
        raise ValueError(f"Recorded evaluation evidence changed: {directory / changed}")


def _validate_attempt(saved: dict, attempt: dict, number: int, child: dict | None = None) -> Path:
    expected_directory = f"resume-attempts/{number:04d}"
    if attempt.get("directory") != expected_directory:
        raise ValueError("Invalid resume attempt directory or ordering")
    slots = attempt.get("slots")
    if not isinstance(slots, dict) or not slots or set(slots) - set(saved["benchmark"]["task_ids"]):
        raise ValueError("Invalid resume attempt task slots")
    counts = set()
    for values in slots.values():
        if not isinstance(values, list) or not values or any(
                type(n) is not int or not 0 <= n < saved["options"]["repetitions"] for n in values):
            raise ValueError("Invalid resume repetition slots")
        if len(values) != len(set(values)) or values != sorted(values):
            raise ValueError("Resume repetition slots must be ordered and unique")
        counts.add(len(values))
    if len(counts) != 1:
        raise ValueError("Resume attempt tasks must have equal repetition counts")
    directory = Path(saved["output_dir"]) / expected_directory
    _scoped(directory, Path(saved["output_dir"]))
    if child is not None:
        expected = deepcopy(saved)
        expected["output_dir"] = str(directory)
        expected["options"]["repetitions"] = next(iter(counts))
        expected["benchmark"]["tasks"] = [t for t in saved["benchmark"]["tasks"] if t["id"] in slots]
        expected["benchmark"]["task_ids"] = [t["id"] for t in expected["benchmark"]["tasks"]]
        expected["benchmark"]["selection"] = "subset"
        _same_request(expected, child)
        if child.get("execution_identity") != saved["execution_identity"]:
            raise ValueError("Resume attempt used a different evaluator implementation")
        if child.get("expected_trials") != len(slots) * next(iter(counts)):
            raise ValueError("Resume attempt expected trial count changed")
    return directory


def _selected_view(result: EvaluationResult, attempts: int, *, write: bool = True) -> None:
    """Validate, and optionally create, the authoritative selected-trial view."""
    from .evaluation import _json_write
    output = Path(result.resolved["output_dir"])
    parent = output / "resume-views"
    _scoped(parent, output)
    if write:
        parent.mkdir(exist_ok=True)
    view = parent / f"{attempts:04d}"
    _scoped(view, output)
    if write:
        view.mkdir(exist_ok=True)
    entries = []
    for number, row in enumerate(result.trials):
        if not row.artifact_dir:
            continue
        source = _scoped(Path(row.artifact_dir), output)
        relative = source.relative_to(output).parts
        initial = len(relative) == 3 and relative[:2] == ("native", "eval")
        retried = (len(relative) == 5 and relative[0] == "resume-attempts"
                   and re.fullmatch(r"[0-9]{4}", relative[1])
                   and relative[2:4] == ("native", "eval"))
        if not (initial or retried) or not (source / "result.json").is_file():
            raise ValueError("Selected trial is not authoritative native evaluation evidence")
        task_name = source.name.rsplit("__", 1)[0] if "__" in source.name else row.task_id.rsplit("/", 1)[-1]
        name = f"{task_name}__resume-{number:06d}"
        link = view / name
        if link.is_symlink():
            if link.resolve() != source.resolve():
                raise ValueError("Selected trial view was redirected")
        elif link.exists():
            raise ValueError("Selected trial view contains unexpected content")
        elif write:
            link.symlink_to(source, target_is_directory=True)
        entries.append({"view": name, "task_id": row.task_id, "repetition": row.repetition,
                        "status": row.status, "source": str(source)})
    manifest = {"format": "rsi-eval-selected-trials-v1", "trials": entries}
    manifest_path = view / "selection.json"
    _scoped(manifest_path, output)
    from .relocation import read_json
    if manifest_path.exists() and read_json(manifest_path) != manifest:
        raise ValueError("Selected trial view metadata changed")
    unexpected = ({p.name for p in view.iterdir()} if view.exists() else set()) - {
        "selection.json", *(row["view"] for row in entries)}
    if unexpected:
        raise ValueError("Selected trial view contains unexpected entries")
    if write:
        _json_write(manifest_path, manifest)
        result.artifact_dir = str(view)


def _read_run(directory: Path) -> tuple[dict, EvaluationResult]:
    from .evaluation import (collect_result, _read_native_json, _native_task_index,
                             _native_task, _validate_native_agent)
    _scoped(directory / "resolved.json", directory)
    resolved = _read_native_json(directory / "resolved.json")
    _scoped(directory / "candidate", directory)
    if resolved["output_dir"] != str(directory):
        raise ValueError("Recorded run directory changed; resume in its original location")
    if artifact_hash(directory / "candidate") != resolved["target"]["sha256"]:
        raise ValueError("Candidate snapshot changed; refusing to resume")
    for task in resolved.get("execution_tasks", []):
        if artifact_hash(Path(task["path"])) != task["sha256"]:
            raise ValueError(f"Prepared task changed: {task['id']}")
    _scoped(directory / "native/eval", directory)
    paths = list((directory / "native/eval").glob("*/result.json"))
    index = _native_task_index(resolved) if paths else {}
    counts = {}
    for path in paths:
        _scoped(path, directory)
        data = _read_native_json(path)
        task = _native_task(data, resolved, index)
        _validate_native_agent(data, resolved)
        counts[task] = counts.get(task, 0) + 1
        if counts[task] > resolved["options"]["repetitions"]:
            raise ValueError(f"Unexpected duplicate native trial for {task}")
    previous = _read_native_json(directory / "result.json") if (directory / "result.json").is_file() else {}
    if "candidate snapshot was modified" in (previous.get("error") or ""):
        raise ValueError("The prior run recorded candidate mutation; start a new experiment")
    elapsed = previous.get("usage", {}).get("elapsed_seconds")
    return resolved, collect_result(resolved, returncode=None, elapsed=elapsed, write_report=False)


def _same_request(saved: dict, current: dict) -> None:
    if saved.get("runtime_snapshot_sha256") != current.get("runtime_snapshot_sha256"):
        raise ValueError("Cannot resume with changed runtime snapshot")
    for key in ("benchmark", "options", "backend", "runtime"):
        if saved[key] != current[key]:
            raise ValueError(f"Cannot resume with changed {key}")
    for key in ("entrypoint", "model", "config", "sha256"):
        if saved["target"][key] != current["target"][key]:
            raise ValueError(f"Cannot resume with changed target.{key}")


def _stored_request(saved: dict) -> tuple[TargetSpec, BenchmarkSelection, RunOptions]:
    target = TargetSpec(Path(saved["output_dir"]) / "candidate", saved["target"]["entrypoint"],
                        saved["target"]["config"], saved["target"]["model"])
    spec = saved["benchmark"]
    # Preserve whether the user selected the full configuration, not just its IDs.
    ids = None if spec["selection"] in {"full", "full_supported_subset"} else tuple(spec["task_ids"])
    return target, BenchmarkSelection(spec["name"], spec["version"], ids), RunOptions(**saved["options"])


def _merge(saved: dict, original: EvaluationResult, attempts: list[tuple[dict, EvaluationResult]]) -> EvaluationResult:
    from .metrics import benchmark_metrics
    slots = {(row.task_id, row.repetition): deepcopy(row) for row in original.trials}
    all_results = [original]
    for attempt, result in attempts:
        all_results.append(result)
        for row in result.trials:
            mapping = attempt["slots"][row.task_id]
            if row.repetition >= len(mapping):
                raise ValueError("Resume attempt contains an unexpected repetition")
            key = (row.task_id, mapping[row.repetition])
            if slots[key].status in MEASURED:
                raise ValueError("A resume attempt overlaps a completed measurement")
            slots[key] = replace(row, repetition=key[1])
    trials = [slots[(task, repetition)] for task in saved["benchmark"]["task_ids"]
              for repetition in range(saved["options"]["repetitions"])]
    complete = sum(row.status in MEASURED for row in trials)
    status = "completed" if complete == saved["expected_trials"] else ("partial" if complete else "failed")
    metrics: dict[str, Any] = {
        "resumed": True,
        "native_reports": [str(Path(r.resolved["output_dir"]) / "native/eval/result.json") for r in all_results],
        "completed_trials": complete, "expected_trials": saved["expected_trials"],
        "source_runs": [{"directory": r.resolved["output_dir"], "status": r.status, "error": r.error} for r in all_results],
        "aggregation_note": "Native reports remain separate. Selected measurements are combined by original task and repetition.",
    }
    if saved["benchmark"].get("aggregation"):
        metrics["benchmark_metrics"] = benchmark_metrics(trials, saved["benchmark"], saved["options"]["repetitions"],
                                                         valid_run=status == "completed")
    usage: dict[str, Any] = {"cost_usd": None, "cost_basis": original.usage["cost_basis"],
                             "accounting_scope": "All observed initial and retry attempts, including failed calls"}
    for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
        # A missing native report may hide an already-started model call. Keep
        # total usage unknown in that case, while retaining every observed token.
        rows = [t for r in all_results for t in r.trials if t.artifact_dir]
        values = [getattr(t, key) for t in rows]
        known = all(r.usage.get(key) is not None for r in all_results)
        usage[key] = sum(values) if values and known and all(v is not None for v in values) else None
        usage[f"observed_{key}"] = sum(v for v in values if v is not None)
    elapsed = [r.usage.get("elapsed_seconds") for r in all_results]
    usage["elapsed_seconds"] = sum(elapsed) if all(isinstance(t, (int, float)) for t in elapsed) else None
    usage["observed_trial_attempts"] = sum(bool(t.artifact_dir) for r in all_results for t in r.trials)
    error = None if status == "completed" else f"{saved['expected_trials'] - complete} trial(s) still lack completed measurements"
    return EvaluationResult(status, trials, metrics, usage, original.artifact_dir, saved, error,
                            str(Path(saved["output_dir"]) / "result.json"))


def _resume_inputs(output: Path, *, target: TargetSpec | None = None,
                   benchmark: BenchmarkSelection | None = None,
                   options: RunOptions | None = None) -> tuple:
    """Load and validate all continuation evidence while the caller holds its lock."""
    from .evaluation import _read_native_json, preview
    saved, original = _read_run(output)
    if saved.get("execution_identity") != implementation_identity():
        raise ValueError("Evaluator implementation changed or this run predates resumable evaluations")
    frozen = _stored_request(saved)
    current = preview(target=target or frozen[0], benchmark=benchmark or frozen[1],
                      options=options or frozen[2], output_dir=output)
    _same_request(saved, current)
    state_path = output / "resume.json"
    state = None
    if state_path.exists():
        _scoped(state_path, output)
        state = _read_native_json(state_path)
        if state.get("schema_version") != 1 or not isinstance(state.get("attempts"), list):
            raise ValueError("Invalid resume state")
        _verify_receipts(output, state["initial_receipts"])
        history_file = output / "resume-history/initial-result.json"
        _scoped(history_file, output)
        if _sha(history_file) != state.get("initial_result_sha256"):
            raise ValueError("Archived initial result changed")
        history = _read_native_json(history_file)
        # Native files, rather than a cached score, remain authoritative.
        original.usage["elapsed_seconds"] = history.get("usage", {}).get("elapsed_seconds")
    attempts = []
    for number, attempt in enumerate(state["attempts"] if state else [], 1):
        directory = _validate_attempt(saved, attempt, number)
        if "receipts" in attempt:
            _verify_receipts(directory, attempt["receipts"])
        if not (directory / "resolved.json").is_file():
            if directory.exists():
                if any(path.name not in {"candidate", ".evaluation.lock"} for path in directory.iterdir()):
                    raise ValueError(f"Unrecognized incomplete resume attempt: {directory}")
                # Only the in-memory state changes during inspection.
                attempt["status"] = "interrupted_during_snapshot"
            continue
        child_saved, result = _read_run(directory)
        _validate_attempt(saved, attempt, number, child_saved)
        attempt.setdefault("receipts", _receipts(directory))
        attempts.append((attempt, result))
    combined = _merge(saved, original, attempts)
    _selected_view(combined, len(state["attempts"]) if state else 0, write=False)
    return saved, original, frozen, state, attempts, combined


def check_resume(output_dir: Path | str, *, target: TargetSpec | None = None,
                 benchmark: BenchmarkSelection | None = None,
                 options: RunOptions | None = None) -> dict:
    """Check the complete recorded continuation without new trials or evidence writes.

    The writer lock prevents inspecting a run while another process changes it.
    As with execution, callers must still revalidate when they actually resume.
    """
    output = Path(output_dir).expanduser().resolve()
    if not (output / "resolved.json").is_file():
        raise ValueError("No recorded evaluation to resume")
    with evaluation_lock(output):
        saved, _, _, state, _, combined = _resume_inputs(
            output, target=target, benchmark=benchmark, options=options)
        completed = sum(row.status in MEASURED for row in combined.trials)
        return {"ok": True, "output_dir": str(output), "status": combined.status,
                "expected_trials": saved["expected_trials"], "completed_trials": completed,
                "pending_trials": saved["expected_trials"] - completed,
                "attempts": len(state["attempts"]) if state else 0, "model_calls": 0}


def resume_evaluation(output_dir: Path | str, *, target: TargetSpec | None = None,
                      benchmark: BenchmarkSelection | None = None,
                      options: RunOptions | None = None) -> EvaluationResult:
    """Resume only unfinished slots using the frozen original candidate.

    Each explicit invocation makes at most one new attempt for each unfinished
    slot. Previous trials, reports and logs remain in place. No API fallback or
    automatic retry is performed when an attempt fails.
    """
    from .evaluation import _json_write, _read_native_json, evaluate
    output = Path(output_dir).expanduser().resolve()
    if not (output / "resolved.json").is_file():
        raise ValueError("No recorded evaluation to resume")
    with evaluation_lock(output):
        saved, original, frozen, state, attempts, combined = _resume_inputs(
            output, target=target, benchmark=benchmark, options=options)
        frozen_target, frozen_benchmark, frozen_options = frozen
        state_path = output / "resume.json"
        if state is None:
            archive = output / "resume-history"
            _scoped(archive, output)
            archive.mkdir(exist_ok=True)
            prior = _read_native_json(output / "result.json") if (output / "result.json").is_file() else original.to_dict()
            _json_write(archive / "initial-result.json", prior)
            state = {"schema_version": 1, "initial_receipts": _receipts(output), "attempts": [],
                     "initial_result_sha256": _sha(archive / "initial-result.json")}
            _json_write(state_path, state)
        pending: dict[str, list[int]] = {}
        for row in combined.trials:
            if row.status not in MEASURED:
                pending.setdefault(row.task_id, []).append(row.repetition)
        # Group tasks with the same missing count so Harbor retains independent
        # repetitions without re-evaluating a settled task or introducing duplicates.
        for count in sorted({len(slots) for slots in pending.values()}):
            ids = [task for task, slots in pending.items() if len(slots) == count]
            attempt = {"directory": f"resume-attempts/{len(state['attempts']) + 1:04d}",
                       "slots": {task: pending[task] for task in ids}}
            state["attempts"].append(attempt)
            _json_write(state_path, state)
            directory = output / attempt["directory"]
            child = evaluate(target=frozen_target,
                             benchmark=BenchmarkSelection(frozen_benchmark.name, frozen_benchmark.version, tuple(ids)),
                             options=replace(frozen_options, repetitions=count), output_dir=directory)
            _validate_attempt(saved, attempt, len(state["attempts"]), child.resolved)
            attempt["receipts"] = _receipts(directory)
            attempt["status"] = child.status
            attempts.append((attempt, child))
            combined = _merge(saved, original, attempts)
            _json_write(state_path, state)
            _selected_view(combined, len(state["attempts"]))
            _json_write(output / "result.json", combined.to_dict())
            # An explicit resume never turns into an unbounded retry loop.
            if child.status != "completed":
                break
        _json_write(state_path, state)
        _selected_view(combined, len(state["attempts"]))
        _json_write(output / "result.json", combined.to_dict())
        return combined
