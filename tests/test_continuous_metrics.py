"""Native score and failure contracts for DROP and Frontier-Eng."""
import json
from pathlib import Path

import pytest

from rsi_eval.evaluation import collect_result
from rsi_eval.metrics import benchmark_metrics, metric_error
from rsi_eval.types import TrialResult


def drop_spec():
    return {"name": "drop", "version": "fixture", "selection": "subset",
            "aggregation": {"type": "drop_em_f1"},
            "tasks": [{"id": "train-one", "split": "train"},
                      {"id": "dev-one", "split": "dev"},
                      {"id": "dev-two", "split": "dev"}]}


def frontier_spec():
    return {"name": "frontier-eng", "version": "fixture", "selection": "subset",
            "aggregation": {"type": "frontier_native"},
            "tasks": [
                {"id": "inventory", "objective": {"name": "combined_score", "range": [0, 1],
                                                      "direction": "higher_is_better"}},
                {"id": "pid", "objective": {"name": "inverse_itae", "range": "unbounded",
                                                "direction": "higher_is_better"}}]}


def native_run(tmp_path, spec, rows, *, repetitions=1, summary=None):
    """Write native evidence; each row supplies task ID, rewards and exception."""
    spec = {**spec, "task_ids": [task["id"] for task in spec["tasks"]]}
    tasks = [{"id": task["id"], "path": str(tmp_path / "tasks" / task["id"])}
             for task in spec["tasks"]]
    by_id = {task["id"]: task for task in tasks}
    resolved = {"output_dir": str(tmp_path), "benchmark": spec, "execution_tasks": tasks,
                "options": {"repetitions": repetitions},
                "target": {"entrypoint": "fixture:Agent", "model": "fixture-model"},
                "expected_trials": len(tasks) * repetitions}
    native = tmp_path / "native/eval"
    native.mkdir(parents=True)
    for index, (task_id, rewards, exception) in enumerate(rows):
        task = by_id[task_id]
        trial = native / f"{task_id}__{index}"
        trial.mkdir()
        (trial / "result.json").write_text(json.dumps({
            "task_name": task_id, "task_id": {"path": task["path"]},
            "config": {"agent": {"import_path": "fixture:Agent", "model_name": "fixture-model"}},
            "finished_at": "2026-10-10T00:00:00Z",
            "verifier": {"finished_at": "2026-10-10T00:00:00Z"},
            "verifier_result": {"rewards": rewards}, "exception_info": exception,
        }))
    if summary is None:
        summary = {"finished_at": "2026-10-10T00:00:00Z", "n_total_trials": resolved["expected_trials"]}
    (native / "result.json").write_text(json.dumps(summary))
    return resolved


def one_drop_spec():
    spec = drop_spec()
    spec["tasks"] = [spec["tasks"][1]]
    return spec


def test_fractional_drop_f1_survives_native_collection_and_json_report(tmp_path):
    rewards = {"reward": 0.625, "exact_match": 0.0, "f1": 0.625}
    resolved = native_run(tmp_path, one_drop_spec(), [("dev-one", rewards, None)])
    result = collect_result(resolved, returncode=0)
    assert result.status == "completed"
    assert result.trials[0].status == "completed"
    assert result.trials[0].reward == 0.625
    assert result.trials[0].metrics == rewards
    metrics = result.metrics["benchmark_metrics"]
    assert metrics["exact_match"] == 0
    assert metrics["f1"] == 0.625
    assert metrics["scale"] == "0_to_1"
    assert json.loads(Path(result.report_path).read_text())["trials"][0]["reward"] == 0.625


@pytest.mark.parametrize("rewards", [
    {"reward": 0.5, "f1": 0.5},
    {"reward": 0.5, "exact_match": 0},
    {"reward": 0.5, "exact_match": 0.5, "f1": 0.5},
    {"reward": 50, "exact_match": 0, "f1": 50},
    {"reward": -0.1, "exact_match": 0, "f1": -0.1},
    {"reward": 1, "exact_match": 0, "f1": 0.5},
    {"reward": "0.5", "exact_match": 0, "f1": "0.5"},
    {"reward": 0.5, "exact_match": True, "f1": 0.5},
])
def test_invalid_or_missing_drop_metrics_remain_unknown(tmp_path, rewards):
    resolved = native_run(tmp_path, one_drop_spec(), [("dev-one", rewards, None)])
    result = collect_result(resolved, returncode=0)
    assert result.status == "failed"
    assert result.trials[0].status == "incomplete"
    assert result.trials[0].reward is None
    assert result.trials[0].metrics == rewards
    assert result.metrics["benchmark_metrics"]["f1"] is None
    assert result.metrics["benchmark_metrics"]["exact_match"] is None


def test_drop_uses_question_means_and_preserves_split_metrics():
    rows = [TrialResult(task_id, 0, "completed", f1,
                        {"reward": f1, "exact_match": em, "f1": f1})
            for task_id, em, f1 in [("train-one", 1, 1), ("dev-one", 0, 0.25), ("dev-two", 0, 0.5)]]
    result = benchmark_metrics(rows, drop_spec(), 1)
    assert result["complete"] is True
    assert result["exact_match"] == pytest.approx(1 / 3)
    assert result["f1"] == pytest.approx(1.75 / 3)
    assert result["by_split"] == {"train": {"exact_match": 1, "f1": 1},
                                  "dev": {"exact_match": 0, "f1": 0.375}}
    assert "pass_at_k" not in result


