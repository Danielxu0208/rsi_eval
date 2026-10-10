import json
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate, preview
from rsi_eval.benchmarks import resolve
from rsi_eval.cli import main
from rsi_eval.evaluation import _job_config, collect_result


@pytest.fixture
def candidate(tmp_path):
    path = tmp_path / "source"
    path.mkdir()
    (path / "agent.py").write_text("CANDIDATE = 'one'\n")
    return TargetSpec(path, "fixture:Agent")


@pytest.fixture
def selection():
    return BenchmarkSelection("terminal-bench", "2.0", task_ids=("extract-elf",))


def test_catalog_resolution_pins_every_task_and_rejects_ambiguous_selection(tmp_path):
    full = resolve(BenchmarkSelection("swe-bench-verified", "1"))
    assert len(full["tasks"]) == 500
    assert all(t["ref"].startswith("sha256:") and len(t["ref"]) == 71 for t in full["tasks"])
    manifest = tmp_path / "tasks.json"
    manifest.write_text(json.dumps({"task_ids": ["astropy__astropy-12907"]}))
    assert resolve(BenchmarkSelection("swe-bench-verified", "1", manifest=manifest))["selection"] == "subset"
    for ids in ((), ("missing",), ("extract-elf", "extract-elf")):
        with pytest.raises(ValueError):
            resolve(BenchmarkSelection("terminal-bench", "2.0", task_ids=ids))


def test_preview_and_cli_use_same_schema_without_model_calls(candidate, selection, tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"target": {"artifact_dir": "source", "entrypoint": "fixture:Agent"},
                                  "benchmark": {"name": "terminal-bench", "version": "2.0", "task_ids": ["extract-elf"]},
                                  "output_dir": "output"}))
    direct = preview(target=candidate, benchmark=selection, output_dir=tmp_path / "output")
    assert main(["preview", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out) == direct
    assert not (tmp_path / "output").exists()


def test_generated_job_validates_with_actual_harbor(candidate, selection, tmp_path):
    model = pytest.importorskip("harbor.models.job.config")
    resolved = preview(target=candidate, benchmark=selection, output_dir=tmp_path / "out")
    config = model.JobConfig.model_validate(_job_config(resolved, tmp_path / "candidate"))
    assert config.retry.max_retries == 0
    assert config.tasks[0].name == "terminal-bench/extract-elf"
    assert config.tasks[0].ref.startswith("sha256:")


def test_candidate_changes_produce_new_hash_and_old_results_never_reused(candidate, selection, tmp_path):
    one = preview(target=candidate, benchmark=selection, output_dir=tmp_path / "out")
    (candidate.artifact_dir / "agent.py").write_text("CANDIDATE = 'two'\n")
    two = preview(target=candidate, benchmark=selection, output_dir=tmp_path / "out")
    assert one["target"]["sha256"] != two["target"]["sha256"]
    out = tmp_path / "out"
    out.mkdir()
    (out / "result.json").write_text("{}")
    with pytest.raises(FileExistsError):
        evaluate(target=candidate, benchmark=selection, output_dir=out)
    with pytest.raises(ValueError):
        preview(target=candidate, benchmark=selection, output_dir=candidate.artifact_dir / "out")


def write_native(directory, resolved, *, reward=0, error=None, tokens=True):
    task = resolved["benchmark"]["tasks"][0]
    org, name = task["name"].split("/", 1)
    target = resolved["target"]
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps({
        "task_name": "terminal-bench/extract-elf", "finished_at": "2026-10-09T00:00:00Z",
        "verifier": {"finished_at": "2026-10-09T00:00:00Z"},
        "task_id": {"org": org, "name": name, "ref": task["ref"]},
        "config": {"agent": {"import_path": target["entrypoint"], "model_name": target["model"]}},
        "verifier_result": {"rewards": {"reward": reward}}, "exception_info": error,
        "agent_result": {"n_input_tokens": 100, "n_output_tokens": 7, "n_cache_tokens": 30} if tokens else None,
    }))


