import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from rsi_eval.materializers import common, engdesign
from rsi_eval import BenchmarkSelection, TargetSpec, preview


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "engdesign" / engdesign.SOURCE_COMMIT


@pytest.fixture
def catalog():
    return json.loads((ROOT / "src/rsi_eval/data/catalog_engdesign.json").read_text())[0]


@pytest.fixture
def official_sources(monkeypatch):
    """Use exact upstream files, while keeping every test offline."""
    requested = []

    def open_source(url, timeout):
        prefix = ("https://raw.githubusercontent.com/AGI4Engineering/EngDesign/"
                  + engdesign.SOURCE_COMMIT + "/")
        assert url.startswith(prefix)
        assert timeout > 0
        relative = url.removeprefix(prefix)
        requested.append(relative)
        return io.BytesIO((FIXTURE / relative).read_bytes())

    monkeypatch.setattr(common.urllib.request, "urlopen", open_source)
    return requested


def materialize(catalog, directory, ids=("XY_01",)):
    catalog["tasks"] = [task for task in catalog["tasks"] if task["id"] in ids]
    return [Path(task["path"]) for task in engdesign.prepare(catalog, directory)]


def run_grader(task, submission, output):
    return subprocess.run(
        [sys.executable, "-I", str(task / "tests/grade.py"),
         "--submission", str(submission), "--output-dir", str(output)],
        capture_output=True, text=True, timeout=30,
    )


def oracle(task, destination):
    subprocess.run([sys.executable, "-I", str(task / "solution/solve.py"), str(destination)],
                   check=True, capture_output=True, text=True, timeout=30)


