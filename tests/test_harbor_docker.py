"""Context isolation checks without Docker or model calls."""
import json
import os
from pathlib import Path
import stat

import pytest

pytest.importorskip("harbor.environments.docker.docker")

from harbor.environments.factory import EnvironmentFactory
from harbor.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from harbor.models.trial.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from rsi_eval.evaluation import DOCKER_ENVIRONMENT
from rsi_eval.harbor_docker import GUARD_VERSION, context_hash, isolate_context


def make_context(root, payload):
    context = root / "environment"
    hidden = context / "workspace/.oracle/solution.enc"
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(payload)
    (context / "Dockerfile").write_text("FROM scratch\nCOPY workspace/ /app/\n")
    for path in context.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
        os.utime(path, ns=(0, 0))
    return context


def tree_metadata(root):
    return {str(path.relative_to(root)): (path.lstat().st_mode, path.lstat().st_mtime_ns)
            for path in root.rglob("*")}


def test_same_stat_hidden_payloads_get_distinct_buildkit_context_names(tmp_path):
    first = make_context(tmp_path / "one", b"python-reference")
    second = make_context(tmp_path / "two", b"golang-reference")
    relative = "workspace/.oracle/solution.enc"
    a, b = (first / relative).stat(), (second / relative).stat()
    assert (a.st_size, a.st_mtime_ns, a.st_mode) == (b.st_size, b.st_mtime_ns, b.st_mode)
    assert first.name == second.name == "environment"
    before = [tree_metadata(root) for root in (first, second)]
    isolated_a, provenance_a = isolate_context(first, tmp_path / "trial-a")
    isolated_b, provenance_b = isolate_context(second, tmp_path / "trial-b")
    assert isolated_a.name != isolated_b.name
    assert isolated_a.name == "environment-" + provenance_a["sha256"]
    assert (isolated_a / relative).read_bytes() == b"python-reference"
    assert (isolated_b / relative).read_bytes() == b"golang-reference"
    assert [tree_metadata(root) for root in (first, second)] == before
    assert tree_metadata(isolated_a) == before[0]
    assert tree_metadata(isolated_b) == before[1]
    assert context_hash(first) == context_hash(isolated_a)
    assert provenance_a["sha256"] != provenance_b["sha256"]
    saved = json.loads((tmp_path / "trial-a/build-context/provenance.json").read_text())
    assert saved == provenance_a


def test_context_identity_includes_modes_and_relative_symlink_targets(tmp_path):
    source = make_context(tmp_path / "source", b"payload")
    (source / "alias").symlink_to("workspace/.oracle/solution.enc")
    before = context_hash(source)
    isolated, _ = isolate_context(source, tmp_path / "trial")
    assert (isolated / "alias").is_symlink()
    assert os.readlink(isolated / "alias") == "workspace/.oracle/solution.enc"
    assert context_hash(isolated) == before
    executable = source / "workspace/.oracle/solution.enc"
    executable.chmod(stat.S_IMODE(executable.stat().st_mode) | 0o111)
    assert context_hash(source) != before
    changed = context_hash(source)
    (source / "alias").unlink()
    (source / "alias").symlink_to("Dockerfile")
    assert context_hash(source) != changed


def test_escaped_link_is_rejected_without_reading_its_target(tmp_path, monkeypatch):
    source = make_context(tmp_path / "source", b"payload")
    secret = tmp_path / "private"
    secret.write_bytes(b"must not be read")
    (source / "escaped").symlink_to("../../private")
    original = Path.open

    def guarded_read(path, *args, **kwargs):
        assert path != secret, "external symlink target was read"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_read)
    with pytest.raises(ValueError, match="escapes"):
        isolate_context(source, tmp_path / "trial")
    assert not (tmp_path / "trial/build-context").exists()


