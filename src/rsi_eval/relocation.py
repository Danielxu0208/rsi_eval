"""Resolve recorded paths after an explicit, receipt-checked experiment import.

Raw benchmark evidence and candidate bytes remain unchanged. A relocation
manifest supplies path substitutions only to readers of those records.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


def _regular_receipt_file(path: Path, root: Path) -> None:
    """Receipts are local control records, never links to ambient host state."""
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError("Experiment relocation receipt escapes its root")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("Experiment relocation manifests and receipts must not be symlinks")
        current = current.parent
    if not path.is_file():
        raise ValueError("Experiment relocation requires a completed import receipt")


def _validate_manifest(root: Path, data: Any) -> None:
    if not isinstance(data, dict) or data.get("format") != "rsi-eval-relocation-v1" or data.get("destination") != str(root):
        raise ValueError("Invalid experiment relocation manifest")
    mappings = data.get("mappings")
    if not isinstance(mappings, dict) or not mappings or any(
            not isinstance(old, str) or not isinstance(new, str)
            or not Path(old).is_absolute() or not Path(new).is_absolute()
            for old, new in mappings.items()):
        raise ValueError("Experiment relocation mappings must contain only absolute paths")
    relative = data.get("receipt")
    if not isinstance(relative, str) or re.fullmatch(r"import-receipts/import-[0-9]{4,}", relative) is None:
        raise ValueError("Experiment relocation requires a scoped import receipt")
    result_path = root / relative / "result.json"
    bundle_path = root / relative / "bundle.json"
    for path in (result_path, bundle_path):
        _regular_receipt_file(path, root)
    result = json.loads(result_path.read_text())
    bundle = json.loads(bundle_path.read_text())
    if (not isinstance(result, dict) or result.get("status") != "completed"
            or result.get("destination") != str(root) or result.get("mappings") != mappings
            or not isinstance(result.get("source"), str)
            or mappings.get(result.get("source")) != str(root)
            or not isinstance(bundle, dict) or bundle.get("format") != "rsi-eval-managed-bundle-v1"
            or bundle.get("source") != result.get("source")):
        raise ValueError("Experiment relocation manifest does not match a completed import receipt")


def manifest_for(anchor: Path) -> tuple[Path, dict] | None:
    anchor = Path(anchor).absolute()
    directory = anchor if anchor.is_dir() else anchor.parent
    for parent in (directory, *directory.parents):
        path = parent / ".rsi-relocation.json"
        if path.is_symlink():
            raise ValueError("Experiment relocation manifest must not be a symlink")
        if path.is_file():
            data = json.loads(path.read_text())
            _validate_manifest(parent, data)
            return parent, data
    return None


def map_text(value: str, mappings: dict[str, str]) -> str:
    # Apply the longest prefix once. A destination may itself contain an old
    # prefix, so iterative str.replace would redirect it a second time.
    if not mappings:
        return value
    pattern = "|".join(re.escape(key) for key in sorted(mappings, key=len, reverse=True))
    return re.sub(r"(?<![A-Za-z0-9_./-])(?:" + pattern + r")(?=$|[/\\\s\"'`,;:)\]}])",
                  lambda match: mappings[match.group()], value)


def map_value(value: Any, mappings: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {map_text(key, mappings) if isinstance(key, str) else key:
                map_value(item, mappings) for key, item in value.items()}
    if isinstance(value, list):
        return [map_value(item, mappings) for item in value]
    if isinstance(value, tuple):
        return tuple(map_value(item, mappings) for item in value)
    return map_text(value, mappings) if isinstance(value, str) else value


def relocate(value: Any, anchor: Path) -> Any:
    found = manifest_for(anchor)
    return map_value(value, found[1]["mappings"]) if found else value


def read_json(path: Path) -> Any:
    return relocate(json.loads(Path(path).read_text()), Path(path))
