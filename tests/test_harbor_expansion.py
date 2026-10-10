"""Offline coverage for immutable catalogs and the shared Harbor boundary."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate, preview
from rsi_eval.benchmarks import list_benchmarks, resolve
from rsi_eval.evaluation import CODEX_AGENT, DOCKER_ENVIRONMENT, _job_config


CATALOG = Path(__file__).parents[1] / "src/rsi_eval/data/catalog_harbor_expansion.json"
VERSION = "1.0-harbor"
EXPECTED_COUNTS = {
    "aider-polyglot": 225,
    "swe-bench-multilingual": 300,
    "swe-bench-pro": 731,
}
SMOKE_IDS = {
    "aider-polyglot": "polyglot_python_dot-dsl",
    "swe-bench-multilingual": "preactjs__preact-3345",
    "swe-bench-pro": "instance_flipt-io__flipt-a0cbc0cb65ae601270bdbe3f5313e2dfd49c80e4",
}


@pytest.fixture
def candidate(tmp_path):
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "agent.py").write_text("def build_prompt(instruction, artifact_dir):\n    return instruction\n")
    return TargetSpec(source, CODEX_AGENT,
                      {"codex_auth_dir": str(tmp_path / "unused-auth"),
                       "codex_version": "fixture-version"}, model="fixture-model")


@pytest.mark.parametrize("name,count", EXPECTED_COUNTS.items())
def test_complete_catalog_is_discoverable_and_every_task_is_immutable(name, count):
    specs = json.loads(CATALOG.read_text())
    spec = next(row for row in specs if row["name"] == name)
    assert (name, VERSION) in {(row["name"], row["version"]) for row in list_benchmarks()}
    full = resolve(BenchmarkSelection(name, VERSION))
    assert full["tasks"] == spec["tasks"]
    assert len(full["tasks"]) == count
    ids = [task["id"] for task in full["tasks"]]
    assert ids == sorted(set(ids))
    assert SMOKE_IDS[name] in ids
    digest = hashlib.sha256(json.dumps(spec["tasks"], sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    assert digest == spec["provenance"]["task_manifest_sha256"]
    for task in full["tasks"]:
        if "name" in task:
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", task["ref"])
        else:
            assert re.fullmatch(r"[0-9a-f]{40}", task["git_commit_id"])
            assert not Path(task["path"]).is_absolute()
            assert ".." not in Path(task["path"]).parts
    if name == "swe-bench-multilingual":
        # One registry task has a separate repair commit. Flattening the source
        # to the first task's revision would silently change the benchmark.
        counts = dict(Counter(task["git_commit_id"] for task in full["tasks"]))
        assert counts == spec["provenance"]["task_git_commit_counts"]
        assert sorted(counts.values()) == [1, 299]
        assert spec["dataset_ref"] == "manifest-sha256:" + digest
    else:
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", spec["dataset_ref"])


@pytest.mark.parametrize("name", EXPECTED_COUNTS)
def test_preview_preserves_protocol_and_manifest_selection(candidate, tmp_path, name):
    manifest = tmp_path / "tasks.json"
    manifest.write_text(json.dumps({"task_ids": [SMOKE_IDS[name]]}))
    output = tmp_path / "preview"
    resolved = preview(target=candidate, benchmark=BenchmarkSelection(name, VERSION, manifest=manifest),
                       output_dir=output)
    assert resolved["benchmark"]["task_ids"] == [SMOKE_IDS[name]]
    assert resolved["benchmark"]["protocol"]["variant"] == "harbor-port"
    assert resolved["benchmark"]["protocol"]["native_harness_reproduction"] is False
    assert not output.exists()
    if name == "aider-polyglot":
        assert resolved["benchmark"]["protocol"]["native_retry_loop_implemented"] is False
    with pytest.raises(ValueError, match="unknown task"):
        resolve(BenchmarkSelection(name, VERSION, ("not-a-task",)))


@pytest.mark.parametrize("name", EXPECTED_COUNTS)
def test_entire_catalog_validates_with_installed_harbor_without_runtime_overrides(candidate, tmp_path, name):
    model = pytest.importorskip("harbor.models.job.config")
    resolved = preview(target=candidate, benchmark=BenchmarkSelection(name, VERSION),
                       output_dir=tmp_path / name)
    config = _job_config(resolved, tmp_path / "snapshot")
    parsed = model.JobConfig.model_validate(config)
    assert len(parsed.tasks) == EXPECTED_COUNTS[name]
    assert config["environment"] == {"type": "docker", "import_path": DOCKER_ENVIRONMENT}
    assert config["retry"] == {"max_retries": 0}
    assert all("source" not in task and "id" not in task for task in config["tasks"])
    if name == "swe-bench-multilingual":
        assert {task.git_commit_id for task in parsed.tasks} == {
            task["git_commit_id"] for task in resolved["benchmark"]["tasks"]
        }


@pytest.fixture
def fake_harbor(tmp_path):
    executable = tmp_path / "fake-harbor"
    executable.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("harbor 0.18.0 (offline fixture)")
    raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index("--config") + 1]).read_text())
agent = config["agents"][0]
assert agent["import_path"] == "rsi_eval.harbor_codex:Agent"
assert (Path(agent["kwargs"]["artifact_dir"]) / "agent.py").is_file()
job = Path(config["jobs_dir"]) / config["job_name"]
for task in config["tasks"]:
    if "name" in task:
        org, short_name = task["name"].split("/", 1)
        identity = {"org": org, "name": short_name, "ref": task["ref"]}
        native_name = task["name"]
    else:
        short_name = Path(task["path"]).name
        identity = {key: task[key] for key in ("git_url", "git_commit_id", "path")}
        native_name = short_name
        if os.environ.get("RSI_FIXTURE_WRONG_COMMIT"):
            identity["git_commit_id"] = "0" * 40
    for attempt in range(config["n_attempts"]):
        trial = job / f"{short_name}__fixture{attempt}"
        trial.mkdir(parents=True)
        (trial / "agent").mkdir()
        (trial / "agent/trajectory.json").write_text('{"fixture": true}')
        (trial / "result.json").write_text(json.dumps({
            "task_name": native_name, "task_id": identity, "config": {"agent": agent},
            "finished_at": "2026-10-10T00:00:00Z",
            "verifier": {"finished_at": "2026-10-10T00:00:00Z"},
            "verifier_result": {"rewards": {"reward": float(attempt % 2)}},
            "agent_result": {"n_input_tokens": 20, "n_output_tokens": 3, "n_cache_tokens": 5}
        }))
(job / "result.json").write_text(json.dumps({
    "finished_at": "2026-10-10T00:00:00Z",
    "n_total_trials": len(config["tasks"]) * config["n_attempts"],
    "stats": {"fixture_metric": "native-summary-preserved"}
}))
''')
    executable.chmod(0o755)
    return str(executable)


@pytest.mark.parametrize("name", EXPECTED_COUNTS)
def test_shared_sdk_preserves_independent_trials_and_native_metrics(candidate, tmp_path, fake_harbor, name):
    result = evaluate(target=candidate,
                      benchmark=BenchmarkSelection(name, VERSION, (SMOKE_IDS[name],)),
                      options=RunOptions(repetitions=2, harbor_executable=fake_harbor),
                      output_dir=tmp_path / "run")
    assert result.status == "completed", result.error
    assert [trial.repetition for trial in result.trials] == [0, 1]
    assert [trial.reward for trial in result.trials] == [0.0, 1.0]
    assert all(trial.task_id == SMOKE_IDS[name] for trial in result.trials)
    assert all(Path(trial.trajectory_path).is_file() for trial in result.trials)
    assert result.metrics["stats"] == {"fixture_metric": "native-summary-preserved"}
    assert result.usage["input_tokens"] == 40
    assert result.usage["cost_usd"] is None
    persisted = json.loads(Path(result.report_path).read_text())
    assert persisted["resolved"]["benchmark"]["protocol"]["variant"] == "harbor-port"


def test_git_trial_from_another_commit_cannot_be_scored(candidate, tmp_path, fake_harbor, monkeypatch):
    monkeypatch.setenv("RSI_FIXTURE_WRONG_COMMIT", "1")
    name = "swe-bench-multilingual"
    result = evaluate(target=candidate,
                      benchmark=BenchmarkSelection(name, VERSION, (SMOKE_IDS[name],)),
                      options=RunOptions(harbor_executable=fake_harbor), output_dir=tmp_path / "wrong")
    assert result.status == "failed"
    assert result.trials[0].reward is None
    assert "does not match" in result.error
