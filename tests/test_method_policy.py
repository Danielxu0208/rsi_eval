"""Trusted objective callbacks cannot change raw scores or bypass native gates."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from rsi_eval.method_policy import normalize_policy, policy_identity, score_trial, select_candidate


def trial(**kwargs):
    return {"task_id": "one", "status": "completed", "reward": .4, "metrics": {"f1": .9}, **kwargs}


def test_mapping_preserves_raw_and_requires_explicit_binary_objective(tmp_path):
    raw = trial()
    before = deepcopy(raw)
    policy = normalize_policy({"score": {"kind": "threshold", "metric": "metrics.f1", "threshold": .8}}, tmp_path)
    assert score_trial(policy, raw, {}, require_binary=True) == 1
    assert raw == before
    with pytest.raises(ValueError, match="binary"):
        score_trial({}, raw, {}, require_binary=True)
    linear = normalize_policy({"score": {"kind": "linear", "min": 0, "max": 2, "direction": "minimize"}}, tmp_path)
    assert score_trial(linear, raw, {}, unit_interval=True) == .8


@pytest.mark.parametrize("record", [trial(status="infra_error"), trial(reward=None), trial(reward=float("nan")), trial(reward=True)])
def test_policy_cannot_rescue_missing_or_invalid_measurement(tmp_path, record):
    policy = normalize_policy({"score": {"kind": "threshold", "threshold": 0}}, tmp_path)
    with pytest.raises(ValueError):
        score_trial(policy, record, {})


def test_python_source_is_frozen_outside_candidate_and_verified(tmp_path):
    callback = tmp_path / "policy.py"
    callback.write_text("def score(payload):\n    payload['trial']['reward'] = 77\n    return .75\n")
    policy = normalize_policy({"score": {"kind": "python", "path": str(callback), "function": "score"}}, tmp_path)
    callback.write_text("raise AssertionError('changed original must not run')")
    raw = trial()
    assert score_trial(policy, raw, {}, unit_interval=True) == .75
    assert raw["reward"] == .4
    assert "source" not in policy_identity(policy)["score"]
    with pytest.raises(ValueError, match="outside evolvable"):
        normalize_policy(policy, tmp_path, candidate_roots=[tmp_path])
    policy["score"]["source"] += "\n# tampered"
    with pytest.raises(ValueError, match="hash"):
        score_trial(policy, raw, {})


def test_selector_receives_only_eligible_choices_and_cannot_add_one(tmp_path):
    path = tmp_path / "policy.py"
    path.write_text("def choose(payload):\n    return min(payload['candidates'], key=lambda c: c['tokens'])['id']\n")
    policy = normalize_policy({"selection": {"kind": "python", "path": str(path), "function": "choose"}}, tmp_path)
    assert select_candidate(policy, [{"id": "A", "tokens": 20}, {"id": "B", "tokens": 10}], "A") == "B"
    path.write_text("def choose(payload):\n    return 'rejected-by-critic'\n")
    bad = normalize_policy({"selection": {"kind": "python", "path": str(path), "function": "choose"}}, tmp_path)
    with pytest.raises(ValueError, match="outside native gates"):
        select_candidate(bad, [{"id": "A"}], "A")


def test_rrsi_manifest_and_full_selection_freeze_and_detect_source_drift(tmp_path, monkeypatch):
    from rsi_eval import benchmarks
    from integrations.rrsi.prepare import normalize_config, verify_frozen_selections
    spec = {"name": "fixture", "version": "1", "dataset_ref": "fixture-immutable",
            "tasks": [{"id": name, "name": name, "ref": "sha256:" + "1" * 64} for name in ("one", "two")]}
    monkeypatch.setattr(benchmarks, "list_benchmarks", lambda: [deepcopy(spec)])
    cfg = {"codex": {"model": "fixture", "auth_dir": "auth", "expected_version": "fixture"},
           "evolve": {"name": "fixture", "version": "1"}}
    full = normalize_config(cfg, tmp_path)
    assert full["evolve"]["task_ids"] == ["one", "two"]
    assert full["smoke"]["task_ids"] == ["one"]
    assert full["selection_provenance"]["evolve"]["selection"] == "full"
    manifest = tmp_path / "tasks.json"
    manifest.write_text(json.dumps(["two", "one"]))
    cfg["evolve"]["manifest"] = "tasks.json"
    frozen = normalize_config(cfg, tmp_path)
    manifest.write_text(json.dumps(["one"]))
    assert frozen["evolve"]["task_ids"] == ["two", "one"]
    assert "manifest" not in frozen["evolve"]
    verify_frozen_selections(frozen)
    spec["tasks"][0]["ref"] = "sha256:" + "2" * 64
    with pytest.raises(ValueError, match="content changed"):
        verify_frozen_selections(frozen)