def test_missing_drop_trial_does_not_become_zero_in_aggregate(tmp_path):
    rewards = {"reward": 0.75, "exact_match": 0, "f1": 0.75}
    resolved = native_run(tmp_path, drop_spec(), [("dev-one", rewards, None)])
    result = collect_result(resolved, returncode=0)
    assert result.status == "partial"
    assert result.metrics["benchmark_metrics"]["complete"] is False
    assert result.metrics["benchmark_metrics"]["f1"] is None
    assert [trial.reward for trial in result.trials] == [None, 0.75, None]


@pytest.mark.parametrize(("exception_type", "expected"), [
    ("CodexAuthError", "auth_error"), ("CodexQuotaError", "quota_exceeded"),
])
def test_missing_drop_metrics_do_not_mask_authentication_or_quota(tmp_path, exception_type, expected):
    error = {"exception_type": exception_type, "exception_message": "service stopped"}
    resolved = native_run(tmp_path, one_drop_spec(), [("dev-one", {}, error)])
    result = collect_result(resolved, returncode=0)
    assert result.status == "failed"
    assert result.trials[0].status == expected
    assert exception_type in result.trials[0].error
    assert result.trials[0].reward is None


def test_frontier_preserves_native_failure_sentinel_and_heterogeneous_objectives(tmp_path):
    spec = frontier_spec()
    rows = [("inventory", {"reward": 0.75, "native_score": 0.75, "valid": 1}, None),
            ("pid", {"reward": -1e18, "native_score": -1e18, "valid": 0, "feasible": 0}, None)]
    result = collect_result(native_run(tmp_path, spec, rows), returncode=0)
    assert result.status == "completed"
    assert result.trials[1].reward == -1e18
    assert result.trials[1].status == "completed"
    metrics = result.metrics["benchmark_metrics"]
    assert metrics["aggregate_score"] is None
    assert "mean" not in metrics and "accuracy" not in metrics and "pass_at_k" not in metrics
    assert metrics["per_task"]["inventory"]["valid_rate"] == 1
    assert metrics["per_task"]["pid"]["valid_rate"] == 0
    assert metrics["per_task"]["pid"]["objective"] == spec["tasks"][1]["objective"]
    assert metrics["per_task"]["pid"]["trials"][0]["metrics"] == rows[1][1]


@pytest.mark.parametrize("rewards", [
    {"reward": 0.5, "valid": 1},
    {"reward": 0.5, "native_score": 0.5},
    {"reward": 0.5, "native_score": 2.0, "valid": 1},
    {"reward": 0.5, "native_score": 0.5, "valid": True},
])
def test_frontier_requires_native_score_and_validity_contract(rewards):
    assert metric_error(rewards, frontier_spec(), "pid")


@pytest.mark.parametrize("kind", ["drop_em_f1", "frontier_native"])
def test_duplicate_repetition_cannot_claim_a_complete_run(kind):
    if kind == "drop_em_f1":
        spec = one_drop_spec()
        rewards = {"reward": 0.5, "exact_match": 0, "f1": 0.5}
    else:
        spec = frontier_spec()
        spec["tasks"] = [spec["tasks"][0]]
        rewards = {"reward": 0.5, "native_score": 0.5, "valid": 1}
    task_id = spec["tasks"][0]["id"]
    rows = [TrialResult(task_id, 0, "completed", 0.5, dict(rewards)) for _ in range(2)]
    result = benchmark_metrics(rows, spec, 2)
    assert result["complete"] is False
    if kind == "drop_em_f1":
        assert result["f1"] is None
    else:
        assert result["per_task"][task_id]["valid_rate"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_native_auxiliary_metric_produces_report_not_serialization_crash(tmp_path, value):
    rewards = {"reward": 0.5, "exact_match": 0, "f1": 0.5, "auxiliary": value}
    resolved = native_run(tmp_path, one_drop_spec(), [("dev-one", rewards, None)])
    result = collect_result(resolved, returncode=0)
    assert result.status == "failed"
    assert result.trials[0].status == "incomplete"
    assert result.trials[0].reward is None
    assert json.loads(Path(result.report_path).read_text())["status"] == "failed"


def test_nonfinite_native_summary_does_not_erase_measured_trial(tmp_path):
    rewards = {"reward": 0.5, "exact_match": 0, "f1": 0.5}
    summary = {"finished_at": "done", "n_total_trials": 1, "auxiliary": float("nan")}
    resolved = native_run(tmp_path, one_drop_spec(), [("dev-one", rewards, None)], summary=summary)
    result = collect_result(resolved, returncode=0)
    assert result.status == "partial"
    assert result.trials[0].reward == 0.5
    assert result.metrics["benchmark_metrics"]["f1"] is None
    assert Path(result.report_path).is_file()


def test_oversized_native_integer_becomes_invalid_score_without_overflow(tmp_path):
    rewards = {"reward": 10 ** 400, "exact_match": 0, "f1": 0.5}
    result = collect_result(native_run(tmp_path, one_drop_spec(), [("dev-one", rewards, None)]), returncode=0)
    assert result.status == "failed"
    assert result.trials[0].status == "incomplete"
    assert result.trials[0].reward is None
    assert Path(result.report_path).is_file()
