"""Content identity and immutable per-evaluation copies of candidate files."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

_SKIP = {".git", ".venv", "__pycache__", ".pytest_cache", ".DS_Store",
         "auth.json", ".env", "node_modules", ".codex-auth"}


def artifact_files(root: Path) -> list[Path]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"candidate artifact directory does not exist: {root}")
    result = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(p in _SKIP for p in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"candidate snapshots require regular files, not symlinks: {relative}")
        if path.is_file():
            result.append(path)
    return result


def artifact_hash(root: Path) -> str:
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for path in artifact_files(root):
        name = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update((path.stat().st_mode & 0o111).to_bytes(2, "big"))
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def snapshot(root: Path, destination: Path) -> str:
    root = Path(root).resolve()
    paths = artifact_files(root)
    destination.mkdir(parents=True, exist_ok=False)
    for path in paths:
        target = destination / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return artifact_hash(destination)
