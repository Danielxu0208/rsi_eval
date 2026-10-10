"""Runtime replay must use observed image identities and immutable task bytes."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec
from rsi_eval.artifacts import artifact_hash
from rsi_eval.benchmarks import list_benchmarks, resolve
from rsi_eval.evaluation import preview, _job_config
from rsi_eval import runtime_snapshot as runtime

IMAGE = {"id": "sha256:" + "a" * 64, "architecture": "arm64", "os": "linux", "repo_digests": []}


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    spec = next(s for s in list_benchmarks() if s["name"] == "mgsm")
    selection = BenchmarkSelection(spec["name"], spec["version"], (spec["tasks"][0]["id"],))
    spec = resolve(selection)
    root = tmp_path / "snapshot"
    task = root / "tasks/000000"
    (task / "environment").mkdir(parents=True)
    (task / "environment/Dockerfile").write_text("FROM mutable:latest\nRUN echo build\n")
    (task / "asset.dat").write_bytes(b"immutable external resource\0")
    (task / "task.toml").write_text('[environment]\ncpus = 1\n')
    (task / "instruction.md").write_text("fixture task")
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text("true\n")
    row = {"id": spec["task_ids"][0], "directory": "tasks/000000", "original_path": "/old/task",
           "files": runtime.tree_receipts(task), "services": {"main": IMAGE}}
    value = {"format": runtime.FORMAT, "benchmark": spec, "benchmark_runtime": {},
             "tasks": [row], "image_archive": None}
    value["sha256"] = runtime.digest(value)
    (root / "runtime-snapshot.json").write_text(json.dumps(value))
    monkeypatch.setattr(runtime, "inspect_image", lambda ref: IMAGE)
    return root, selection, spec, value


def test_replay_checks_files_images_and_uses_frozen_backend(frozen, tmp_path):
    root, selection, spec, value = frozen
    tasks = runtime.prepare_snapshot(str(root), spec, tmp_path / "prepared")
    assert tasks[0]["sha256"] == artifact_hash(root / "tasks/000000")
    target = tmp_path / "candidate"
    target.mkdir()
    (target / "agent.py").write_text("pass\n")
    result = preview(target=TargetSpec(target, "fixture:Agent"), benchmark=selection,
                     options=RunOptions(runtime_snapshot=str(root)), output_dir=tmp_path / "run")
    result["execution_tasks"] = tasks
    config = _job_config(result, tmp_path / "run/candidate")
    assert result["runtime_snapshot_sha256"] == value["sha256"]
    assert config["environment"]["import_path"] == "rsi_eval.runtime_docker:FrozenDockerEnvironment"
    assert config["environment"]["kwargs"]["runtime_snapshot"] == str(root)
    assert config["tasks"][0]["path"] == tasks[0]["path"]


@pytest.mark.parametrize("mutation", ["asset", "mode", "directory", "manifest", "task-id", "image", "protocol"])
def test_changed_identity_is_rejected(frozen, monkeypatch, mutation):
    root, _, spec, value = frozen
    if mutation == "asset":
        (root / "tasks/000000/asset.dat").write_bytes(b"changed")
    elif mutation == "mode":
        (root / "tasks/000000/asset.dat").chmod(0o755)
    elif mutation == "directory":
        (root / "tasks/000000/empty-extra").mkdir()
    elif mutation == "manifest":
        value["tasks"][0]["services"]["main"]["id"] = "sha256:" + "b" * 64
        (root / "runtime-snapshot.json").write_text(json.dumps(value))
    elif mutation == "task-id":
        spec = deepcopy(spec)
        spec["tasks"][0]["id"] = "another-task"
    elif mutation == "protocol":
        spec = deepcopy(spec)
        spec["protocol"] = "different-grading-protocol"
    else:
        monkeypatch.setattr(runtime, "inspect_image", lambda ref: {**IMAGE, "architecture": "amd64"})
    with pytest.raises(ValueError):
        runtime.load_snapshot(root, spec=spec, check_images=True)


def test_symlink_and_credentials_are_not_snapshotted(frozen, tmp_path):
    root, _, spec, _ = frozen
    file = root / "tasks/000000/auth.json"
    file.write_text("test sentinel")
    with pytest.raises(ValueError, match="credential"):
        runtime.load_snapshot(root)
    file.unlink()
    file.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        runtime.load_snapshot(root)


def test_restore_verifies_archive_before_docker_load(frozen, monkeypatch):
    root, _, _, value = frozen
    archive = root / "images.tar"
    archive.write_bytes(b"fixture docker archive")
    value["image_archive"] = {"file": "images.tar", "sha256": runtime.file_hash(archive)}
    value.pop("sha256")
    value["sha256"] = runtime.digest(value)
    (root / "runtime-snapshot.json").write_text(json.dumps(value))
    calls = []
    monkeypatch.setattr(runtime, "_docker", lambda *args: calls.append(args))
    assert runtime.restore_images(root)["images_restored"]
    assert calls == [("image", "load", "--input", str(archive))]
    archive.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        runtime.restore_images(root)
    assert len(calls) == 1


def test_preview_never_inspects_or_loads_images(frozen, monkeypatch, tmp_path):
    root, selection, _, _ = frozen
    monkeypatch.setattr(runtime, "_docker", lambda *args: pytest.fail("offline preview called Docker"))
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "agent.py").write_text("pass")
    preview(target=TargetSpec(source, "fixture:Agent"), benchmark=selection,
            options=RunOptions(runtime_snapshot=str(root)), output_dir=tmp_path / "out")


def test_capture_requires_contemporaneous_image_record(frozen, tmp_path, monkeypatch):
    from rsi_eval.types import EvaluationResult, TrialResult
    root, _, spec, _ = frozen
    run = tmp_path / "run"
    run.mkdir()
    resolved = {"benchmark": spec, "execution_tasks": [{"id": spec["task_ids"][0],
        "path": str(root / "tasks/000000"), "sha256": artifact_hash(root / "tasks/000000")}], "options": {}}
    trial = TrialResult(spec["task_ids"][0], 0, "completed", artifact_dir=str(run / "native/eval/trial"))
    result = EvaluationResult("completed", [trial], {}, {}, str(run), resolved)
    import importlib
    monkeypatch.setattr(importlib.import_module("rsi_eval.resume"), "_read_run", lambda path: (resolved, result))
    with pytest.raises(ValueError, match="historical tags are not reconstructed"):
        runtime.capture(run, tmp_path / "export")
    assert not (tmp_path / "export").exists()
    evidence = Path(trial.artifact_dir) / "build-context/runtime-images.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({"format": runtime.FORMAT, "services": {"main": IMAGE}}))
    report = runtime.capture(run, tmp_path / "export", save_images=False)
    assert report["images"] == 1 and report["model_calls"] == 0
    runtime.load_snapshot(tmp_path / "export", spec=spec, check_images=True)


def test_frozen_preflight_retains_judge_requirements(monkeypatch):
    monkeypatch.delenv("JUDGE_API_KEY", raising=False)
    spec = next(s for s in list_benchmarks() if s["name"] == "jobbench")
    checks = runtime.preflight(spec, {})
    assert not checks["judge_credentials"]["ok"]


def test_capture_rejects_output_inside_run_through_parent_symlink(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(run, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the evaluation"):
        runtime.capture(run, alias / "snapshot")
    assert not (run / "snapshot").exists()


def test_capture_rejects_prepared_asset_changed_after_evaluation(tmp_path, monkeypatch):
    run = tmp_path / "run"
    candidate = run / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "agent.py").write_text("pass\n")
    task = run / "prepared/task"
    task.mkdir(parents=True)
    asset = task / "asset.dat"
    asset.write_bytes(b"evaluated asset")
    resolved = {"output_dir": str(run), "target": {"sha256": artifact_hash(candidate)},
                "execution_tasks": [{"id": "test/task", "path": str(task),
                                     "sha256": artifact_hash(task)}]}
    (run / "resolved.json").write_text(json.dumps(resolved))
    asset.write_bytes(b"changed after evaluation")
    monkeypatch.setattr(runtime, "_docker", lambda *args: pytest.fail("changed task reached Docker"))
    with pytest.raises(ValueError, match="Prepared task changed"):
        runtime.capture(run, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_rebinding_frozen_asset_and_credential_locations_does_not_change_protocol():
    before = {"assets_dir": "/old/assets", "judge_env_file": "/old/judge", "gpu": True,
              "task_overrides": {"x": {"assets_dir": "/old/extra", "cpus": 2}}}
    after = {"assets_dir": "/new/assets", "judge_env_file": "/new/judge", "gpu": True,
             "task_overrides": {"x": {"assets_dir": "/new/extra", "cpus": 2}}}
    assert runtime.runtime_settings_match(before, after)
    after["gpu"] = False
    assert not runtime.runtime_settings_match(before, after)
