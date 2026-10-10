"""Frozen Compose routing with real Harbor objects and deterministic processes."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor.environments.docker.docker")

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from rsi_eval import runtime_docker
from rsi_eval.harbor_docker import DockerEnvironment
from rsi_eval.runtime_snapshot import tree_receipts

MAIN = "sha256:" + "a" * 64
SIDECAR = "sha256:" + "b" * 64


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    task = tmp_path / "new/task"
    source = task / "environment"
    source.mkdir(parents=True)
    (source / "Dockerfile").write_text("FROM mutable:tag\nRUN false\n")
    config = EnvironmentConfig(docker_image="mutable:tag")
    original = config.model_dump()
    manifest = {"sha256": "fixture", "tasks": [{"id": "task", "original_path": "/old/task",
                "files": tree_receipts(task), "services": {
                    "main": {"id": MAIN}, "harbor-docker-egress-control-sidecar": {"id": SIDECAR}}}]}
    monkeypatch.setattr(runtime_docker, "load_snapshot", lambda *args, **kwargs: (tmp_path, manifest))
    environment = runtime_docker.FrozenDockerEnvironment(source, "fixture", "fixture-session",
                  TrialPaths(tmp_path / "trial"), config, runtime_snapshot="snapshot")
    assert config.model_dump() == original
    return environment


def test_frozen_compose_rebases_bindings_removes_all_builds_and_never_pulls(frozen, monkeypatch):
    original = {"services": {
        "main": {"build": {"context": "/old/task/environment"}, "image": "mutable:tag",
                 "pull_policy": "always", "volumes": [
                     {"type": "bind", "source": "/old/task/assets/data", "target": "/data", "read_only": True},
                     {"type": "bind", "source": "/current-trial/logs", "target": "/logs"}],
                 "env_file": [{"path": "/old/task/runtime.env", "required": True}],
                 "environment": {"LITERAL": "$$value", "UNRELATED": "/old/task-other"}},
        "harbor-docker-egress-control-sidecar": {"build": {"context": "/anything"}, "image": "sidecar:latest"}},
        "configs": {"data": {"file": "/old/task/assets/config"}}}
    calls = []
    async def normalize():
        return deepcopy(original)
    async def execute(self, command, **kwargs):
        assert command != ["config", "--format", "json", "--no-env-resolution"]
        filename = self._frozen_document
        assert filename.stat().st_mode & 0o777 == 0o600
        document = json.loads(filename.read_text())
        calls.append((command, document, filename))
        return SimpleNamespace(return_code=0, stdout="ok")
    monkeypatch.setattr(frozen, "_normalized_compose_config", normalize)
    monkeypatch.setattr(runtime_docker.HarborDockerEnvironment, "_run_docker_compose_command", execute)
    result = asyncio.run(frozen._run_docker_compose_command(["up", "--detach", "--wait"]))
    assert result.return_code == 0 and len(calls) == 1
    _, document, temporary = calls[0]
    assert not temporary.exists() and frozen._frozen_document is None
    for name, service in document["services"].items():
        assert "build" not in service and service["pull_policy"] == "never"
        assert service["image"] == frozen._frozen_services[name]["id"]
    main = document["services"]["main"]
    assert main["volumes"][0]["source"] == str(frozen._current_task / "assets/data")
    assert main["volumes"][1]["source"] == "/current-trial/logs"
    assert main["env_file"][0]["path"] == str(frozen._current_task / "runtime.env")
    assert main["environment"] == original["services"]["main"]["environment"]
    assert document["configs"]["data"]["file"] == str(frozen._current_task / "assets/config")
    assert original["services"]["main"]["image"] == "mutable:tag"


def test_frozen_sidecar_uses_observed_image_without_building(frozen, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Frozen network policy must not build a new sidecar")
    monkeypatch.setattr(runtime_docker.HarborDockerEnvironment, "_ensure_egress_control_sidecar_image_built", forbidden)
    asyncio.run(frozen._ensure_egress_control_sidecar_image_built())
    assert frozen._env_vars.egress_control_sidecar_image_name == SIDECAR
    assert frozen.task_env_config.docker_image == MAIN
    assert frozen._env_vars.prebuilt_image_name == MAIN
    del frozen._frozen_services["harbor-docker-egress-control-sidecar"]
    with pytest.raises(ValueError, match="network-control"):
        asyncio.run(frozen._ensure_egress_control_sidecar_image_built())
    with pytest.raises(ValueError, match="cannot be rebuilt"):
        asyncio.run(frozen.start(force_build=True))


def test_compose_warning_is_not_parsed_as_json(frozen, monkeypatch):
    # Exercise the shared normalization implementation, not a mocked parser.
    class Process:
        returncode = 0
        async def communicate(self):
            return b'{"services":{}}', b'warning: the attribute `version` is obsolete\n'
    captured = {}
    async def spawn(*command, **kwargs):
        captured.update(command=command, **kwargs)
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(DockerEnvironment._normalized_compose_config(frozen))
    assert result == {"services": {}}
    assert captured["stderr"] == asyncio.subprocess.PIPE
    assert "--no-env-resolution" in captured["command"]


def test_frozen_service_set_change_rejected_before_launch(frozen, monkeypatch):
    async def normalize():
        return {"services": {"main": {"image": MAIN}}}
    async def forbidden(*args, **kwargs):
        pytest.fail("Changed service set must never be started")
    monkeypatch.setattr(frozen, "_normalized_compose_config", normalize)
    monkeypatch.setattr(runtime_docker.HarborDockerEnvironment, "_run_docker_compose_command", forbidden)
    with pytest.raises(ValueError, match="service set"):
        asyncio.run(frozen._run_docker_compose_command(["up"]))


@pytest.mark.parametrize("remove", [["--rmi", "local"], ["--rmi=all"]])
def test_frozen_cleanup_retains_snapshot_images_but_removes_trial_resources(frozen, monkeypatch, remove):
    async def normalize():
        return {"services": {name: {"image": row["id"]} for name, row in frozen._frozen_services.items()}}
    calls = []
    async def execute(self, command, **kwargs):
        calls.append(command)
        return SimpleNamespace(return_code=0, stdout="")
    monkeypatch.setattr(frozen, "_normalized_compose_config", normalize)
    monkeypatch.setattr(runtime_docker.HarborDockerEnvironment, "_run_docker_compose_command", execute)
    asyncio.run(frozen._run_docker_compose_command(["down", *remove, "--volumes", "--remove-orphans"]))
    assert calls == [["down", "--volumes", "--remove-orphans"]]


def test_sdk_job_preserves_frozen_kwargs_through_real_harbor_factory(frozen, tmp_path):
    from harbor.environments.factory import EnvironmentFactory
    from harbor.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
    from rsi_eval.evaluation import _job_config
    resolved = {"target": {"entrypoint": "oracle", "config": {}, "model": None},
                "options": {"environment": "docker", "runtime_snapshot": "snapshot", "repetitions": 1, "concurrency": 1},
                "benchmark": {"tasks": []}, "output_dir": str(tmp_path / "job")}
    value = _job_config(resolved, tmp_path / "candidate")
    config = TrialEnvironmentConfig.model_validate(value["environment"])
    assert config.kwargs == {"runtime_snapshot": "snapshot"}
    actual = EnvironmentFactory.create_environment_from_config(config,
             environment_dir=frozen._current_task / "environment", environment_name="factory-test",
             session_id="factory-session", trial_paths=TrialPaths(tmp_path / "factory-trial"),
             task_env_config=EnvironmentConfig())
    assert isinstance(actual, runtime_docker.FrozenDockerEnvironment)
    assert actual._env_vars.main_image_name == MAIN
