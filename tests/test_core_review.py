"""Regressions for the boundary between submitted work and native evidence."""
import json
from pathlib import Path
import sys

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate, preview
from rsi_eval.evaluation import CODEX_AGENT, collect_result


def test_preview_records_only_safe_runtime_provenance(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setenv("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
    monkeypatch.setenv("DOCKER_CONTEXT", "fixture-context")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-recorded")
    resolved = preview(target=TargetSpec(candidate, "oracle"),
                       benchmark=BenchmarkSelection("terminal-bench", "2.0", ("extract-elf",)),
                       output_dir=tmp_path / "output")
    assert resolved["runtime"]["docker_default_platform"] == "linux/amd64"
    assert resolved["runtime"]["docker_context"] == "fixture-context"
    assert resolved["runtime"]["host_platform"]
    assert resolved["runtime"]["host_machine"]
    assert resolved["runtime"]["python_version"]
    assert "must-not-be-recorded" not in json.dumps(resolved)


@pytest.fixture
def native_run(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("# candidate\n")
    target = TargetSpec(source, "fixture:Agent", model="specified-model")
    resolved = preview(target=target,
                       benchmark=BenchmarkSelection("terminal-bench", "2.0", ("extract-elf",)),
                       output_dir=tmp_path / "output")
    task = resolved["benchmark"]["tasks"][0]
    org, name = task["name"].split("/", 1)
    trial = {"task_name": task["name"],
             "task_id": {"org": org, "name": name, "ref": task["ref"]},
             "config": {"agent": {"import_path": target.entrypoint, "model_name": target.model}},
             "finished_at": "2026-10-09T00:00:00Z",
             "verifier": {"finished_at": "2026-10-09T00:00:00Z"},
             "verifier_result": {"rewards": {"reward": 0}},
             "agent_result": {"n_input_tokens": 2, "n_output_tokens": 1, "n_cache_tokens": 0}}
    summary = {"finished_at": "2026-10-09T00:00:00Z", "n_total_trials": 1, "stats": {}}
    directory = Path(resolved["output_dir"]) / "native/eval/trial"
    directory.mkdir(parents=True)

    def collect():
        (directory / "result.json").write_text(json.dumps(trial))
        (directory.parent / "result.json").write_text(json.dumps(summary))
        return collect_result(resolved, returncode=0)

    return trial, summary, collect


@pytest.mark.parametrize("mismatch", ["ref", "org", "name", "entrypoint", "model"])
def test_native_evidence_must_match_exact_submitted_identity(native_run, mismatch):
    trial, _, collect = native_run
    if mismatch in {"ref", "org", "name"}:
        trial["task_id"][mismatch] = "another-task-or-revision"
    elif mismatch == "entrypoint":
        trial["config"]["agent"]["import_path"] = "another:Agent"
    else:
        trial["config"]["agent"]["model_name"] = "another-model"
    result = collect()
    assert result.status == "failed"
    assert result.trials[0].status == "incomplete"
    assert result.trials[0].reward is None
    assert "does not match" in result.error


@pytest.mark.parametrize("field,value", [("finished_at", None), ("n_total_trials", 0),
                                         ("n_total_trials", 2), ("n_total_trials", True)])
def test_unfinished_or_wrong_sized_job_is_never_complete(native_run, field, value):
    _, summary, collect = native_run
    summary[field] = value
    result = collect()
    assert result.status == "partial"
    assert result.trials[0].reward == 0  # Preserve the real task outcome.
    assert "native job" in result.error


@pytest.mark.parametrize("field,value", [("verifier_result", [1]),
                                         ("agent_result", [1]),
                                         ("config", [1])])
def test_malformed_nested_native_data_is_a_reported_incomplete_trial(native_run, field, value):
    trial, _, collect = native_run
    trial[field] = value
    result = collect()
    assert result.status == "failed"
    assert result.trials[0].reward is None
    assert result.error


def test_real_zero_and_verified_timeout_remain_distinguishable(native_run):
    trial, _, collect = native_run
    zero = collect()
    assert zero.status == "completed"
    assert zero.trials[0].reward == 0
    trial["exception_info"] = {"exception_type": "AgentTimeoutError", "exception_message": "deadline"}
    trial["verifier_result"]["rewards"]["reward"] = 1
    timed_out = collect()
    assert timed_out.status == "completed"
    assert timed_out.trials[0].status == "task_timeout"
    assert timed_out.trials[0].reward == 1
    trial["verifier"]["finished_at"] = None
    unverified = collect()
    assert unverified.status == "failed"
    assert unverified.trials[0].status == "incomplete"


@pytest.mark.parametrize("mutate_snapshot", [False, True])
def test_fixed_codex_target_does_not_import_candidate_packages(tmp_path, mutate_snapshot):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("# candidate\n")
    # The candidate is loaded explicitly by the fixed target. These names must
    # not shadow dependencies during Harbor startup.
    (source / "json.py").write_text("raise RuntimeError('candidate shadowed stdlib')\n")
    fake = tmp_path / "fake-harbor"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
if '--version' in sys.argv:
    print('harbor 0.18.0 (offline fixture)')
    raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index('--config') + 1]).read_text())
task = config['tasks'][0]
agent = config['agents'][0]
snapshot = Path(agent['kwargs']['artifact_dir'])
assert str(snapshot) not in os.environ['PYTHONPATH'].split(os.pathsep)
org, name = task['name'].split('/', 1)
trial = Path(config['jobs_dir']) / config['job_name'] / 'trial'
trial.mkdir(parents=True)
(trial / 'result.json').write_text(json.dumps({'task_name': task['name'], 'task_id': {'org': org, 'name': name, 'ref': task['ref']}, 'config': {'agent': agent}, 'finished_at': '2026-10-09T00:00:00Z', 'verifier_result': {'rewards': {'reward': 1}}}))
(trial.parent / 'result.json').write_text(json.dumps({'finished_at': '2026-10-09T00:00:00Z', 'n_total_trials': 1}))
''' + ("(snapshot / 'added-link').symlink_to(snapshot / 'agent.py')\n" if mutate_snapshot else ""))
    fake.chmod(0o700)
    target = TargetSpec(source, CODEX_AGENT, {"codex_auth_dir": str(tmp_path / "private"),
                                            "codex_version": "fixture"}, "specified-model")
    result = evaluate(target=target,
                      benchmark=BenchmarkSelection("terminal-bench", "2.0", ("extract-elf",)),
                      options=RunOptions(harbor_executable=str(fake)), output_dir=tmp_path / "output")
    if mutate_snapshot:
        assert result.status == "partial"
        assert "snapshot was modified" in result.error
    else:
        assert result.status == "completed", result.error
