"""Registry and Git tasks keep their native identity through explicit capture."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate
from rsi_eval import benchmarks, runtime_snapshot as runtime
from rsi_eval.evaluation import check

IMAGE = {"id": "sha256:" + "a" * 64, "architecture": "arm64", "os": "linux", "repo_digests": []}


@pytest.fixture(params=["package", "git"])
def native_run(tmp_path, monkeypatch, request):
    task = tmp_path / "harbor-cache/task"
    (task / "environment").mkdir(parents=True)
    (task / "environment/Dockerfile").write_text("FROM mutable:latest\n")
    native_name = "fixture/task" if request.param == "package" else "task"
    (task / "task.toml").write_text(f"version = '1.0'\n[task]\nname = '{native_name}'\n")
    (task / "instruction.md").write_text("Preserve the original task.\n")
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text("echo original-verifier\n")
    identity = ({"name": "fixture/task", "ref": "sha256:" + "b" * 64}
                if request.param == "package" else
                {"git_url": "https://example.invalid/tasks.git", "git_commit_id": "c" * 40, "path": "tasks/task"})
    spec = {"name": "runtime-fixture", "version": request.param, "dataset_ref": "fixed-fixture",
            "tasks": [{"id": "original-task-id", **identity}]}
    monkeypatch.setitem(benchmarks._EXTRA, (spec["name"], spec["version"]), spec)
    monkeypatch.setattr(runtime, "inspect_image", lambda ref: IMAGE)
    runner = tmp_path / "fake-harbor"
    runner.write_text(f"#!{sys.executable}\n" + f"SOURCE = {str(task)!r}\nIMAGE = {IMAGE!r}\n" + '''
import json, sys, tomllib
from pathlib import Path
from rsi_eval.runtime_snapshot import task_evidence, FORMAT
if '--version' in sys.argv:
    print('harbor 0.18.0 fixture'); raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index('--config') + 1]).read_text())
job = Path(config['jobs_dir']) / config['job_name']
job.mkdir(parents=True)
for task in config['tasks']:
    source = Path(SOURCE) if 'name' in task or 'git_url' in task else Path(task['path'])
    assert (source / 'tests/test.sh').read_text() == 'echo original-verifier\\n'
    if 'name' in task:
        org, name = task['name'].split('/', 1)
        identity = {'org': org, 'name': name, 'ref': task['ref']}
        task_name = task['name']
    else:
        identity = task
        task_name = Path(task['path']).name
    task_config = tomllib.loads((source / 'task.toml').read_text())
    if 'task' in task_config:
        task_name = task_config['task']['name']
    for attempt in range(config['n_attempts']):
        trial = job / f'task__{attempt}'
        (trial / 'build-context').mkdir(parents=True)
        row = {'task_name': task_name, 'task_id': identity,
               'config': {'agent': config['agents'][0]}, 'finished_at': 'today',
               'verifier_result': {'rewards': {'reward': 1}}}
        (trial / 'result.json').write_text(json.dumps(row))
        (trial / 'build-context/runtime-images.json').write_text(json.dumps({
            'format': FORMAT, 'task': task_evidence(source), 'services': {'main': IMAGE}, 'external_inputs': []}))
(job / 'result.json').write_text(json.dumps({'finished_at': 'today', 'n_total_trials': len(config['tasks']) * config['n_attempts']}))
''')
    runner.chmod(0o700)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "README.md").write_text("Oracle fixture; no model calls.\n")
    target = TargetSpec(candidate, "oracle")
    selection = BenchmarkSelection(spec["name"], spec["version"])
    options = RunOptions(harbor_executable=str(runner), repetitions=2)
    result = evaluate(target=target, benchmark=selection, options=options, output_dir=tmp_path / "run")
    assert result.status == "completed", result.error
    assert "execution_tasks" not in result.resolved
    return target, selection, options, result, task


def test_native_capture_replay_does_not_need_original_cache(native_run, tmp_path, monkeypatch):
    target, selection, options, initial, task = native_run
    originals = {p: p.read_bytes() for p in Path(initial.artifact_dir).rglob("*.json")}
    snapshot = tmp_path / "snapshot"
    assert runtime.capture(Path(initial.resolved["output_dir"]), snapshot, save_images=False)["tasks"] == 1
    shutil.rmtree(task.parent)
    frozen_options = replace(options, runtime_snapshot=str(snapshot))
    # Preflight must not invoke a materializer for a package/Git benchmark.
    import rsi_eval.evaluation as evaluation
    monkeypatch.setattr(evaluation, "_probe", lambda command, **kwargs: {"ok": True, "detail": "harbor 0.18.0"})
    assert check(target=target, benchmark=selection, options=frozen_options)["ok"]
    replay = evaluate(target=target, benchmark=selection, options=frozen_options, output_dir=tmp_path / "replay")
    assert replay.status == "completed", replay.error
    assert [t.task_id for t in replay.trials] == ["original-task-id"] * 2
    assert [t.reward for t in replay.trials] == [1, 1]
    declared = "fixture/task" if selection.version == "package" else "task"
    assert replay.resolved["execution_tasks"][0]["native_name"] == declared
    copied_config = tmp_path / "replay/tasks/000000/task.toml"
    assert copied_config.read_bytes() == (snapshot / "tasks/000000/task.toml").read_bytes()
    config = json.loads((tmp_path / "replay/harbor.json").read_text())
    assert config["environment"]["import_path"] == "rsi_eval.runtime_docker:FrozenDockerEnvironment"
    assert config["tasks"] == [{"path": str(tmp_path / "replay/tasks/000000")}]
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_frozen_native_name_does_not_relax_exact_local_path_identity(native_run, tmp_path):
    target, selection, options, initial, _ = native_run
    snapshot = tmp_path / "snapshot"
    runtime.capture(Path(initial.resolved["output_dir"]), snapshot, save_images=False)
    replay = evaluate(target=target, benchmark=selection,
                      options=replace(options, runtime_snapshot=str(snapshot)), output_dir=tmp_path / "replay")
    assert replay.status == "completed", replay.error
    trial_path = Path(replay.trials[0].artifact_dir) / "result.json"
    trial = json.loads(trial_path.read_text())
    trial["task_id"]["path"] = str(tmp_path / "different-root/000000")
    trial_path.write_text(json.dumps(trial))
    with pytest.raises(ValueError, match="immutable task"):
        runtime.capture(tmp_path / "replay", tmp_path / "recaptured", save_images=False)


@pytest.mark.parametrize("mutation", ["missing-task-evidence", "source-bytes", "source-mode", "pinned-identity", "repeat-image", "repeat-task"])
def test_native_capture_rejects_unproven_or_changed_inputs(native_run, tmp_path, mutation):
    _, _, _, result, task = native_run
    run = Path(result.resolved["output_dir"])
    trial = Path(result.trials[-1].artifact_dir)
    path = trial / "build-context/runtime-images.json"
    evidence = json.loads(path.read_text())
    expected = ""
    if mutation == "missing-task-evidence":
        evidence.pop("task")
        expected = "contemporaneous task evidence"
    elif mutation == "source-bytes":
        (task / "instruction.md").write_text("changed after evaluation")
        expected = "task changed"
    elif mutation == "source-mode":
        (task / "tests/test.sh").chmod(0o700)
        expected = "task changed"
    elif mutation == "pinned-identity":
        native_path = trial / "result.json"
        row = json.loads(native_path.read_text())
        key = "ref" if "ref" in row["task_id"] else "git_commit_id"
        row["task_id"][key] = "different-source"
        native_path.write_text(json.dumps(row))
        expected = "immutable task"
    elif mutation == "repeat-image":
        evidence["services"]["main"]["id"] = "sha256:" + "d" * 64
        expected = "different images"
    else:
        alternate = tmp_path / "alternate-cache"
        shutil.copytree(task, alternate)
        (alternate / "instruction.md").write_text("different trial task")
        evidence["task"] = runtime.task_evidence(alternate)
        expected = "different task contents"
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match=expected):
        runtime.capture(run, tmp_path / "snapshot", save_images=False)
    assert not (tmp_path / "snapshot").exists()


def test_missing_trial_cannot_read_image_evidence_from_working_directory(native_run, tmp_path, monkeypatch):
    _, _, _, result, _ = native_run
    run = Path(result.resolved["output_dir"])
    working = tmp_path / "unrelated-working-directory"
    (working / "build-context").mkdir(parents=True)
    evidence = Path(result.trials[0].artifact_dir) / "build-context/runtime-images.json"
    shutil.copy2(evidence, working / "build-context/runtime-images.json")
    for trial in result.trials:
        (Path(trial.artifact_dir) / "result.json").unlink()
    monkeypatch.chdir(working)
    with pytest.raises(ValueError, match="Missing contemporaneous task evidence"):
        runtime.capture(run, tmp_path / "snapshot", save_images=False)
    assert not (tmp_path / "snapshot").exists()


def test_record_images_captures_original_task_before_agent_execution(tmp_path, monkeypatch):
    task = tmp_path / "cache/task"
    (task / "environment").mkdir(parents=True)
    (task / "instruction.md").write_text("original instruction")
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text("original verifier")

    async def compose(command):
        return SimpleNamespace(stdout="container-id\n")

    async def normalized():
        return {"services": {"main": {}}}

    environment = SimpleNamespace(
        _run_docker_compose_command=compose, _normalized_compose_config=normalized,
        _retain_runtime_images=False, _guard_trial_dir=tmp_path / "trial",
        context_provenance={"source_dir": str(task / "environment")})
    monkeypatch.setattr(runtime, "_docker", lambda *args: json.dumps({"service": "main", "image": IMAGE["id"]}))
    monkeypatch.setattr(runtime, "inspect_image", lambda ref: IMAGE)
    asyncio.run(runtime.record_images(environment))
    saved = json.loads((tmp_path / "trial/build-context/runtime-images.json").read_text())
    assert saved["task"] == runtime.task_evidence(task)
    assert saved["services"] == {"main": IMAGE}
    assert saved["external_inputs"] == []
