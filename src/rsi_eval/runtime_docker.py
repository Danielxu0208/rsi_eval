"""Replay observed Docker image content IDs without running image builds."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile

from harbor.environments.docker.docker import DockerEnvironment as HarborDockerEnvironment

from .harbor_docker import DockerEnvironment
from .runtime_snapshot import load_snapshot, tree_receipts


def _relocate_compose_paths(value, original: Path, current: Path):
    """Rebase whole task-owned absolute path values, never edit frozen files."""
    if isinstance(value, dict):
        return {key: _relocate_compose_paths(item, original, current) for key, item in value.items()}
    if isinstance(value, list):
        return [_relocate_compose_paths(item, original, current) for item in value]
    if isinstance(value, str) and (value == str(original) or value.startswith(str(original) + os.sep)):
        return str(current) + value[len(str(original)):]
    return value


class FrozenDockerEnvironment(DockerEnvironment):
    def __init__(self, environment_dir, *args, runtime_snapshot: str, **kwargs):
        self._frozen_document = None
        self._frozen_lock = asyncio.Lock()
        source = Path(environment_dir).resolve()
        _, manifest = load_snapshot(runtime_snapshot, check_images=True)
        receipts = tree_receipts(source.parent)
        matches = [row for row in manifest["tasks"] if row["files"] == receipts]
        if len(matches) != 1:
            raise ValueError("Prepared task does not uniquely match the frozen runtime")
        self._frozen_services = matches[0]["services"]
        self._original_task = Path(matches[0]["original_path"])
        self._current_task = source.parent
        self._frozen_identity = manifest["sha256"]
        super().__init__(environment_dir, *args, **kwargs)
        self._env_vars.main_image_name = self._frozen_services["main"]["id"]
        self._env_vars.prebuilt_image_name = self._frozen_services["main"]["id"]
        # Harbor's prebuilt OS validation must inspect the recorded content ID,
        # including when the task originally referred to a mutable tag.
        self.task_env_config = self.task_env_config.model_copy(update={"docker_image": self._frozen_services["main"]["id"]})

    async def start(self, force_build: bool):
        if force_build:
            raise ValueError("A frozen runtime cannot be rebuilt; remove force_build")
        await super().start(force_build=False)

    async def _ensure_egress_control_sidecar_image_built(self):
        image = self._frozen_services.get("harbor-docker-egress-control-sidecar")
        if image is None:
            raise ValueError("Frozen runtime is missing its required network-control sidecar image")
        self._env_vars.egress_control_sidecar_image_name = image["id"]

    @property
    def _docker_compose_paths(self):
        if self._frozen_document is not None:
            return [self._frozen_document]
        return super()._docker_compose_paths

    async def _run_docker_compose_command(self, command, check=True, timeout_sec=None,
                                         stdin_data=None, on_output=None):
        if command and command[0] == "down":
            # The archived content IDs are reusable snapshot inputs. Harbor's
            # ordinary cleanup removes untagged images; retain these while
            # still deleting this trial's containers, volumes and networks.
            retained = []
            arguments = iter(command)
            for argument in arguments:
                if argument == "--rmi":
                    next(arguments, None)
                elif not argument.startswith("--rmi="):
                    retained.append(argument)
            command = retained
        # Normalize with Harbor's original merge/runtime environment. The build
        # graph is then removed in full, so no task Dockerfile is evaluated.
        async with self._frozen_lock:
            # This path keeps Compose warnings on stderr. Harbor's generic
            # command runner merges them into stdout and would corrupt JSON.
            document = await self._normalized_compose_config()
            document = _relocate_compose_paths(document, self._original_task, self._current_task)
            if set(document["services"]) != set(self._frozen_services):
                raise ValueError("Compose service set differs from frozen runtime")
            for name, service in document["services"].items():
                service.pop("build", None)
                service["image"] = self._frozen_services[name]["id"]
                service["pull_policy"] = "never"
            fd, filename = tempfile.mkstemp(prefix="rsi-frozen-compose-", suffix=".json")
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(document, stream)
                self._frozen_document = Path(filename)
                return await HarborDockerEnvironment._run_docker_compose_command(
                    self, command, check=check, timeout_sec=timeout_sec,
                    stdin_data=stdin_data, on_output=on_output)
            finally:
                self._frozen_document = None
                Path(filename).unlink(missing_ok=True)