def test_harbor_factory_uses_isolated_default_context_without_changing_task_config(tmp_path, monkeypatch):
    source = make_context(tmp_path / "source", b"payload")
    task_config = TaskEnvironmentConfig()
    original = task_config.model_dump()
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("Docker must not run"))
    config = EnvironmentConfig.model_validate({"type": "docker", "import_path": DOCKER_ENVIRONMENT})
    environment = EnvironmentFactory.create_environment_from_config(
        config, environment_dir=source, environment_name="fixture", session_id="fixture-session",
        trial_paths=TrialPaths(tmp_path / "trial"), task_env_config=task_config)
    assert environment.environment_dir != source
    assert Path(environment._env_vars.context_dir) == environment.environment_dir
    assert environment._environment_docker_compose_path.parent == environment.environment_dir
    assert environment._env_vars.main_image_name.endswith("-" + GUARD_VERSION)
    assert task_config.model_dump() == original
    assert environment.environment_name == "fixture"
    assert environment.session_id == "fixture-session"


def test_prebuilt_image_reference_is_preserved(tmp_path):
    source = tmp_path / "environment"
    source.mkdir()
    (source / "asset.txt").write_text("runtime asset")
    task_config = TaskEnvironmentConfig(docker_image="example/image@sha256:" + "1" * 64)
    config = EnvironmentConfig.model_validate({"type": "docker", "import_path": DOCKER_ENVIRONMENT})
    environment = EnvironmentFactory.create_environment_from_config(
        config, environment_dir=source, environment_name="prebuilt", session_id="prebuilt-session",
        trial_paths=TrialPaths(tmp_path / "trial"), task_env_config=task_config)
    assert environment._env_vars.prebuilt_image_name == task_config.docker_image
    assert (environment.environment_dir / "asset.txt").read_text() == "runtime asset"


def test_prebuilt_image_can_omit_environment_directory(tmp_path):
    source = tmp_path / "missing-environment"
    task_config = TaskEnvironmentConfig(docker_image="example/image@sha256:" + "1" * 64)
    environment = EnvironmentFactory.create_environment_from_config(
        EnvironmentConfig.model_validate({"type": "docker", "import_path": DOCKER_ENVIRONMENT}),
        environment_dir=source, environment_name="prebuilt", session_id="prebuilt-session",
        trial_paths=TrialPaths(tmp_path / "trial"), task_env_config=task_config)
    assert not source.exists()
    assert environment.environment_dir == source
    assert environment._env_vars.prebuilt_image_name == task_config.docker_image
    assert environment.context_provenance["skipped"] == "prebuilt_image_without_local_context"


@pytest.mark.parametrize("context", [".", "./worker", "..", "/absolute/path"])
def test_compose_keeps_runtime_paths_until_normalized_at_execution(tmp_path, context):
    source = make_context(tmp_path / "source", b"payload")
    (source / "docker-compose.yaml").write_text(
        "services:\n  main:\n    build:\n      context: " + context + "\n")
    environment = EnvironmentFactory.create_environment_from_config(
        EnvironmentConfig.model_validate({"type": "docker", "import_path": DOCKER_ENVIRONMENT}),
        environment_dir=source, environment_name="fixture", session_id="fixture-session",
        trial_paths=TrialPaths(tmp_path / "trial"), task_env_config=TaskEnvironmentConfig())
    assert environment.environment_dir == source
    assert environment._env_vars.context_dir == str(source)
    assert environment.context_provenance["applied"] is False
    assert environment.context_provenance["status"] == "pending_compose_normalization"
    assert not list((tmp_path / "trial/build-context").glob("environment-*"))


def test_extra_compose_file_keeps_original_context(tmp_path):
    source = make_context(tmp_path / "source", b"payload")
    extra = tmp_path / "extra-compose.yaml"
    extra.write_text("services:\n  main:\n    environment:\n      FIXTURE: yes\n")
    environment = EnvironmentFactory.create_environment_from_config(
        EnvironmentConfig.model_validate({"type": "docker", "import_path": DOCKER_ENVIRONMENT,
                                           "extra_docker_compose": [str(extra)]}),
        environment_dir=source, environment_name="fixture", session_id="fixture-session",
        trial_paths=TrialPaths(tmp_path / "trial"), task_env_config=TaskEnvironmentConfig())
    assert environment.environment_dir == source
    assert environment.context_provenance["applied"] is False
    assert environment.context_provenance["compose_files"] == [{"compose_file": str(extra)}]
