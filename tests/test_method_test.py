"""Independent tests use native selected evidence and keep search state untouched."""
from pathlib import Path
import importlib
import json
from types import SimpleNamespace

import pytest

from rsi_eval import BenchmarkSelection, EvaluationResult, TrialResult, TargetSpec
from rsi_eval.artifacts import artifact_hash
from rsi_eval import method_test, methods
from rsi_eval.cli import main


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def measurement(candidate):
    return {"status": "completed", "resolved": {"target": {"sha256": artifact_hash(candidate)}},
            "trials": [{"status": "completed", "reward": 0.0}]}


@pytest.mark.parametrize("method", ["ahe", "harnessx"])
def test_selected_exports_use_measured_incumbent_not_last_candidate(tmp_path, monkeypatch, method):
    root = tmp_path / "experiment"
    candidate = root / "run/candidates/baseline"
    candidate.mkdir(parents=True)
    (candidate / "prompt.md").write_text("measured incumbent")
    other = root / "run/workspace"
    other.mkdir()
    (other / "prompt.md").write_text("unselected later edit")
    digest = artifact_hash(candidate)
    report = root / "run/evaluations/baseline/integration_result.json"
    write(report, measurement(candidate))
    config = {"codex": {"model": "test-model", "auth_dir": str(tmp_path / "auth"), "expected_version": "fixture"},
              "checkout": str(root / "checkout")}
    if method == "ahe":
        write(root / "run/summary.json", {"selected_candidate": str(candidate), "selected_sha256": digest,
              "best": {"iteration": 1}, "evaluations": [{"candidate_sha256": digest, "output_dir": str(report.parent)}]})
    else:
        from integrations.harnessx import launcher
        monkeypatch.setattr(launcher, "runtime_environment", lambda config: None)
        write(root / "run/result.json", {"current_candidate": str(candidate),
              "baseline": {"candidate": str(candidate), "candidate_hash": digest, "report_path": str(report)},
              "rounds": [{"candidate": str(other), "candidate_hash": artifact_hash(other), "report_path": "not-selected"}]})
    record = {"method": method, "experiment": str(root), "configuration": config}
    target, _, evidence = method_test.selected_target(record)
    assert target.artifact_dir == candidate and evidence["method"] == method
    (candidate / "prompt.md").write_text("tampered")
    with pytest.raises(ValueError, match="changed|differs"):
        method_test.selected_target(record)


def test_independent_tests_share_options_preserve_raw_scores_and_resume(tmp_path, monkeypatch):
    root = tmp_path / "experiment"
    candidate = root / "run/candidate"
    candidate.mkdir(parents=True)
    (candidate / "prompt.md").write_text("measured")
    selection = {"name": "drop", "version": "1.0-agent",
                 "task_ids": ["dev-ac6ba235-3024-4f63-a6ab-730a14def4cb"], "benchmark_runtime": {"workers": 2}}
    cfg = {"rrsi": {"k": 3}, "test": [selection, selection], "run_options": {"concurrency": 1}}
    record = {"method": "rrsi", "experiment": str(root), "configuration": cfg}
    monkeypatch.setattr(method_test, "selected_target", lambda record: (TargetSpec(candidate, "fixture:Agent"), cfg, {"commit": "measured"}))
    monkeypatch.setattr("rsi_eval.check", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(importlib.import_module("rsi_eval.resume"), "check_resume", lambda *args, **kwargs: {"ok": True})
    calls = []
    def evaluate(**kwargs):
        calls.append(kwargs)
        out = kwargs["output_dir"]
        out.mkdir(exist_ok=True)
        (out / "result.json").write_text("{}")
        return EvaluationResult("completed", [TrialResult("task", 0, "completed", 0.4)],
                                {"f1": 0.4}, {}, str(out), {}, report_path=str(out / "result.json"))
    output = tmp_path / "tests"
    result = method_test.test_selected(record, output, evaluator=evaluate)
    assert result["status"] == "completed"
    assert len(calls) == 2
    assert all(call["options"].repetitions == 3 for call in calls)
    assert calls[0]["options"].benchmark_runtime == {"workers": 2}
    assert calls[0]["target"].artifact_dir == output / "candidate"
    assert result["evaluations"][0]["trials"][0]["reward"] == 0.4
    method_test.test_selected(record, output, evaluator=evaluate, resume=True)
    assert all(call.get("resume") for call in calls[2:])
    with pytest.raises(ValueError, match="changed"):
        method_test.test_selected(record, output, selections=[selection], evaluator=evaluate, resume=True)
    with pytest.raises(ValueError, match="overlap"):
        method_test.test_selected(record, root / "run/new-test", evaluator=evaluate)


def test_cli_routes_all_methods_test_into_the_frozen_runtime(tmp_path, monkeypatch):
    record = {"experiment": str(tmp_path / "experiment"), "environment": str(tmp_path / "env")}
    for method in methods.METHODS:
        command = methods.method_command({**record, "method": method}, "test", output=tmp_path / "test", selection_file=tmp_path / "selection.json", test_resume=True)
        assert command[:3] == [str(tmp_path / "env/bin/python"), "-m", "rsi_eval.method_test"]
        assert "--selection" in command and "--resume" in command
    calls = []
    monkeypatch.setattr(methods, "launch_method", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    assert main(["method", "test", "--experiment", str(tmp_path), "--output", str(tmp_path / "test")]) == 0
    assert calls[0][0] == (tmp_path, "test")


def test_override_selections_resolve_relative_runtime_inputs(tmp_path):
    path = tmp_path / "selection.json"
    write(path, [{"name": "terminal-bench", "version": "2.1", "manifest": "tasks.json"}])
    assert method_test.load_selections(path)[0]["manifest"] == str(tmp_path / "tasks.json")
