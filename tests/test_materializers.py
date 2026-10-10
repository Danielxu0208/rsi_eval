import hashlib
import json
from pathlib import Path
import sys

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate, preview
from rsi_eval.benchmarks import _validate_spec, resolve
from rsi_eval.materializers import common
from rsi_eval.materializers import mgsm
from rsi_eval.materializers.mgsm_verifier import score
from rsi_eval.metrics import benchmark_metrics
from rsi_eval.types import TrialResult


@pytest.mark.parametrize(("target", "answer", "expected"), [
    ("1000", "1,000.00\n", True), ("42", " 42 ", True),
    ("42", "The answer is 42", False), ("42", "42.1", False),
    ("-5", "-5.0", True), ("42", "", False), ("42", "420", False),
])
def test_mgsm_numeric_scoring(target, answer, expected):
    assert score(target, answer) is expected


def fake_mgsm_source(url, destination, sha256):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.name == "LICENSE":
        destination.write_text("fixture license\n")
    else:
        destination.write_text("\n".join(f"Fixture question {i}\t{10000+i}" for i in range(250)))
    return destination


def test_preparation_keeps_answers_out_of_agent_context(monkeypatch, tmp_path):
    monkeypatch.setattr(mgsm, "download", fake_mgsm_source)
    spec = resolve(BenchmarkSelection("mgsm", "1.0-agent", task_ids=("zh-002", "en-001")))
    tasks = mgsm.prepare(spec, tmp_path)
    assert [row["id"] for row in tasks] == ["zh-002", "en-001"]
    root = Path(tasks[0]["path"])
    assert "Fixture question 1" in (root / "instruction.md").read_text()
    assert "10001" not in (root / "instruction.md").read_text()
    assert "COPY" not in (root / "environment/Dockerfile").read_text()
    assert json.loads((root / "tests/expected.json").read_text())["answer"] == "10001"


def test_materialized_preview_is_offline_and_pinned(monkeypatch, tmp_path):
    monkeypatch.setattr(common.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network used in preview"))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "agent.py").write_text("VALUE = 1\n")
    result = preview(target=TargetSpec(candidate, "fixture:Agent"), benchmark=BenchmarkSelection(
        "mgsm", "1.0-agent", task_ids=("en-001",)), output_dir=tmp_path / "out")
    assert result["expected_trials"] == 1
    assert len(result["benchmark"]["source"]["commit"]) == 40
    assert not (tmp_path / "out").exists()
    spec = result["benchmark"]
    spec["source"] = {"revision": "main"}
    with pytest.raises(ValueError, match="immutable"):
        _validate_spec(spec)


def test_source_cache_is_opt_in_and_always_verified(monkeypatch, tmp_path):
    payload = b"public source"
    digest = hashlib.sha256(payload).hexdigest()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / digest).write_bytes(payload)
    monkeypatch.setenv("RSI_EVAL_SOURCE_CACHE", str(cache))
    monkeypatch.setattr(common.urllib.request, "urlopen", lambda *a, **k: pytest.fail("unexpected download"))
    assert common.download("https://example.test/source", tmp_path / "one", digest).read_bytes() == payload
    (cache / digest).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        common.download("https://example.test/source", tmp_path / "two", digest)
    assert not (tmp_path / "two").exists()
    assert not list(tmp_path.glob("*.part"))


def test_pass_at_k_is_not_mean_reward_and_incomplete_is_unknown():
    spec = {"tasks": [{"id": "one"}, {"id": "two"}], "selection": "subset",
            "aggregation": {"type": "pass_at_k", "k": [1, 2, 4]}}
    trials = [TrialResult(task, i, "completed", reward) for task, rewards in (
        ("one", [1, 0, 0]), ("two", [1, 1, 0])) for i, reward in enumerate(rewards)]
    result = benchmark_metrics(trials, spec, 3)
    assert result["pass_at_k"]["1"] == pytest.approx(0.5)
    assert result["pass_at_k"]["2"] == pytest.approx(5 / 6)
    assert result["pass_at_k"]["4"] is None
    trials[0].status = "infra_error"
    assert benchmark_metrics(trials, spec, 3)["pass_at_k"]["1"] is None
    trials[0].status = "completed"
    trials[0].reward = 0.5
    assert benchmark_metrics(trials, spec, 3)["pass_at_k"]["1"] is None


def test_sdk_runs_prepared_local_task_and_keeps_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr(mgsm, "download", fake_mgsm_source)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "agent.py").write_text("VALUE = 1\n")
    fake = tmp_path / "fake-harbor"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
if '--version' in sys.argv:
    print('harbor 0.18.0 offline fixture')
    raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
job = Path(config['jobs_dir']) / config['job_name']
task = config['tasks'][0]
trial = job / 'trial'
trial.mkdir(parents=True)
(trial / 'result.json').write_text(json.dumps({'task_name': Path(task['path']).name,
 'task_id': {'path': task['path']}, 'config': {'agent': config['agents'][0]},
 'finished_at': 'done', 'verifier_result': {'rewards': {'reward': 1}}}))
(job / 'result.json').write_text(json.dumps({'finished_at': 'done', 'n_total_trials': 1}))
''')
    fake.chmod(0o755)
    result = evaluate(target=TargetSpec(candidate, "fixture:Agent"), benchmark=BenchmarkSelection(
        "mgsm", "1.0-agent", task_ids=("en-001",)), options=RunOptions(harbor_executable=str(fake)),
        output_dir=tmp_path / "out")
    assert result.status == "completed", result.error
    assert result.resolved["benchmark"]["tasks"][0]["record_id"] == 0
    assert len(result.resolved["execution_tasks"][0]["sha256"]) == 64
    assert result.metrics["benchmark_metrics"]["accuracy_by_language"] == {"en": 1.0}


def test_source_failure_is_infrastructure_not_wrong_answer(monkeypatch, tmp_path):
    def fail(*args):
        raise OSError("download unavailable")
    monkeypatch.setattr(mgsm, "download", fail)
    monkeypatch.setattr("rsi_eval.evaluation._probe", lambda *a, **k: {"ok": True, "detail": "harbor 0.18.0"})
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "agent.py").write_text("pass\n")
    result = evaluate(target=TargetSpec(candidate, "fixture:Agent"), benchmark=BenchmarkSelection(
        "mgsm", "1.0-agent", task_ids=("en-001",)), output_dir=tmp_path / "out")
    assert result.status == "failed"
    assert result.trials[0].status == "infra_error"
    assert result.trials[0].reward is None
    assert result.metrics["benchmark_metrics"]["accuracy"] is None


def test_missing_backend_does_not_download_large_sources(monkeypatch, tmp_path):
    monkeypatch.setattr("rsi_eval.evaluation._probe", lambda *a, **k: {"ok": False, "detail": "not installed"})
    monkeypatch.setattr(mgsm, "download", lambda *a, **k: pytest.fail("must not download before backend check"))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "agent.py").write_text("pass\n")
    result = evaluate(target=TargetSpec(candidate, "fixture:Agent"), benchmark=BenchmarkSelection(
        "mgsm", "1.0-agent", task_ids=("en-001",)), output_dir=tmp_path / "out")
    assert result.status == "failed"
    assert "requires Harbor" in result.error
