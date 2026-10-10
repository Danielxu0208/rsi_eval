import copy
import hashlib
import json
from pathlib import Path
import tomllib

import pytest

from rsi_eval.materializers import frontier_eng as adapter
from rsi_eval.materializers import frontier_eng_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalog():
    return json.loads((ROOT / "src/rsi_eval/data/catalog_frontier_eng.json").read_text())[0]


def broker(scores=None, budget=3):
    observed = []

    def grade(source):
        observed.append(source)
        if source == "infra":
            raise RuntimeError("dependency failure")
        value = float(source)
        return {"metrics": {"combined_score": value, "valid": float(value >= 0)},
                "artifacts": {"outputs": {"reference_result.json": {"solution": "PRIVATE_REFERENCE"}},
                              "error_message": "candidate diagnostic", "candidate_stderr": "stderr"}}

    result = runtime.Broker({"baseline_source": "0.25", "candidate_budget": budget,
                             "task_id": "example", "protocol": adapter.PROTOCOL}, grade)
    return result, observed


def test_subset_is_exact_and_preserves_separate_native_objectives(catalog):
    assert {task["id"] for task in catalog["tasks"]} == set(adapter.TASKS)
    coverage = catalog["coverage"]
    assert coverage["supported_task_count"] == 2
    assert coverage["upstream_v1_task_count"] == 47
    assert len(coverage["unsupported_task_ids"]) == 45
    assert not set(adapter.TASKS).intersection(coverage["unsupported_task_ids"])
    assert catalog["aggregation"]["type"] == "frontier_native"
    assert catalog["protocol"]["candidate_budget"] == 100
    for task in catalog["tasks"]:
        assert task["objective"] == {"metric": "combined_score", "direction": "maximize"}


def test_feedback_budget_counts_failed_candidates_and_reserves_final():
    session, observed = broker()
    assert session.status()["best"]["metrics"]["combined_score"] == .25
    session.feedback("0.5")
    session.feedback("-1e18")
    with pytest.raises(ValueError, match="budget exhausted"):
        session.feedback("0.8")
    result = session.final("0.75")
    assert result["candidate_calls"] == 3
    assert result["feedback_calls"] == 2
    assert result["best_metrics"]["combined_score"] == .75
    assert result["records"][2]["metrics"]["combined_score"] == -1e18
    assert observed == ["0.25", "0.5", "-1e18", "0.75", "0.75"]
    with pytest.raises(ValueError, match="finalized"):
        session.final("0.8")
    with pytest.raises(ValueError, match="closed"):
        session.feedback("0.8")


def test_best_feasible_retains_earlier_code_and_baseline():
    session, observed = broker()
    session.feedback("0.9")
    result = session.final("-1e18")
    assert result["best_index"] == 1
    assert result["records"][result["best_index"]]["source"] == "0.9"
    assert observed[-1] == "0.9"
    session, _ = broker()
    result = session.final("-1e18")
    assert result["best_index"] == 0
    assert result["best_metrics"]["combined_score"] == .25


def test_feedback_hides_reference_outputs_but_final_report_keeps_them():
    session, _ = broker()
    feedback = session.feedback("0.5")
    assert "PRIVATE_REFERENCE" not in json.dumps(feedback)
    assert "candidate diagnostic" in json.dumps(feedback)
    assert "PRIVATE_REFERENCE" in json.dumps(session.final("0.5"))


def test_environment_failure_does_not_become_a_zero_score():
    session, _ = broker()
    feedback = session.feedback("infra")
    assert feedback["error"]
    assert feedback["evaluation"]["status"] == "verifier_error"
    assert "metrics" not in feedback["evaluation"]
    result = session.final("0.8")
    assert result["error"]
    assert result["final_verification_evaluations"] == 0


def test_native_randomness_is_retained_as_confirmation_diagnostics():
    session, _ = broker()
    original = session.evaluate
    calls = 0
    def changing(source):
        nonlocal calls
        calls += 1
        value = original(source)
        if calls == 3:
            value["metrics"]["combined_score"] += .01
        return value
    session.evaluate = changing
    result = session.final("0.6")
    assert result["error"] is None
    assert result["best_metrics"]["combined_score"] == .6
    assert result["confirmation"]["score_delta_from_selected"] == pytest.approx(.01)


