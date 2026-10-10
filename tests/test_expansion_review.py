"""Regression cases found while reviewing benchmark expansion results."""
import http.client
import json
from pathlib import Path

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate, preview
from rsi_eval.benchmarks import resolve
from rsi_eval.evaluation import collect_result
from rsi_eval import evaluation, materializers


@pytest.fixture
def target(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "agent.py").write_text("VALUE = 1\n")
    return TargetSpec(candidate, "fixture:Agent")


def native_mgsm_result(target, tmp_path, *, reward=1, summary=None):
    resolved = preview(target=target, benchmark=BenchmarkSelection(
        "mgsm", "1.0-agent", task_ids=("en-001",)), output_dir=tmp_path / "out")
    task_path = tmp_path / "out/tasks/en-001"
    resolved["execution_tasks"] = [{"id": "en-001", "path": str(task_path)}]
    native = tmp_path / "out/native/eval"
    trial = native / "en-001__fixture"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "task_name": task_path.name,
        "task_id": {"path": str(task_path)},
        "config": {"agent": {"import_path": target.entrypoint, "model_name": target.model}},
        "finished_at": "2026-10-10T00:00:00Z",
        "verifier": {"finished_at": "2026-10-10T00:00:00Z"},
        "verifier_result": {"rewards": {"reward": reward}},
    }))
    if summary is None:
        summary = {"finished_at": "2026-10-10T00:00:00Z", "n_total_trials": 1}
    (native / "result.json").write_text(json.dumps(summary))
    return resolved


@pytest.mark.parametrize("reward", [-1, 0.5, 2])
def test_nonbinary_native_reward_cannot_become_a_measured_binary_score(target, tmp_path, reward):
    resolved = native_mgsm_result(target, tmp_path, reward=reward)
    result = collect_result(resolved, returncode=0)
    assert result.status == "failed"
    assert result.trials[0].status == "incomplete"
    assert result.trials[0].reward is None
    assert result.trials[0].metrics == {"reward": reward}
    assert "nonbinary reward" in result.error
    assert result.metrics["benchmark_metrics"]["accuracy"] is None
    saved = json.loads(Path(result.report_path).read_text())
    assert saved["trials"][0]["reward"] is None
    assert saved["trials"][0]["metrics"]["reward"] == reward


@pytest.mark.parametrize("summary", [[], "malformed summary"])
def test_nonobject_native_summary_preserves_trial_evidence_without_aggregation(target, tmp_path, summary):
    resolved = native_mgsm_result(target, tmp_path, summary=summary)
    result = collect_result(resolved, returncode=0)
    assert result.status == "partial"
    assert result.trials[0].status == "completed"
    assert result.trials[0].reward == 1
    assert "native job result must be an object" in result.error
    assert result.metrics["benchmark_metrics"]["complete"] is False
    assert result.metrics["benchmark_metrics"]["accuracy"] is None
    assert json.loads(Path(result.report_path).read_text())["status"] == "partial"


def test_truncated_source_download_saves_infrastructure_failure(target, tmp_path, monkeypatch):
    def fail_prepare(*args):
        raise http.client.IncompleteRead(b"truncated public dataset", 200)

    monkeypatch.setattr(materializers, "prepare", fail_prepare)
    monkeypatch.setattr(evaluation, "_probe", lambda *a, **k: {"ok": True, "detail": "harbor 0.18.0"})
    monkeypatch.setattr(evaluation.subprocess, "Popen", lambda *a, **k: pytest.fail("Harbor job must not run"))
    result = evaluate(target=target, benchmark=BenchmarkSelection(
        "mgsm", "1.0-agent", task_ids=("en-001",)), output_dir=tmp_path / "out")
    assert result.status == "failed"
    assert result.trials[0].status == "infra_error"
    assert result.trials[0].reward is None
    assert "IncompleteRead" in result.error
    assert result.metrics["benchmark_metrics"]["accuracy"] is None
    saved = json.loads(Path(result.report_path).read_text())
    assert saved["trials"][0]["status"] == "infra_error"
    assert saved["trials"][0]["reward"] is None
    assert not (tmp_path / "out/command.json").exists()


@pytest.mark.parametrize("field", ["repetitions", "concurrency"])
@pytest.mark.parametrize("value", [True, 2.0, 0, -1, "2"])
def test_trial_counts_reject_nonpositive_or_noninteger_values_before_preparation(target, tmp_path, field, value):
    with pytest.raises(ValueError, match="positive integers"):
        evaluate(target=target, benchmark=BenchmarkSelection(
            "mgsm", "1.0-agent", task_ids=("en-001",)),
            options=RunOptions(**{field: value}), output_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_all_supported_engdesign_tasks_do_not_claim_full_upstream_coverage():
    version = "open-subset-c8ec338-v1"
    supported = resolve(BenchmarkSelection("engdesign-open", version))
    assert supported["selection"] == "full_supported_subset"
    assert supported["coverage"]["supported_task_count"] == len(supported["task_ids"]) == 2
    assert supported["coverage"]["upstream_open_task_count"] == 67
    explicit = resolve(BenchmarkSelection("engdesign-open", version,
                                          task_ids=tuple(supported["task_ids"])))
    assert explicit["selection"] == "subset"
    assert explicit["coverage"] == supported["coverage"]
