"""Rewrite only build inputs in a fully normalized Docker Compose document.

Compose itself owns merging, interpolation and runtime path resolution. This
module receives that result, never parses or merges task-authored YAML.
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
import shutil


def isolate_compose_builds(document: dict, project_dir: Path, destination: Path,
                           *, hash_context) -> tuple[dict, list[dict]]:
    result = copy.deepcopy(document)
    project_dir, destination = Path(project_dir).resolve(), Path(destination).resolve()
    records: list[dict] = []
    copied: dict[Path, Path] = {}

    def local_path(value: str, *, base: Path, role: str) -> Path:
        if not isinstance(value, str) or not value or "://" in value or value.startswith(("git@", "github.com/", "~")):
            raise ValueError(f"Unsupported {role}: expected an explicit local path")
        return (base / value).resolve()

    def isolate(value: str, *, service: str, role: str) -> Path:
        source = local_path(value, base=project_dir, role=role)
        if destination == source or destination.is_relative_to(source):
            raise ValueError("isolated Compose context must be outside its source")
        digest = hash_context(source)
        target = destination / ("context-" + digest)
        if source in copied and copied[source] != target:
            raise ValueError("Compose build context changed within one configuration")
        if source not in copied:
            destination.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy2)
            if hash_context(target) != digest or hash_context(source) != digest:
                raise ValueError("Compose build context changed while being copied")
            copied[source] = target
        records.append({"service": service, "role": role, "source_dir": str(source),
                        "isolated_dir": str(target), "sha256": digest})
        return target

    for service, definition in result.get("services", {}).items():
        build = definition.get("build")
        if build is None:
            continue
        if not isinstance(build, dict):
            raise ValueError("Compose build definition was not normalized")
        original = local_path(build.get("context", "."), base=project_dir, role="build context")
        target = isolate(str(original), service=service, role="context")
        build["context"] = str(target)
        if "dockerfile_inline" not in build:
            dockerfile = local_path(build.get("dockerfile", "Dockerfile"), base=original, role="Dockerfile")
            if not dockerfile.is_file() or dockerfile.is_symlink():
                raise ValueError("Dockerfile must be a regular file")
            if dockerfile.is_relative_to(original):
                build["dockerfile"] = str(target / dockerfile.relative_to(original))
            else:
                # An explicitly declared external Dockerfile does not grant
                # permission to copy its entire parent directory.
                files = [dockerfile]
                ignore = dockerfile.with_name(dockerfile.name + ".dockerignore")
                if ignore.exists():
                    if not ignore.is_file() or ignore.is_symlink():
                        raise ValueError("Dockerfile-specific ignore must be a regular file")
                    files.append(ignore)
                def file_identity(paths):
                    identity = hashlib.sha256()
                    for path in paths:
                        identity.update(path.name.encode() + b"\0" + str(path.stat().st_mode).encode()
                                        + b"\0" + path.read_bytes())
                    return identity.hexdigest()
                digest = file_identity(files)
                folder = destination / ("dockerfile-" + digest)
                folder.mkdir(parents=True, exist_ok=True)
                for path in files:
                    shutil.copy2(path, folder / path.name)
                if file_identity(files) != digest or file_identity([folder / p.name for p in files]) != digest:
                    raise ValueError("External Dockerfile changed while being copied")
                build["dockerfile"] = str(folder / dockerfile.name)
                records.append({"service": service, "role": "external_dockerfile",
                                "source_file": str(dockerfile), "isolated_file": build["dockerfile"],
                                "sha256": digest, "copied_files": [p.name for p in files]})
        additional = build.get("additional_contexts", {})
        if isinstance(additional, list):
            try:
                additional = dict(entry.split("=", 1) for entry in additional)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid additional build context") from exc
            build["additional_contexts"] = additional
        if not isinstance(additional, dict):
            raise ValueError("Additional build contexts were not normalized")
        for name, value in list(additional.items()):
            if isinstance(value, str) and value.startswith("service:"):
                if value.removeprefix("service:") not in result.get("services", {}):
                    raise ValueError("Additional build context references a missing service")
                records.append({"service": service, "role": "additional:" + name,
                                "reference": value, "kind": "compose_service"})
            elif isinstance(value, str) and value.startswith("docker-image:"):
                if not re.search(r"@sha256:[0-9a-f]{64}$", value):
                    raise ValueError("Additional image contexts must use an immutable digest")
                records.append({"service": service, "role": "additional:" + name,
                                "reference": value, "kind": "pinned_image"})
            else:
                additional[name] = str(isolate(value, service=service, role="additional:" + name))
    return result, records
