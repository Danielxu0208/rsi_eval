"""A later invalid test selection must never spend an earlier selection's budget."""
from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

from rsi_eval import EvaluationResult, TargetSpec, TrialResult, method_test


def selection(version="2.0", **changes):
    return {"name": "terminal-bench", "version": version,
            "task_ids": ["extract-elf"], **changes}


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    root = tmp_path / "experiment"
    candidate = root / "run/candidate"
    candidate.mkdir(parents=True)
    (candidate / "prompt.md").write_text("measured selected candidate")
    config = {"rrsi": {"k": 2}, "test": [selection(), selection("2.1")],
              "run_options": {"concurrency": 1, "timeout": 123}}
    record = {"method": "rrsi", "experiment": str(root), "configuration": config}
    target = TargetSpec(candidate, "fixture:Agent", {"setting": "preserved"}, "fixture-model")
    monkeypatch.setattr(method_test, "selected_target", lambda record: (
        target, config, {"commit": "measured"}))
    return record, config, tmp_path / "independent-tests"


def checked(**kwargs):
    return {"ok": True, "checks": {"fixture": {"ok": True}}}


def completed(kwargs, *, reward=0.375):
    output = kwargs["output_dir"]
    output.mkdir(parents=True, exist_ok=True)
    result = EvaluationResult(
        "completed", [TrialResult(kwargs["benchmark"].task_ids[0], 0, "completed", reward)],
        {"native_fractional_score": reward}, {}, str(output), {},
        report_path=str(output / "result.json"))
    (output / "result.json").write_text(json.dumps(result.to_dict()))
    return result


def no_evaluation(**kwargs):
    pytest.fail("A model-consuming evaluator ran before the entire batch passed preflight")


def preflight(output):
    return json.loads((output / "preflight.json").read_text())


@pytest.mark.parametrize("bad", [
    selection(name="unknown-benchmark"),
    selection(version="not-a-version"),
    selection(task_ids=["not-an-official-task"]),
])
def test_later_unknown_benchmark_version_or_task_stops_every_evaluation(experiment, bad):
    record, config, output = experiment
    config["test"][1] = bad
    result = method_test.test_selected(record, output, checker=checked, evaluator=no_evaluation)
    assert result["status"] == "preflight_failed"
    assert result["evaluations"] == []
    report = preflight(output)
    assert report["ok"] is False and report["model_calls"] == 0
    assert [row["ok"] for row in report["checks"]] == [True, False]
    assert report["checks"][1]["index"] == 1
    assert report["checks"][1]["error_type"]
    assert (output / "request.json").is_file()
    assert (output / "candidate/prompt.md").read_text() == "measured selected candidate"
    assert json.loads((output / "method-test.json").read_text()) == result
    assert not list(output.glob("benchmark-*"))


def test_collects_multiple_selection_and_option_errors_in_one_report(experiment):
    record, config, output = experiment
    config["test"] = [selection(), selection(task_ids=["missing-task"]),
                      selection("2.1", benchmark_runtime=[]),
                      {"version": "2.0", "task_ids": ["extract-elf"]}, "not-an-object"]
    checks = []

    def checker(**kwargs):
        checks.append(kwargs["benchmark"])
        return checked(**kwargs)

    result = method_test.test_selected(record, output, checker=checker, evaluator=no_evaluation)
    assert result["status"] == "preflight_failed"
    report = preflight(output)
    assert [row["index"] for row in report["checks"]] == list(range(5))
    assert [row["ok"] for row in report["checks"]] == [True, False, False, False, False]
    assert len(checks) == 1
    assert all(row.get("error_type") and row.get("detail") for row in report["checks"][1:])


@pytest.mark.parametrize("raises", [False, True])
def test_later_environment_failure_or_checker_exception_stops_every_evaluation(experiment, raises):
    record, config, output = experiment
    config["test"].append(selection())
    visited = []

    def checker(**kwargs):
        version = kwargs["benchmark"].version
        visited.append(version)
        if version == "2.1":
            if raises:
                raise OSError("fixture prerequisite unavailable")
            return {"ok": False, "checks": {"docker": {
                "ok": False, "detail": "fixture prerequisite unavailable"}}}
        return checked(**kwargs)

    result = method_test.test_selected(record, output, checker=checker, evaluator=no_evaluation)
    assert result["status"] == "preflight_failed"
    assert visited == ["2.0", "2.1", "2.0"]
    assert [row["ok"] for row in preflight(output)["checks"]] == [True, False, True]
    assert "fixture prerequisite unavailable" in json.dumps(preflight(output))


