"""Freeze generated file references so Harbor executes the hashed candidate."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

TRUSTED_MODULE_ROOTS = ("harnessx.", "benchmarks.", "recipe.")


def _checked_file(path: Path, roots: tuple[Path, ...]) -> Path:
    """Check lexical ancestors before resolve can hide a symlink."""
    path = path.absolute()
    for root in roots:
        if not path.is_relative_to(root):
            continue
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Candidate dependencies cannot contain symlinks: {current}")
        resolved = path.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    raise ValueError(f"Candidate dependency is outside declared roots or missing: {path}")


def _check_target(value: str, checkout: Path | None = None) -> None:
    if value.startswith("file://"):
        return
    if not value.startswith(TRUSTED_MODULE_ROOTS):
        raise ValueError(f"Generated components must use file:// references; ambient import is not pinned: {value}")
    if checkout is not None:
        module_name, _, symbol = value.rpartition(".")
        spec = importlib.util.find_spec(module_name)
        filename = spec.origin if spec else None
        if not filename or not Path(filename).resolve().is_relative_to(checkout):
            raise ValueError(f"Runtime component is outside the pinned HarnessX checkout: {value}")
        relative = str(Path(filename).resolve().relative_to(checkout))
        tracked = subprocess.run(["git", "-C", str(checkout), "ls-files", "--error-unmatch", "--", relative],
                                 capture_output=True)
        if tracked.returncode:
            raise ValueError(f"Runtime component is not tracked in the pinned HarnessX checkout: {value}")
        module = importlib.import_module(module_name)
        if not hasattr(module, symbol):
            raise ValueError(f"Runtime component does not exist: {value}")


def freeze_config(source: Path, destination: Path, *, allowed_roots=()) -> Path:
    import yaml
    if Path(source).is_symlink():
        raise ValueError("Candidate YAML cannot be a symlink")
    source, destination = Path(source).resolve(), Path(destination).resolve()
    roots = (source.parent, *(Path(root).resolve() for root in allowed_roots))
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    files = {}

    def visit(value, key=None):
        if isinstance(value, dict):
            if key == "plugins" and "path" in value:
                raise ValueError("Plugin directories must be converted to explicit file:// components before bundling")
            return {k: visit(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v, key) for v in value]
        if not isinstance(value, str):
            return value
        if key in {"_target_", "custom"}:
            _check_target(value)
        if key == "plugins":
            raise ValueError("Plugin directories must be converted to explicit file:// components before bundling")
        path, suffix = None, ""
        if value.startswith("file://"):
            raw, separator, symbol = value[7:].partition("::")
            path, suffix = Path(raw), ("::" + symbol if separator else "")
        elif key == "template_path":
            path = Path(value)
        if path is None:
            return value
        path = _checked_file(source.parent / path if not path.is_absolute() else path, roots)
        if path.name in {"auth.json", ".env"} or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Candidate dependencies must be code/templates, not credentials or large assets")
        parent_id = hashlib.sha256(str(path.parent).encode()).hexdigest()[:16]
        target_dir = destination / "dependencies" / parent_id
        if path.parent == destination or path.parent in destination.parents:
            raise ValueError("Candidate destination must be outside dependency source directory")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        shutil.copyfile(path, target)
        files[str(target.relative_to(destination))] = hashlib.sha256(target.read_bytes()).hexdigest()
        return ("file://" if value.startswith("file://") else "") + str(target) + suffix

    config = visit(yaml.safe_load(source.read_text()))
    (destination / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (destination / "bundle.json").write_text(json.dumps({
        "original_root": str(destination), "source_config": str(source), "files": files,
    }, indent=2) + "\n")
    return destination / "config.yaml"


def load_config(artifact_dir: Path, *, checkout: Path | None = None):
    import yaml
    from harnessx.core.harness import HarnessConfig
    root = Path(artifact_dir).resolve()
    manifest = json.loads((root / "bundle.json").read_text())
    if checkout is not None:
        checkout = Path(checkout).resolve()
    for relative, digest in manifest.get("files", {}).items():
        dependency = _checked_file(root / relative, (root,))
        if hashlib.sha256(dependency.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Candidate dependency changed after bundling: {relative}")
    origin = manifest["original_root"]
    value = yaml.safe_load((root / "config.yaml").read_text())

    def rebase(node, key=None):
        if isinstance(node, dict):
            return {k: rebase(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [rebase(v, key) for v in node]
        if isinstance(node, str):
            value = node.replace(origin + "/", str(root) + "/")
            if key in {"_target_", "custom"}:
                _check_target(value, checkout)
            if value.startswith("file://") or key == "template_path":
                path = Path(value[7:].partition("::")[0] if value.startswith("file://") else value)
                _checked_file(path, (root,))
            return value
        return node

    return HarnessConfig.from_yaml(yaml.safe_dump(rebase(value)))
