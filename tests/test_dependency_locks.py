from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from rsi_eval import dependency_locks as locks
from rsi_eval import methods

real_create_lock = locks.create_lock


def fixture_lock(path, *, method="meta-harness"):
    value = {"schema_version": 1, "method": method, "scope": "host-python-package-versions",
             "environment": {"python": "3.12.15", "implementation": "CPython", "system": "Darwin",
                             "machine": "arm64", "packages": {"harbor": "0.18.0", "pip": "26.0"}},
             "requirements_sha256": {}, "requirements": ["harbor==0.18.0", "pip==26.0"]}
    value["sha256"] = locks._digest(value)
    path.write_text(json.dumps(value))
    return value


def test_lock_detects_tampering_method_and_pip_option_injection(tmp_path):
    path = tmp_path / "lock.json"
    value = fixture_lock(path)
    assert locks.load_lock(path, method="meta-harness") == value
    with pytest.raises(ValueError, match="another method"):
        locks.load_lock(path, method="rrsi")
    value["environment"]["packages"]["harbor"] = "0.19.0"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="content hash"):
        locks.load_lock(path, method="meta-harness")
    value = fixture_lock(path)
    value["requirements"] = ["--extra-index-url https://untrusted.invalid", "harbor==0.18.0"]
    value["sha256"] = locks._digest({k: v for k, v in value.items() if k != "sha256"})
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="exact package"):
        locks.load_lock(path, method="meta-harness")


def test_lock_requires_matching_platform_and_exact_package_inventory(tmp_path, monkeypatch):
    value = fixture_lock(tmp_path / "lock.json")
    installed = deepcopy(value["environment"])
    monkeypatch.setattr(locks, "inspect_environment", lambda python: installed)
    locks.verify_environment(value, Path("python"))
    installed["system"] = "Linux"
    with pytest.raises(ValueError, match="platform"):
        locks.verify_environment(value, Path("python"))
    installed["system"] = "Darwin"
    installed["python"] = "3.13.0"
    with pytest.raises(ValueError, match="Python minor"):
        locks.verify_environment(value, Path("python"))
    installed["python"] = "3.12.99"
    installed["packages"]["extra"] = "1"
    with pytest.raises(ValueError, match="differ"):
        locks.verify_environment(value, Path("python"))


def test_inventory_ignores_caller_pythonpath_metadata(tmp_path, monkeypatch):
    metadata = tmp_path / "not_installed_in_venv-99.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: not-installed-in-venv\nVersion: 99\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    actual = locks.inspect_environment(Path(sys.executable))
    assert "not-installed-in-venv" not in actual["packages"]


def test_prepare_replays_lock_and_pins_identity_without_reresolving_adapter_requirements(tmp_path, monkeypatch):
    lock_path = tmp_path / "lock.json"
    value = fixture_lock(lock_path)
    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").symlink_to(sys.executable)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"codex": {"model": "fixture", "auth_dir": str(tmp_path / "auth"),
                                            "expected_version": "fixed"}}))
    calls = []
    def invoke(command, **kwargs):
        calls.append(command)
        if command[:2] == ["git", "clone"]:
            Path(command[-1]).mkdir()
    monkeypatch.setattr(methods, "_invoke", invoke)
    monkeypatch.setattr(methods, "_has_pip", lambda python: True)
    monkeypatch.setattr(methods, "_git_output", lambda path, *args: "" if args[0] == "status" else methods.METHODS["meta-harness"]["commit"])
    monkeypatch.setattr(methods, "environment_identity", lambda path: value["environment"])
    monkeypatch.setattr(locks, "inspect_environment", lambda python: deepcopy(value["environment"]))
    root = tmp_path / "experiment"
    record = methods.prepare_method(method="meta-harness", experiment=root, profile_file=profile,
                                    environment=environment, dependency_lock=lock_path)
    assert record["identity"]["dependency_lock_sha256"] == value["sha256"]
    assert record["dependency_lock_sha256"] == value["sha256"]
    install = next(command for command in calls if "install" in command)
    assert install[-2:] == ["-r", str(root / "dependency-lock.txt")]
    assert "--report" in install and "harbor==0.18.0" not in install
    assert (root / "dependency-lock.txt").read_text() == "harbor==0.18.0\npip==26.0\n"
    assert any(command[-3:] == ["-m", "pip", "check"] for command in calls)
    with pytest.raises(ValueError, match="settings changed"):
        methods.prepare_method(method="meta-harness", experiment=root, profile_file=profile,
                               environment=environment, install=False)


def test_new_prepare_writes_reusable_lock_automatically(tmp_path, monkeypatch):
    from test_methods_cli import setup_probe
    # Reuse the manager's external-process fixture, then exercise the real lock
    # writer with a deterministic installed inventory and successful pip check.
    args, calls, _ = setup_probe.__wrapped__(tmp_path, monkeypatch)
    fixture = fixture_lock(tmp_path / "reference.json")
    monkeypatch.setattr(methods, "_has_pip", lambda python: True)
    monkeypatch.setattr(locks, "inspect_environment", lambda python: fixture["environment"])
    # setup_probe stubs this optional dependency boundary; restore real writer.
    monkeypatch.setattr(locks, "create_lock", real_create_lock)
    monkeypatch.setattr(locks.subprocess, "run", lambda *a, **kw: None)
    record = methods.prepare_method(**args)
    saved = locks.load_lock(args["experiment"] / "dependency-lock.json", method="meta-harness")
    assert record["dependency_lock_sha256"] == saved["sha256"]
    assert saved["requirements"] == ["harbor==0.18.0", "pip==26.0"]


def test_bundled_locks_match_current_adapter_inputs():
    root = Path(__file__).resolve().parents[1]
    for method, spec in methods.METHODS.items():
        lock = locks.load_lock(root / "integrations/locks/macos-arm64-py312" / f"{method}.json", method=method)
        assert lock["environment"]["machine"] == "arm64"
        assert lock["environment"]["system"] == "Darwin"
        requirements = root / "integrations" / spec["module"] / "requirements.txt"
        expected = {str(requirements.relative_to(root)): locks.hashlib.sha256(requirements.read_bytes()).hexdigest()} if requirements.exists() else {}
        assert lock["requirements_sha256"] == expected