def test_all_checks_precede_execution_and_share_prepared_options_and_snapshot(experiment):
    record, config, output = experiment
    config["codex"] = {"executable": "/fixture/custom-codex"}
    events, checked_arguments, evaluated_arguments = [], [], []

    def checker(**kwargs):
        events.append(("check", kwargs["benchmark"].version))
        checked_arguments.append(kwargs)
        return checked(**kwargs)

    def evaluator(**kwargs):
        events.append(("evaluate", kwargs["benchmark"].version))
        evaluated_arguments.append(kwargs)
        return completed(kwargs)

    result = method_test.test_selected(record, output, checker=checker, evaluator=evaluator)
    assert result["status"] == "completed"
    assert events == [("check", "2.0"), ("check", "2.1"),
                      ("evaluate", "2.0"), ("evaluate", "2.1")]
    for prior, actual in zip(checked_arguments, evaluated_arguments):
        assert prior["target"] is actual["target"]
        assert prior["target"].artifact_dir == output / "candidate"
        assert prior["options"] is actual["options"]
        assert actual["options"].repetitions == 2
        assert actual["options"].timeout == 123
        assert prior["benchmark"] is actual["benchmark"]
        assert prior["codex_executable"] == "/fixture/custom-codex"
    assert preflight(output)["ok"] is True
    assert preflight(output)["model_calls"] == 0
    assert all(row["trials"][0]["reward"] == 0.375 for row in result["evaluations"])
    assert result["evaluations"][0]["metrics"] == {"native_fractional_score": 0.375}


def test_source_runtime_override_is_checked_and_reused_without_score_mapping(experiment):
    record, config, output = experiment
    config["run_options"]["benchmark_runtime"] = {"unrelated_backend_setting": "old"}
    config["test"] = [{"name": "drop", "version": "1.0-agent",
                       "task_ids": ["train-f16c0ee7-f131-4a8b-a6ac-4d275ea68066"],
                       "benchmark_runtime": {"workers": 2}}]
    prepared = []

    def checker(**kwargs):
        assert kwargs["options"].benchmark_runtime == {"workers": 2}
        prepared.append(kwargs["options"])
        return checked(**kwargs)

    def evaluator(**kwargs):
        assert kwargs["options"] is prepared[0]
        return completed(kwargs, reward=0.42)

    result = method_test.test_selected(record, output, checker=checker, evaluator=evaluator)
    assert result["status"] == "completed"
    assert result["evaluations"][0]["trials"][0]["reward"] == 0.42
    assert "score_mapping" not in config


def test_failed_initial_preflight_can_resume_after_external_prerequisite_repair(experiment):
    record, config, output = experiment
    failed = method_test.test_selected(record, output, evaluator=no_evaluation,
        checker=lambda **kwargs: {"ok": False, "checks": {"fixture": {"ok": False}}})
    assert failed["status"] == "preflight_failed"
    original_request = (output / "request.json").read_bytes()
    calls = []

    def evaluator(**kwargs):
        calls.append(kwargs)
        return completed(kwargs)

    result = method_test.test_selected(record, output, resume=True, checker=checked, evaluator=evaluator)
    assert result["status"] == "completed" and len(calls) == 2
    assert not any(call.get("resume") for call in calls)
    assert (output / "request.json").read_bytes() == original_request
    assert preflight(output)["ok"] is True


