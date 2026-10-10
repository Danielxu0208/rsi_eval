"""Whole-collection matching stays strict and builds the task index once."""
import json

import pytest

from rsi_eval import evaluation


def test_full_result_collection_uses_one_index_and_checks_immutable_versions(tmp_path, monkeypatch):
    count = 200
    tasks = [{"id": f"task-{i}", "name": f"fixture/task-{i}", "ref": "sha256:" + f"{i:064x}"}
             for i in range(count)]
    resolved = {"benchmark": {"tasks": tasks, "task_ids": [task["id"] for task in tasks]},
                "target": {"entrypoint": "oracle", "model": None},
                "options": {"repetitions": 1}, "expected_trials": count,
                "output_dir": str(tmp_path)}
    native = tmp_path / "native/eval"
    for task in reversed(tasks):
        trial = native / task["id"]
        trial.mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps({
            "task_name": task["name"], "task_id": {"org": "fixture", "name": task["id"], "ref": task["ref"]},
            "config": {"agent": {"name": "oracle", "model_name": None}},
            "verifier_result": {"rewards": {"reward": 1}}, "finished_at": "done",
        }))
    (native / "result.json").write_text(json.dumps({"finished_at": "done", "n_total_trials": count}))
    calls = []
    original = evaluation._native_task_index

    def counted(spec):
        calls.append(1)
        return original(spec)

    monkeypatch.setattr(evaluation, "_native_task_index", counted)
    result = evaluation.collect_result(resolved, returncode=0)
    assert result.status == "completed"
    assert [trial.task_id for trial in result.trials] == resolved["benchmark"]["task_ids"]
    assert len(calls) == 1
    # A matching task name cannot make a result from another source revision valid.
    changed = native / "task-199/result.json"
    data = json.loads(changed.read_text())
    data["task_id"]["ref"] = "sha256:" + "f" * 64
    changed.write_text(json.dumps(data))
    result = evaluation.collect_result(resolved, returncode=0)
    assert result.status == "partial"
    assert result.trials[-1].reward is None
    assert "immutable task" in result.error
    assert len(calls) == 2


def test_duplicate_native_identity_cannot_select_one_arbitrary_record():
    task = {"id": "one", "path": "/tasks/same-path"}
    with pytest.raises(ValueError, match="ambiguous"):
        evaluation._native_task_index({"benchmark": {"tasks": [task, {**task, "id": "two"}]}})