def test_catalog_describes_exact_supported_subset_and_official_hashes(catalog):
    assert "open-subset" in catalog["version"]
    assert catalog["coverage"]["supported_task_count"] == 2
    assert len(catalog["coverage"]["unsupported_task_ids"]) == 65
    supported = {task["id"] for task in catalog["tasks"]}
    assert supported == {"AB_01", "XY_01"}
    assert not supported.intersection(catalog["coverage"]["unsupported_task_ids"])
    for name, expected in catalog["source"]["files"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == expected
    assert "MIT License" in (FIXTURE / "LICENSE").read_text()


def test_catalog_preview_is_offline_and_rejects_unsupported_ids(catalog, tmp_path, monkeypatch):
    def no_download(*args, **kwargs):
        raise AssertionError("preview made a network call")

    monkeypatch.setattr(common.urllib.request, "urlopen", no_download)
    artifact = tmp_path / "candidate"
    artifact.mkdir()
    (artifact / "agent.py").write_text("# offline preview candidate\n")
    result = preview(target=TargetSpec(artifact, "oracle"),
                     benchmark=BenchmarkSelection(catalog["name"], catalog["version"], ("XY_01",)),
                     output_dir=tmp_path / "run")
    assert result["benchmark"]["task_ids"] == ["XY_01"]
    assert result["benchmark"]["coverage"]["scope"] == "reviewed-open-subset"
    assert not (tmp_path / "run").exists()
    with pytest.raises(ValueError, match="unknown task IDs"):
        preview(target=TargetSpec(artifact, "oracle"),
                benchmark=BenchmarkSelection(catalog["name"], catalog["version"], ("JY_01",)),
                output_dir=tmp_path / "run")


def test_prepare_preserves_native_code_and_keeps_grader_out_of_agent_image(
        catalog, official_sources, tmp_path):
    task, = materialize(catalog, tmp_path / "prepared")
    assert task.is_relative_to(tmp_path)
    assert not any("AB_01" in name for name in official_sources)
    assert (task / "tests/upstream/evaluate.py").read_bytes() == (
        FIXTURE / "EngDesign-Open/XY_01/evaluate.py").read_bytes()
    assert (task / "environment/output_structure.py").read_bytes() == (
        FIXTURE / "EngDesign-Open/XY_01/output_structure.py").read_bytes()
    assert set(p.name for p in (task / "environment").iterdir()) == {
        "Dockerfile", "requirements.txt", "output_structure.py", "font_rom.sv"}
    prompt = (FIXTURE / "EngDesign-Open/XY_01/LLM_prompt.txt").read_text()
    assert (task / "instruction.md").read_text().startswith(prompt)
    assert "/workspace/output.json" in (task / "instruction.md").read_text()
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["metadata"]["source_commit"] == engdesign.SOURCE_COMMIT
    assert config["verifier"]["timeout_sec"] == 120
    assert (task / "solution/solve.sh").stat().st_mode & 0o111


def test_native_harbor_task_config_is_valid(catalog, official_sources, tmp_path):
    model = pytest.importorskip("harbor.models.task.config")
    task, = materialize(catalog, tmp_path)
    config = model.TaskConfig.model_validate(tomllib.loads((task / "task.toml").read_text()))
    assert config.agent.timeout_sec == 600
    assert config.environment.cpus == 1


def test_download_and_cached_source_corruption_fail_before_evaluation(
        catalog, official_sources, tmp_path):
    catalog["source"]["files"]["LICENSE"] = "0" * 64
    with pytest.raises(ValueError, match="checksum mismatch"):
        materialize(catalog, tmp_path / "new")
    assert not list((tmp_path / "new").rglob("*.part"))
    cache = tmp_path / "cached/source" / engdesign.SOURCE_COMMIT / "LICENSE"
    cache.parent.mkdir(parents=True)
    cache.write_text("changed")
    with pytest.raises(ValueError, match="cached EngDesign source checksum"):
        materialize(catalog, tmp_path / "cached")


@pytest.mark.parametrize("task", [
    {"id": "JY_01", "record_id": "JY_01"},
    {"id": "XY_01", "record_id": "../../evaluate.py"},
])
def test_unsupported_task_or_record_cannot_be_silently_materialized(catalog, task, tmp_path):
    catalog["tasks"] = [task]
    with pytest.raises(ValueError, match="unsupported EngDesign task"):
        engdesign.prepare(catalog, tmp_path)
    assert not list(tmp_path.iterdir())


def test_native_xy_oracle_and_partial_score_remain_distinct(catalog, official_sources, tmp_path):
    pytest.importorskip("pydantic")
    task, = materialize(catalog, tmp_path / "prepared")
    submission = tmp_path / "answer.json"
    oracle(task, submission)
    output = tmp_path / "verifier"
    run = run_grader(task, submission, output)
    assert run.returncode == 0, run.stderr
    assert json.loads((output / "reward.json").read_text()) == {
        "reward": 1.0, "native_score": 100, "confidence": 100}
    answer = json.loads(submission.read_text())
    answer["tetromino_pattern"]["visual"] = ["wrong"] * 4
    submission.write_text(json.dumps(answer))
    run = run_grader(task, submission, output)
    assert run.returncode == 0, run.stderr
    assert json.loads((output / "reward.json").read_text()) == {
        "reward": 0.0, "native_score": 70, "confidence": 100}
    verdict = json.loads((output / "verdict.json").read_text())
    assert verdict["status"] == "graded"
    assert verdict["details"]["pattern_match"] is True
    assert verdict["details"]["visual_match"] is False


@pytest.mark.parametrize("answer", [None, "not JSON", '{}', '{"config": {"rotation": NaN}}', "directory"])
def test_missing_or_invalid_deliverable_fails_task_without_inventing_native_score(
        catalog, official_sources, tmp_path, answer):
    pytest.importorskip("pydantic")
    task, = materialize(catalog, tmp_path / "prepared")
    submission = tmp_path / "answer.json"
    if answer == "directory":
        submission.mkdir()
    elif answer is not None:
        submission.write_text(answer)
    output = tmp_path / "verifier"
    run = run_grader(task, submission, output)
    assert run.returncode == 0, run.stderr
    assert json.loads((output / "reward.json").read_text()) == {"reward": 0.0}
    verdict = json.loads((output / "verdict.json").read_text())
    assert verdict["status"] == "invalid_submission"
    assert verdict["native_score"] is None


def test_verifier_failure_never_reuses_a_reward_or_becomes_a_zero(
        catalog, official_sources, tmp_path):
    pytest.importorskip("pydantic")
    task, = materialize(catalog, tmp_path / "prepared")
    submission = tmp_path / "answer.json"
    oracle(task, submission)
    output = tmp_path / "verifier"
    assert run_grader(task, submission, output).returncode == 0
    with (task / "tests/upstream/evaluate.py").open("a") as stream:
        stream.write("\n# corrupted after materialization\n")
    run = run_grader(task, submission, output)
    assert run.returncode == 1
    assert not (output / "reward.json").exists()
    assert not (output / "reward.txt").exists()
    verdict = json.loads((output / "verdict.json").read_text())
    assert verdict["status"] == "verifier_error"
    assert "checksum mismatch" in verdict["error"]


def test_ab_native_oracle_uses_frozen_evaluator_and_keeps_partial_score(
        catalog, official_sources, tmp_path):
    for package in ("pydantic", "numpy", "scipy", "matplotlib"):
        pytest.importorskip(package)
    task, = materialize(catalog, tmp_path / "prepared", ids=("AB_01",))
    submission = tmp_path / "answer.json"
    oracle(task, submission)
    output = tmp_path / "verifier"
    run = run_grader(task, submission, output)
    assert run.returncode == 0, run.stderr
    assert json.loads((output / "reward.json").read_text()) == {
        "reward": 1.0, "native_score": 100, "confidence": 100}
    answer = json.loads(submission.read_text())
    answer["config"]["mean"] += 10
    submission.write_text(json.dumps(answer))
    run = run_grader(task, submission, output)
    assert run.returncode == 0, run.stderr
    assert json.loads((output / "reward.json").read_text()) == {
        "reward": 0.0, "native_score": 80, "confidence": 100}