@pytest.mark.parametrize(("error", "expected"), [
    (None, "completed"),
    ({"exception_type": "CodexAuthError", "exception_message": "login expired"}, "auth_error"),
    ({"exception_type": "ApiUsageLimitError", "exception_message": "quota exceeded"}, "quota_exceeded"),
    ({"exception_type": "AgentTimeoutError", "exception_message": "time"}, "task_timeout"),
    ({"exception_type": "NonZeroAgentExitCodeError", "exception_message": "exit 1"}, "agent_error"),
    ({"exception_type": "NonZeroAgentExitCodeError", "exception_message": "codex exec -c cli_auth_credentials_store=file -c forced_login_method=chatgpt exited 1"}, "agent_error"),
    ({"exception_type": "NonZeroAgentExitCodeError", "exception_message": "Task: implement HTTP 401 and HTTP 429 quota handling; command exit 1"}, "agent_error"),
    ({"exception_type": "RuntimeError", "exception_message": "docker unavailable"}, "infra_error"),
])
def test_native_failure_semantics_and_real_zero_score(candidate, selection, tmp_path, error, expected):
    out = tmp_path / "out"
    resolved = preview(target=candidate, benchmark=selection, output_dir=out)
    write_native(out / "native/eval/extract-elf__one", resolved, error=error)
    summary = {"stats": {"native_metric": 0}, "finished_at": "2026-10-09T00:00:00Z", "n_total_trials": 1}
    (out / "native/eval/result.json").write_text(json.dumps(summary))
    result = collect_result(resolved, returncode=0)
    assert result.trials[0].status == expected
    assert result.status == ("completed" if expected in {"completed", "task_timeout", "agent_error"} else "failed")
    if result.status == "failed":
        assert "first failure extract-elf" in result.error
    assert result.trials[0].reward == 0
    assert result.usage["input_tokens"] == 100
    assert result.usage["cached_input_tokens"] == 30
    assert result.metrics == summary


def test_missing_trials_and_tokens_remain_unknown(candidate, selection, tmp_path):
    out = tmp_path / "out"
    resolved = preview(target=candidate, benchmark=selection, options=RunOptions(repetitions=2), output_dir=out)
    write_native(out / "native/eval/extract-elf__one", resolved, tokens=False)
    (out / "native/eval/result.json").write_text(json.dumps({"finished_at": "2026-10-09T00:00:00Z", "n_total_trials": 2}))
    result = collect_result(resolved, returncode=0)
    assert result.status == "partial"
    assert len(result.trials) == 2
    assert result.trials[1].reward is None
    assert result.usage["input_tokens"] is None
    assert result.usage["cost_usd"] is None


def test_subprocess_config_and_snapshot_are_the_ones_actually_consumed(candidate, selection, tmp_path):
    fake = tmp_path / "fake-harbor"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
if '--version' in sys.argv:
    print('harbor 0.18.0 (offline fixture)')
    raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
from fixture import VALUE
task = config['tasks'][0]
org, name = task['name'].split('/', 1)
job = Path(config['jobs_dir']) / config['job_name']
trial = job / 'extract-elf__fixture'
trial.mkdir(parents=True)
(trial / 'result.json').write_text(json.dumps({'task_name': task['name'], 'task_id': {'org': org, 'name': name, 'ref': task['ref']}, 'config': {'agent': config['agents'][0]}, 'finished_at': '2026-10-09T00:00:00Z', 'agent_result': {'n_input_tokens': 2, 'n_output_tokens': 1, 'n_cache_tokens': 0}, 'verifier_result': {'rewards': {'reward': VALUE}}}))
(job / 'result.json').write_text(json.dumps({'fixture': True, 'stats': {'reward': VALUE}, 'finished_at': '2026-10-09T00:00:00Z', 'n_total_trials': 1}))
''')
    fake.chmod(0o755)
    (candidate.artifact_dir / "fixture.py").write_text("VALUE = 1\n")
    one = evaluate(target=candidate, benchmark=selection, options=RunOptions(harbor_executable=str(fake)), output_dir=tmp_path / "one")
    (candidate.artifact_dir / "fixture.py").write_text("VALUE = 0\n")
    two = evaluate(target=candidate, benchmark=selection, options=RunOptions(harbor_executable=str(fake)), output_dir=tmp_path / "two")
    assert one.status == two.status == "completed"
    assert [one.trials[0].reward, two.trials[0].reward] == [1, 0]
    assert one.resolved["target"]["sha256"] != two.resolved["target"]["sha256"]
    assert (tmp_path / "one/candidate/fixture.py").read_text() == "VALUE = 1\n"
