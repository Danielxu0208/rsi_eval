"""Reject incompatible evolution scores before external work begins."""
import importlib
import json
import sys
from pathlib import Path

import pytest

from integrations.meta_harness import launcher
from integrations.rrsi.prepare import normalize_config


@pytest.fixture
def score_catalog(monkeypatch):
    from rsi_eval import benchmarks

    specs = [
        {"name": name, "version": "fixture", "dataset_ref": "fixture",
         "aggregation": {"type": kind},
         "tasks": [{"id": "one", "name": "fixture/task", "ref": "sha256:" + "1" * 64}]}
        for name, kind in (("binary-fixture", "pass_at_k"),
                           ("fractional-fixture", "drop_em_f1"),
                           ("heterogeneous-fixture", "frontier_native"))
    ]
    monkeypatch.setattr(benchmarks, "list_benchmarks", lambda: specs)


def selection(name):
    return {"name": name, "version": "fixture", "task_ids": ["one"]}


def config(tmp_path, evolve):
    return {"codex": {"model": "fixture", "auth_dir": str(tmp_path / "auth"),
                      "expected_version": "fixture"},
            "evolve": selection(evolve), "test": selection("heterogeneous-fixture"),
            "smoke": selection("heterogeneous-fixture")}


@pytest.mark.parametrize("name", ["fractional-fixture", "heterogeneous-fixture"])
def test_meta_rejects_continuous_evolve_before_checkout_or_model_work(tmp_path, monkeypatch, score_catalog, name):
    import rsi_eval.codex

    def unexpected(*args, **kwargs):
        pytest.fail("incompatible scoring reached an execution boundary")

    monkeypatch.setattr(rsi_eval.codex, "codex_version", unexpected)
    monkeypatch.setattr(launcher, "prepare_checkout", unexpected)
    cfg = config(tmp_path, name)
    cfg["evolve"] = [selection("binary-fixture"), cfg["evolve"]]
    output = tmp_path / "run"
    with pytest.raises(launcher.IntegrationError, match="reward > 0"):
        launcher.run_experiment(cfg, output, evaluate_fn=unexpected)
    assert not output.exists()


def test_rrsi_rejects_frontier_during_config_normalization(tmp_path, score_catalog):
    with pytest.raises(ValueError, match=r"comparable rewards in \[0, 1\]"):
        normalize_config(config(tmp_path, "heterogeneous-fixture"), tmp_path)


@pytest.mark.parametrize("name", ["binary-fixture", "fractional-fixture"])
def test_rrsi_allows_drop_evolve_and_continuous_smoke_test(tmp_path, score_catalog, name):
    result = normalize_config(config(tmp_path, name), tmp_path)
    assert result["evolve"] == selection(name)
    assert result["smoke"] == result["test"] == selection("heterogeneous-fixture")


@pytest.fixture
def domain_class(monkeypatch):
    upstream = Path(__file__).resolve().parents[1] / "rrsi"
    if not (upstream / "rrsi/domain.py").is_file():
        pytest.skip("Pinned upstream RRSI checkout is required")
    names = lambda: [name for name in sys.modules if name == "rrsi" or name.startswith("rrsi.")
                     or name == "integrations.rrsi.domain_bridge"]
    saved = {name: sys.modules.pop(name) for name in names()}
    monkeypatch.syspath_prepend(str(upstream))
    try:
        yield importlib.import_module("integrations.rrsi.domain_bridge").CodexDomain
    finally:
        for name in names():
            del sys.modules[name]
        sys.modules.update(saved)


@pytest.mark.parametrize(("name", "allowed"), [
    ("heterogeneous-fixture", False), ("fractional-fixture", True),
])
def test_rrsi_domain_guards_direct_and_existing_checkout_entry(tmp_path, score_catalog, domain_class, name, allowed):
    # Simulate loading a saved configuration without calling normalize_config.
    (tmp_path / "experiment.json").write_text(json.dumps(config(tmp_path, name)))
    if allowed:
        assert domain_class(tmp_path).evolve_ids() == ["one"]
    else:
        with pytest.raises(ValueError, match="RRSI evolve cannot use"):
            domain_class(tmp_path)
