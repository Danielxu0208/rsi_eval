from pathlib import Path

import pytest

from rsi_eval.compose_context import isolate_compose_builds


def hash_context(path):
    # The real filesystem hasher is optional with Harbor; these tests exercise
    # context routing without adding Harbor as a core dependency.
    import hashlib
    if not path.is_dir():
        raise ValueError("not a directory")
    return hashlib.sha256(b"".join(p.relative_to(path).as_posix().encode() + p.read_bytes()
        for p in sorted(path.rglob("*")) if p.is_file())).hexdigest()


def test_multiple_contexts_external_dockerfile_runtime_paths_unchanged(tmp_path):
    project = tmp_path / "task/environment"
    project.mkdir(parents=True)
    context, extra = project / "worker", tmp_path / "declared-assets"
    context.mkdir(); extra.mkdir()
    (context / "input").write_text("first")
    (extra / "hidden").write_text("extra")
    dockerfile = tmp_path / "external.Dockerfile"
    dockerfile.write_text("FROM scratch\nCOPY input /input\n")
    (tmp_path / "external.Dockerfile.dockerignore").write_text("ignored\n")
    runtime = {"volumes": [{"type": "bind", "source": str(project / "runtime"), "target": "/runtime"}],
               "env_file": [{"path": str(project / "runtime.env"), "required": True}],
               "environment": {"LITERAL": "$keep"}}
    build = {"context": str(context), "dockerfile": str(dockerfile),
             "additional_contexts": {"assets": str(extra)}}
    document = {"services": {"main": {**runtime, "build": build}, "reader": {"image": "unchanged"}}}
    result, records = isolate_compose_builds(document, project, tmp_path / "out", hash_context=hash_context)
    assert document["services"]["main"]["build"] == build
    for key, value in runtime.items():
        assert result["services"]["main"][key] == value
    fixed = result["services"]["main"]["build"]
    assert (Path(fixed["context"]) / "input").read_text() == "first"
    assert Path(fixed["dockerfile"]).read_text() == dockerfile.read_text()
    assert Path(fixed["dockerfile"] + ".dockerignore").read_text() == "ignored\n"
    assert (Path(fixed["additional_contexts"]["assets"]) / "hidden").read_text() == "extra"
    assert {row["role"] for row in records} == {"context", "external_dockerfile", "additional:assets"}


@pytest.mark.parametrize("context", ["https://example.test/repo.git", "git@example.test:repo", "~/.private"])
def test_unsupported_context_fails_explicitly(tmp_path, context):
    with pytest.raises(ValueError, match="explicit local path"):
        isolate_compose_builds({"services": {"main": {"build": {"context": context}}}},
                              tmp_path, tmp_path / "out", hash_context=hash_context)


def test_context_changes_do_not_reuse_old_snapshot(tmp_path):
    source = tmp_path / "input"; source.mkdir()
    (source / "data").write_text("aaaa")
    doc = {"services": {"main": {"build": {"context": str(source), "dockerfile_inline": "FROM scratch"}}}}
    one, _ = isolate_compose_builds(doc, tmp_path, tmp_path / "out", hash_context=hash_context)
    (source / "data").write_text("bbbb")
    two, _ = isolate_compose_builds(doc, tmp_path, tmp_path / "out", hash_context=hash_context)
    assert one["services"]["main"]["build"]["context"] != two["services"]["main"]["build"]["context"]


def test_all_services_and_named_service_contexts_are_isolated(tmp_path):
    services = {}
    for name in ("main", "worker"):
        context = tmp_path / name
        context.mkdir()
        (context / "Dockerfile").write_text("FROM scratch\n")
        (context / "identity").write_text(name)
        services[name] = {"build": {"context": str(context)}}
    services["main"]["build"]["additional_contexts"] = ["worker=service:worker"]
    result, records = isolate_compose_builds({"services": services}, tmp_path,
                                             tmp_path / "out", hash_context=hash_context)
    assert {r["service"] for r in records if r["role"] == "context"} == {"main", "worker"}
    assert result["services"]["main"]["build"]["additional_contexts"] == {"worker": "service:worker"}
    assert all(result["services"][name]["build"]["context"] != str(tmp_path / name) for name in services)


def test_tampered_existing_snapshot_is_rejected(tmp_path):
    source = tmp_path / "input"; source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    doc = {"services": {"main": {"build": {"context": str(source)}}}}
    result, _ = isolate_compose_builds(doc, tmp_path, tmp_path / "out", hash_context=hash_context)
    (Path(result["services"]["main"]["build"]["context"]) / "Dockerfile").write_text("tampered")
    with pytest.raises(ValueError, match="changed while being copied"):
        isolate_compose_builds(doc, tmp_path, tmp_path / "out", hash_context=hash_context)