@pytest.mark.parametrize("value", [None, {}, {"combined_score": True, "valid": 1},
    {"combined_score": float("nan"), "valid": 1}, {"combined_score": 0.2, "valid": 0.5}])
def test_invalid_native_metrics_fail_closed(value):
    with pytest.raises(RuntimeError):
        runtime.check_metrics(value)


def test_frozen_source_hash_checks_include_symlink_rejection(tmp_path):
    source = tmp_path / "grader.py"
    source.write_text("original")
    expected = {"grader.py": hashlib.sha256(b"original").hexdigest()}
    runtime.verify_files(tmp_path, expected)
    source.write_text("changed")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        runtime.verify_files(tmp_path, expected)
    source.unlink()
    (tmp_path / "other.py").write_text("original")
    source.symlink_to("other.py")
    with pytest.raises(RuntimeError, match="missing frozen"):
        runtime.verify_files(tmp_path, expected)


def test_preparation_builds_separate_private_grader_and_public_client(catalog, tmp_path, monkeypatch):
    # Synthetic downloaded text exercises packaging without redistributing
    # upstream source. Real pinned source validation is a separate runtime check.
    def fake_download(url, target, expected):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# " + url.rsplit("/", 1)[-1] + "\n")
        return target
    monkeypatch.setattr(adapter, "download", fake_download)
    monkeypatch.setenv("RSI_EVAL_FRONTIER_PLATFORM", "linux/arm64")
    tasks = adapter.prepare(catalog, tmp_path)
    for item in tasks:
        task = Path(item["path"])
        config = tomllib.loads((task / "task.toml").read_text())
        assert config["metadata"]["grader_platform"] == "linux/arm64"
        assert config["agent"]["timeout_sec"] == 600
        agent_dir, = (task / "environment").glob("agent-*")
        grader_dir, = (task / "environment").glob("grader-*")
        private = json.loads((grader_dir / "broker.json").read_text())
        token = private["verifier_token"]
        assert private["candidate_budget"] == 100
        assert token == json.loads((task / "tests/verifier.json").read_text())["verifier_token"]
        assert all(token not in p.read_text() for p in agent_dir.rglob("*") if p.is_file())
        assert not list(agent_dir.rglob("reference.py"))
        compose = (task / "environment/docker-compose.yaml").read_text()
        assert "internal: true" in compose
        assert "privileged:" not in compose and "docker.sock" not in compose
        assert "seccomp=unconfined" in compose and "systempaths=unconfined" in compose
        assert "/opt/frontier_runtime.py feedback" in (task / "instruction.md").read_text()


@pytest.mark.parametrize("mutation", ["commit", "unsupported", "empty", "duplicate"])
def test_bad_identity_fails_before_download(catalog, tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(adapter, "download", lambda *_: pytest.fail("unexpected download"))
    if mutation == "commit":
        catalog["source"]["commit"] = "wrong"
    elif mutation == "unsupported":
        catalog["tasks"] = [{"id": "KernelEngineering/MLA", "record_id": "KernelEngineering/MLA"}]
    elif mutation == "empty":
        catalog["tasks"] = []
    else:
        catalog["tasks"] *= 2
    with pytest.raises(ValueError):
        adapter.prepare(catalog, tmp_path)


def test_native_architecture_default_and_explicit_override(monkeypatch):
    monkeypatch.delenv("RSI_EVAL_FRONTIER_PLATFORM", raising=False)
    monkeypatch.setattr(adapter.platform, "machine", lambda: "aarch64")
    assert adapter.grader_platform() == "linux/arm64"
    monkeypatch.setattr(adapter.platform, "machine", lambda: "x86_64")
    assert adapter.grader_platform() == "linux/amd64"
    monkeypatch.setenv("RSI_EVAL_FRONTIER_PLATFORM", "macos/arm64")
    with pytest.raises(ValueError):
        adapter.grader_platform()


def test_conflicting_docker_default_fails_before_download(catalog, tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
    monkeypatch.setenv("RSI_EVAL_FRONTIER_PLATFORM", "linux/arm64")
    monkeypatch.setattr(adapter, "download", lambda *_: pytest.fail("unexpected download"))
    with pytest.raises(ValueError, match="unset DOCKER_DEFAULT_PLATFORM"):
        adapter.prepare(catalog, tmp_path)
    assert not list(tmp_path.iterdir())
