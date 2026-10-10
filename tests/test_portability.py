"""Bundles retain immutable execution bytes and explicitly rebase control paths."""
from pathlib import Path
import json
import sys

import pytest

from rsi_eval import methods, portability
from rsi_eval.artifacts import artifact_hash
from rsi_eval.evaluation import _json_write


def portable_record(root, method, config, monkeypatch, *, environment=None):
    """Attach manager metadata to native deterministic loop fixtures (no installs)."""
    root.mkdir(exist_ok=True)
    runtime = root / "runtime"
    if not runtime.exists():
        methods._runtime_copy(runtime)
    (root / "upstream-source").mkdir(exist_ok=True)
    environment = environment or Path(sys.prefix)
    if not environment.exists():
        environment.mkdir()
    identity = {"python": "3.12.0", "packages": {"harbor": "0.18.0"}}
    monkeypatch.setattr(methods, "environment_identity", lambda path: identity)
    monkeypatch.setattr(methods, "_verify_upstream", lambda *args: None)
    record = {"schema_version": 2, "status": "prepared", "method": method, "experiment": str(root),
              "environment": str(environment), "environment_identity": identity,
              "identity": {"upstream_commit": methods.METHODS[method]["commit"]},
              "configuration": config, "runtime_sha256": artifact_hash(runtime)}
    _json_write(root / "configuration.json", config)
    _json_write(root / "method.json", record)
    return record


def test_bundle_relocates_control_but_preserves_raw_candidate_runtime_bytes(tmp_path, monkeypatch):
    root = tmp_path / "experiment"
    config = {"codex": {"model": "fixture", "auth_dir": str(tmp_path / "auth"), "expected_version": "fixture"}, "seed": str(root / "seed")}
    record = portable_record(root, "rrsi", config, monkeypatch)
    raw = root / "run/jobs/base/evaluation"
    (raw / "candidate").mkdir(parents=True)
    (raw / "candidate/prompt.md").write_text("keep bytes: " + str(root))
    (raw / "resolved.json").write_text(json.dumps({"output_dir": str(raw)}))
    original_raw = (raw / "resolved.json").read_bytes()
    _json_write(root / "run/frontier.json", {"job": str(raw)})
    (root / "run/view").symlink_to(raw, target_is_directory=True)
    bundle = tmp_path / "bundle"
    portability.export_experiment(root, bundle)
    destination = tmp_path / "moved"
    result = portability.import_experiment(bundle, destination, Path(record["environment"]), tmp_path / "new-auth")
    assert result["status"] == "imported"
    assert (destination / "run/jobs/base/evaluation/resolved.json").read_bytes() == original_raw
    assert artifact_hash(destination / "runtime") == record["runtime_sha256"]
    assert (destination / "run/view").resolve() == destination / "run/jobs/base/evaluation"
    assert json.loads((destination / "run/frontier.json").read_text())["job"] == str(destination / "run/jobs/base/evaluation")
    from rsi_eval.relocation import read_json
    assert read_json(destination / "run/jobs/base/evaluation/resolved.json")["output_dir"] == str(destination / "run/jobs/base/evaluation")
    assert list((destination / "import-receipts").glob("*/original-controls/run/frontier.json"))


def test_bundle_tamper_environment_mismatch_and_legacy_records_are_refused(tmp_path, monkeypatch):
    root = tmp_path / "experiment"
    config = {"codex": {"auth_dir": str(tmp_path / "auth")}}
    record = portable_record(root, "meta-harness", config, monkeypatch)
    bundle = tmp_path / "bundle"
    portability.export_experiment(root, bundle)
    monkeypatch.setattr(methods, "environment_identity", lambda path: {"different": True})
    with pytest.raises(ValueError, match="exact installed dependency"):
        portability.import_experiment(bundle, tmp_path / "moved", Path(record["environment"]), tmp_path / "auth")
    assert not (tmp_path / "moved").exists()
    (bundle / "experiment/configuration.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        portability.import_experiment(bundle, tmp_path / "moved", Path(record["environment"]), tmp_path / "auth")
    record["schema_version"] = 1
    _json_write(root / "method.json", record)
    with pytest.raises(ValueError, match="legacy"):
        portability.export_experiment(root, tmp_path / "legacy-bundle")


def test_external_inputs_require_same_contents_after_explicit_relocation(tmp_path, monkeypatch):
    root = tmp_path / "experiment"
    tasks = tmp_path / "tasks.json"
    tasks.write_text('["extract-elf"]')
    config = {"codex": {"auth_dir": str(tmp_path / "auth")}, "test": {"manifest": str(tasks)}}
    record = portable_record(root, "rrsi", config, monkeypatch)
    bundle = tmp_path / "bundle"
    portability.export_experiment(root, bundle)
    changed = tmp_path / "changed.json"
    changed.write_text('["another-task"]')
    with pytest.raises(ValueError, match="external input changed"):
        portability.import_experiment(bundle, tmp_path / "moved", Path(record["environment"]), tmp_path / "auth", path_map={str(tasks): str(changed)})


def test_export_does_not_legitimize_tampered_meta_evidence(tmp_path, monkeypatch):
    from integrations.meta_harness.state import tree
    root = tmp_path / "experiment"
    portable_record(root, "meta-harness", {"codex": {"auth_dir": str(tmp_path / "auth")}}, monkeypatch)
    raw = root / "run/upstream/jobs/task/result.json"
    raw.parent.mkdir(parents=True)
    _json_write(raw, {"reward": 0})
    _json_write(root / "run/resume.json", {"protected": tree(root / "run/upstream")})
    _json_write(raw, {"reward": 1})
    with pytest.raises(ValueError, match="recorded evidence changed"):
        portability.export_experiment(root, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_export_refuses_missing_declared_external_input_before_creating_bundle(tmp_path, monkeypatch):
    root = tmp_path / "experiment"
    portable_record(root, "rrsi", {"codex": {"auth_dir": str(tmp_path / "auth")},
                    "test": {"manifest": str(tmp_path / "missing-manifest.json")}}, monkeypatch)
    with pytest.raises(ValueError, match="Required external input is missing"):
        portability.export_experiment(root, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()
