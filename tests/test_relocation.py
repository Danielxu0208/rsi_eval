import json
from pathlib import Path

import pytest

from rsi_eval.relocation import read_json


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def imported(root):
    root.mkdir()
    old = str(root.parent / "deleted-source")
    mappings = {old: str(root), str(root.parent / "old-auth"): str(root.parent / "new-auth")}
    manifest = {"format": "rsi-eval-relocation-v1", "destination": str(root),
                "mappings": mappings, "receipt": "import-receipts/import-0001"}
    write(root / ".rsi-relocation.json", manifest)
    receipt = root / manifest["receipt"]
    write(receipt / "result.json", {"status": "completed", "source": old,
                                    "destination": str(root), "mappings": mappings})
    write(receipt / "bundle.json", {"format": "rsi-eval-managed-bundle-v1", "source": old})
    raw = root / "run/result.json"
    write(raw, {"artifact_dir": old + "/run/candidate", "model": "fixed-model"})
    return manifest, raw, receipt


def test_matching_receipt_relocates_paths_after_original_tree_is_deleted(tmp_path):
    root = tmp_path / "moved"
    _, raw, _ = imported(root)
    before = raw.read_bytes()
    assert read_json(raw) == {"artifact_dir": str(root / "run/candidate"), "model": "fixed-model"}
    assert raw.read_bytes() == before


@pytest.mark.parametrize("change", ["nonpath", "no_receipt", "partial", "changed_map", "changed_source", "escaped_receipt"])
def test_unverified_relocation_cannot_rewrite_execution_identity(tmp_path, change):
    root = tmp_path / "moved"
    manifest, raw, receipt = imported(root)
    if change == "nonpath":
        manifest["mappings"]["fixed-model"] = "changed-model"
    elif change == "no_receipt":
        (receipt / "result.json").unlink()
    elif change == "partial":
        result = json.loads((receipt / "result.json").read_text())
        write(receipt / "result.json", {**result, "status": "importing"})
    elif change == "changed_map":
        first = next(iter(manifest["mappings"]))
        manifest["mappings"][first] = str(tmp_path / "another-root")
    elif change == "changed_source":
        bundle = json.loads((receipt / "bundle.json").read_text())
        write(receipt / "bundle.json", {**bundle, "source": str(tmp_path / "other-source")})
    else:
        manifest["receipt"] = "../outside"
    write(root / ".rsi-relocation.json", manifest)
    with pytest.raises(ValueError, match="relocation"):
        read_json(raw)


@pytest.mark.parametrize("location", ["manifest", "result", "parent"])
def test_manifest_and_receipt_symlinks_are_rejected(tmp_path, location):
    root = tmp_path / "moved"
    _, raw, receipt = imported(root)
    if location == "manifest":
        path = root / ".rsi-relocation.json"
    elif location == "result":
        path = receipt / "result.json"
    else:
        path = receipt
    destination = tmp_path / "redirected"
    path.rename(destination)
    path.symlink_to(destination, target_is_directory=destination.is_dir())
    with pytest.raises(ValueError, match="symlink|escapes"):
        read_json(raw)
