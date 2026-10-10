"""Exercise explicit runtime configuration and preflight failure boundaries."""
import json
from pathlib import Path

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, check, evaluate, preview
from rsi_eval.benchmarks import resolve
from rsi_eval.config import load_config
from rsi_eval import materializers
from rsi_eval.materializers import mgsm


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "candidate"
    path.mkdir()
    (path / "agent.py").write_text("VALUE = 1\n")
    return TargetSpec(path, "fixture:Agent")


@pytest.fixture
def selection():
    return BenchmarkSelection("mgsm", "1.0-agent", ("en-001",))


def test_config_resolves_explicit_asset_paths_and_preserves_runtime_in_preview(tmp_path, target, selection):
    config = tmp_path / "run.json"
    config.write_text(json.dumps({
        "target": {"artifact_dir": "candidate", "entrypoint": target.entrypoint},
        "benchmark": {"name": selection.name, "version": selection.version, "task_ids": ["en-001"]},
        "options": {"benchmark_runtime": {"assets_dir": "assets", "runtime_image": "image@sha256:fixture",
            "task_overrides": {"selected/task": {"assets_dir": "other-assets"}}}},
    }))
    loaded_target, loaded_selection, options, output = load_config(config)
    expected = {"assets_dir": str(tmp_path / "assets"), "runtime_image": "image@sha256:fixture",
                "task_overrides": {"selected/task": {"assets_dir": str(tmp_path / "other-assets")}}}
    assert options.benchmark_runtime == expected
    rendered = preview(target=loaded_target, benchmark=loaded_selection, options=options, output_dir=output)
    assert rendered["options"]["benchmark_runtime"] == expected
    assert not output.exists()


def test_preflight_failure_is_reported_before_source_downloads_or_model_calls(tmp_path, target, selection, monkeypatch):
    runtime = {"assets_dir": str(tmp_path / "missing")}

    def requirements(spec):
        assert spec["runtime_options"] == runtime
        return {"assets": {"ok": False, "detail": "required simulator assets are missing"}}

    monkeypatch.setattr(mgsm, "preflight", requirements, raising=False)
    monkeypatch.setattr(mgsm, "prepare", lambda *args: pytest.fail("download/prepare must not run"))
    monkeypatch.setattr("rsi_eval.evaluation.subprocess.Popen", lambda *args, **kwargs: pytest.fail("model/backend job must not run"))
    monkeypatch.setattr("rsi_eval.evaluation._probe", lambda *args, **kwargs: {"ok": True, "detail": "harbor 0.18.0"})
    options = RunOptions(benchmark_runtime=runtime)
    diagnostic = check(target=target, benchmark=selection, options=options)
    assert not diagnostic["ok"]
    assert diagnostic["checks"]["benchmark.assets"]["detail"] == "required simulator assets are missing"
    result = evaluate(target=target, benchmark=selection, options=options, output_dir=tmp_path / "run")
    assert result.status == "failed"
    assert result.trials[0].status == "infra_error"
    assert result.trials[0].reward is None
    assert "simulator assets" in result.error
    assert not (tmp_path / "run/command.json").exists()
    assert result.resolved["options"]["benchmark_runtime"] == runtime
    assert not result.resolved["benchmark_checks"]["assets"]["ok"]


def test_materializer_receives_runtime_without_mutating_the_pinned_catalog(tmp_path, selection, monkeypatch):
    spec = resolve(selection)
    original = json.dumps(spec, sort_keys=True)
    runtime = {"runtime_image": "fixture:latest"}
    monkeypatch.setattr(mgsm, "preflight", lambda spec: {}, raising=False)

    def prepare(configured, output):
        assert configured["runtime_options"] == runtime
        configured["runtime_options"]["runtime_image"] = "changed-only-in-copy"
        task = output / "task"
        for name in ("task.toml", "instruction.md", "environment/Dockerfile", "tests/test.sh"):
            path = task / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n")
        return [{"id": "en-001", "path": str(task)}]

    monkeypatch.setattr(mgsm, "prepare", prepare)
    tasks = materializers.prepare(spec, tmp_path / "tasks", runtime_options=runtime)
    assert len(tasks[0]["sha256"]) == 64
    assert json.dumps(spec, sort_keys=True) == original
    assert runtime["runtime_image"] == "fixture:latest"


def test_runtime_options_cannot_be_silently_ignored(selection):
    with pytest.raises(ValueError, match="does not support benchmark_runtime"):
        materializers.preflight(resolve(selection), {"runtime_image": "some-image"})


def test_invalid_preflight_diagnostics_still_save_a_failure_report(target, selection, tmp_path, monkeypatch):
    monkeypatch.setattr(mgsm, "preflight", lambda spec: {
        "assets": {"ok": False, "detail": float("nan")}}, raising=False)
    monkeypatch.setattr("rsi_eval.evaluation._probe", lambda *args, **kwargs: {"ok": True, "detail": "harbor 0.18.0"})
    monkeypatch.setattr(mgsm, "prepare", lambda *args: pytest.fail("preparation must not run"))
    result = evaluate(target=target, benchmark=selection, output_dir=tmp_path / "run")
    assert result.status == "failed"
    assert result.trials[0].status == "infra_error"
    assert "finite JSON" in result.error
    assert json.loads(Path(result.report_path).read_text())["trials"][0]["reward"] is None


@pytest.mark.parametrize("runtime", [None, [], {"timeout": float("nan")}, {"path": Path("somewhere")}])
def test_runtime_must_be_a_json_object(target, selection, tmp_path, runtime):
    with pytest.raises(ValueError, match="benchmark_runtime"):
        preview(target=target, benchmark=selection, options=RunOptions(benchmark_runtime=runtime),
                output_dir=tmp_path / "run")
