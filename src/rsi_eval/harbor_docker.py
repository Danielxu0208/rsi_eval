"""Isolate Docker build contexts while retaining Harbor's task execution."""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from harbor.environments.docker.docker import DockerEnvironment as HarborDockerEnvironment
from harbor.environments.docker.docker import _sanitize_docker_compose_project_name


GUARD_VERSION = "rsi-context-v2"


def _custom_contexts(source: Path, extra_paths=()) -> list[dict]:
    paths = [Path(p) for p in extra_paths or ()]
    compose = source / "docker-compose.yaml"
    if compose.exists() or compose.is_symlink():
        paths.insert(0, compose)
    # Normalize the full merge at execution time, after Harbor has generated its
    # mount/resource layers and chosen prebuilt versus local build behavior.
    return [{"compose_file": str(path)} for path in paths]


def _write_provenance(trial_dir: Path, provenance: dict) -> None:
    parent = Path(trial_dir).resolve() / "build-context"
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")


def _entries(root: Path):
    """Walk every entry, including hidden files, without following symlinks."""
    for path in sorted(root.iterdir()):
        mode = path.lstat().st_mode
        yield path, mode
        if stat.S_ISDIR(mode):
            yield from _entries(path)


def context_hash(directory: Path) -> str:
    """Hash paths, file bytes, modes and link targets, excluding timestamps."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError(f"Docker build context is not a directory: {root}")
    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    add(stat.S_IMODE(root.stat().st_mode).to_bytes(4, "big"))
    for path, mode in _entries(root):
        add(path.relative_to(root).as_posix().encode())
        add(mode.to_bytes(4, "big"))
        if stat.S_ISLNK(mode):
            target = os.readlink(path)
            if Path(target).is_absolute():
                raise ValueError(f"absolute context symlink cannot be relocated: {path.relative_to(root)}")
            try:
                contained = path.resolve().is_relative_to(root)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"invalid context symlink: {path.relative_to(root)}") from exc
            if not contained:
                raise ValueError(f"context symlink escapes its root: {path.relative_to(root)}")
            add(os.fsencode(target))
        elif stat.S_ISREG(mode):
            size = path.stat().st_size
            digest.update(size.to_bytes(8, "big"))
            count = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    count += len(chunk)
            if count != size:
                raise ValueError(f"context file changed while hashing: {path.relative_to(root)}")
        elif stat.S_ISDIR(mode):
            add(b"")
        else:
            raise ValueError(f"unsupported context file type: {path.relative_to(root)}")
    return digest.hexdigest()


def isolate_context(source: Path, trial_dir: Path) -> tuple[Path, dict]:
    source = Path(source).resolve()
    parent = Path(trial_dir).resolve() / "build-context"
    if parent == source or parent.is_relative_to(source):
        raise ValueError("isolated context must be outside its source")
    original_hash = context_hash(source)
    destination = parent / f"environment-{original_hash}"
    parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    if context_hash(destination) != original_hash or context_hash(source) != original_hash:
        raise ValueError("Docker build context changed while being copied")
    provenance = {
        "guard_version": GUARD_VERSION, "applied": True,
        "source_dir": str(source),
        "isolated_dir": str(destination),
        "sha256": original_hash,
        "preserved": ["file_bytes", "modes", "mtimes", "symlink_targets"],
    }
    _write_provenance(trial_dir, provenance)
    return destination, provenance


class DockerEnvironment(HarborDockerEnvironment):
    """Use a content-specific context basename for BuildKit local sources."""

    async def start(self, force_build: bool):
        await super().start(force_build)
        from .runtime_snapshot import record_images
        await record_images(self)

    def __init__(self, environment_dir, environment_name, session_id, trial_paths,
                 task_env_config, *args, retain_runtime_images=False, **kwargs):
        self._retain_runtime_images = retain_runtime_images
        source = Path(environment_dir).resolve()
        custom = _custom_contexts(source, kwargs.get("extra_docker_compose"))
        self._guard_custom_compose = bool(custom)
        self._guard_document_path = None
        self._guard_compose_lock = asyncio.Lock()
        self._guard_trial_dir = Path(trial_paths.trial_dir)
        if custom:
            isolated = source
            self.context_provenance = {
                "guard_version": GUARD_VERSION, "applied": False,
                "source_dir": str(source), "isolated_dir": None, "sha256": None,
                "status": "pending_compose_normalization", "compose_files": custom,
            }
            _write_provenance(trial_paths.trial_dir, self.context_provenance)
        elif task_env_config.docker_image and not source.exists():
            isolated = source
            self.context_provenance = {
                "guard_version": GUARD_VERSION, "applied": False, "source_dir": str(source),
                "isolated_dir": None, "sha256": None,
                "skipped": "prebuilt_image_without_local_context",
                "docker_image": task_env_config.docker_image,
            }
            _write_provenance(trial_paths.trial_dir, self.context_provenance)
        else:
            isolated, self.context_provenance = isolate_context(source, trial_paths.trial_dir)
        super().__init__(environment_dir=isolated, environment_name=environment_name,
                         session_id=session_id, trial_paths=trial_paths,
                         task_env_config=task_env_config, *args, **kwargs)
        # Do not reuse an image tag created before context isolation was enabled.
        if self.context_provenance["applied"] or self._guard_custom_compose:
            self._env_vars.main_image_name += f"-{GUARD_VERSION}"

    @property
    def _docker_compose_paths(self):
        if self._guard_document_path is not None:
            return [self._guard_document_path]
        return super()._docker_compose_paths

    async def _normalized_compose_config(self):
        command = ["docker", "compose", "--project-name",
                   _sanitize_docker_compose_project_name(self.session_id),
                   "--project-directory", str(self.environment_dir.resolve())]
        for path in super()._docker_compose_paths:
            command.extend(["-f", str(path.resolve())])
        command.extend(["config", "--format", "json", "--no-env-resolution"])
        # Compose warnings go to stderr. Harbor normally combines the streams,
        # which would make otherwise valid JSON unreadable for older task YAML.
        process = await asyncio.create_subprocess_exec(*command,
            env=self._compose_env_vars(include_os_env=True), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        except BaseException:
            await self._terminate_process(process)
            raise
        if process.returncode:
            # Never include the resolved document: it can contain credentials.
            raise RuntimeError("Docker Compose normalization failed: " + stderr.decode(errors="replace")[-3000:])
        return json.loads(stdout)

    async def _run_docker_compose_command(self, command, check=True, timeout_sec=None,
                                          stdin_data=None, on_output=None):
        if not self._guard_custom_compose:
            return await super()._run_docker_compose_command(
                command, check=check, timeout_sec=timeout_sec,
                stdin_data=stdin_data, on_output=on_output)
        from .compose_context import isolate_compose_builds

        async with self._guard_compose_lock:
            # Docker Compose supplies exact merge, interpolation, path and
            # env_file semantics. Do not resolve env_file values into reports.
            normalized = await self._normalized_compose_config()
            document, contexts = isolate_compose_builds(
                normalized, self.environment_dir,
                self._guard_trial_dir / "build-context" / "compose",
                hash_context=context_hash)
            self.context_provenance = {
                "guard_version": GUARD_VERSION, "applied": True,
                "status": "isolated" if contexts else "no_local_build_contexts",
                "source_dir": str(self.environment_dir), "contexts": contexts,
                "runtime_paths": "preserved_from_docker_compose_config",
                "compose_files": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                                  for p in super()._docker_compose_paths],
            }
            _write_provenance(self._guard_trial_dir, self.context_provenance)
            # Interpolated service environment may contain private values.
            # Keep it in a mode-0600 temporary file only during this invocation;
            # never write it into trial artifacts or provenance.
            fd, filename = tempfile.mkstemp(prefix="rsi-compose-", suffix=".json")
            try:
                with os.fdopen(fd, "w") as handle:
                    # Compose config already escapes literal dollars for a
                    # round trip through Compose; escaping again changes data.
                    json.dump(document, handle)
                self._guard_document_path = Path(filename)
                return await super()._run_docker_compose_command(
                    command, check=check, timeout_sec=timeout_sec,
                    stdin_data=stdin_data, on_output=on_output)
            finally:
                self._guard_document_path = None
                Path(filename).unlink(missing_ok=True)


Environment = DockerEnvironment