def test_failed_resume_preserves_previous_summary_and_checks_all_children(experiment, monkeypatch):
    record, config, output = experiment
    first = method_test.test_selected(record, output, checker=checked,
                                      evaluator=lambda **kwargs: completed(kwargs))
    previous = (output / "method-test.json").read_bytes()
    visited = []

    def check_resume(output_dir, **kwargs):
        visited.append(Path(output_dir).name)
        if Path(output_dir).name == "benchmark-001":
            raise ValueError("fixture saved receipt mismatch")
        return {"ok": True}

    resume_module = importlib.import_module("rsi_eval.resume")
    monkeypatch.setattr(resume_module, "check_resume", check_resume)
    result = method_test.test_selected(record, output, resume=True,
                                      checker=checked, evaluator=no_evaluation)
    assert result["status"] == "preflight_failed"
    assert result["evaluations"] == first["evaluations"]
    assert visited == ["benchmark-000", "benchmark-001"]
    assert (output / "method-test.json").read_bytes() == previous
    assert preflight(output)["ok"] is False
    assert "fixture saved receipt mismatch" in json.dumps(preflight(output))


def test_changed_request_after_failed_preflight_requires_a_new_output(experiment):
    record, config, output = experiment
    method_test.test_selected(record, output, evaluator=no_evaluation,
        checker=lambda **kwargs: {"ok": False, "checks": {"fixture": {"ok": False}}})
    previous = (output / "request.json").read_bytes()
    changed = deepcopy(config["test"])
    changed.reverse()
    with pytest.raises(ValueError, match="changed"):
        method_test.test_selected(record, output, resume=True, selections=changed,
                                  evaluator=no_evaluation, checker=checked)
    assert (output / "request.json").read_bytes() == previous


def test_default_checker_checks_host_prerequisites_without_launching_evaluator(experiment, monkeypatch):
    record, config, output = experiment
    import rsi_eval.evaluation as evaluation
    probes = []

    def probe(command, **kwargs):
        probes.append(command)
        if command[0] == "docker":
            return {"ok": False, "detail": "fixture Docker daemon unavailable"}
        return {"ok": True, "detail": "harbor 0.18.0"}

    monkeypatch.setattr(evaluation, "_probe", probe)
    result = method_test.test_selected(record, output, evaluator=no_evaluation)
    assert result["status"] == "preflight_failed"
    assert len([command for command in probes if command[0] == "harbor"]) == 2
    assert all(row["checks"]["docker"]["ok"] is False for row in preflight(output)["checks"])


def test_harnessx_auth_failure_is_checked_even_for_non_codex_harbor_entrypoint(experiment, monkeypatch):
    record, config, output = experiment
    record["method"] = "harnessx"
    config["codex"] = {"model": "fixture-model", "expected_version": "fixture-version",
                       "auth_dir": str(output.parent / "fixture-auth"),
                       "executable": "/fixture/codex"}
    import rsi_eval.codex as codex
    calls = []

    def validate_auth(auth_dir):
        calls.append(Path(auth_dir))
        raise codex.CodexAuthError("fixture expired login")

    monkeypatch.setattr(codex, "validate_auth", validate_auth)
    result = method_test.test_selected(record, output, evaluator=no_evaluation, checker=checked)
    assert result["status"] == "preflight_failed"
    assert calls == [output.parent / "fixture-auth"]
    assert "fixture expired login" in json.dumps(preflight(output)["target_checks"])
    assert all(row["ok"] for row in preflight(output)["checks"])


def test_invalid_shared_runtime_options_are_reported_for_every_selection(experiment):
    record, config, output = experiment
    config["run_options"]["concurrency"] = 0
    result = method_test.test_selected(record, output, evaluator=no_evaluation, checker=checked)
    assert result["status"] == "preflight_failed"
    assert len(preflight(output)["checks"]) == 2
    assert all(not row["ok"] and "positive integers" in row["detail"]
               for row in preflight(output)["checks"])


def test_cli_returns_nonzero_and_prints_entire_failed_batch(experiment, monkeypatch, capsys):
    record, config, output = experiment
    config["test"] = [selection(version="missing-version"), selection(task_ids=["missing-task"])]
    root = Path(record["experiment"])
    (root / "method.json").write_text(json.dumps(record))
    import rsi_eval
    monkeypatch.setattr(rsi_eval, "evaluate", no_evaluation)
    monkeypatch.setattr(rsi_eval, "check", checked)
    code = method_test.main(["--experiment", str(root), "--output", str(output)])
    assert code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "preflight_failed"
    assert len(printed["preflight"]["checks"]) == 2
    assert not any(row["ok"] for row in printed["preflight"]["checks"])
