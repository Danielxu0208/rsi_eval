"""Keep all audited tasks selectable, with oracle limits applied separately."""
import hashlib
import json
from pathlib import Path

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, preview
from rsi_eval.benchmarks import resolve
from rsi_eval.evaluation import _job_config


ROOT = Path(__file__).resolve().parents[1]
AUDIT = json.loads((ROOT / "docs/coverage-audit-other.json").read_text())
PROFILES = AUDIT["profiles"]


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "agent.py").write_text("# Coverage fixture; no external execution.\n")
    return TargetSpec(root, "fixture:Agent")


@pytest.mark.parametrize("audit", PROFILES, ids=lambda row: row["profile"])
def test_full_selection_matches_independently_audited_source_manifest(candidate, tmp_path, audit):
    resolved = preview(target=candidate,
                       benchmark=BenchmarkSelection(audit["name"], audit["version"]),
                       output_dir=tmp_path / "output")
    spec = resolved["benchmark"]
    digest = hashlib.sha256(json.dumps(spec["tasks"], sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    assert digest == audit["task_manifest_sha256"]
    assert spec["dataset_ref"] == audit["dataset_ref"]
    assert spec["selection"] == "full"
    assert len(spec["task_ids"]) == len(set(spec["task_ids"])) == audit["catalog_task_count"]
    assert resolved["expected_trials"] == audit["catalog_task_count"]
    assert resolved["options"]["timeout"] is None
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("audit", [row for row in PROFILES
                                  if row["candidate_route"] == "native_harbor_explicit_tasks"],
                         ids=lambda row: row["profile"])
def test_full_native_job_forwards_every_pinned_task(candidate, tmp_path, audit):
    resolved = preview(target=candidate,
                       benchmark=BenchmarkSelection(audit["name"], audit["version"]),
                       options=RunOptions(repetitions=2), output_dir=tmp_path / "output")
    job = _job_config(resolved, candidate.artifact_dir)
    tasks = resolved["benchmark"]["tasks"]
    assert job["tasks"] == [{key: value for key, value in task.items()
                              if key not in {"id", "source", "sha256"}} for task in tasks]
    assert len(job["tasks"]) == audit["catalog_task_count"]
    assert job["n_attempts"] == 2
    assert resolved["expected_trials"] == 2 * audit["catalog_task_count"]


@pytest.mark.parametrize(("name", "version"), [("livecodebench", "release_v1"), ("drop", "1.0-agent")])
def test_partial_oracle_coverage_does_not_shrink_candidate_selection(candidate, tmp_path, name, version):
    spec = resolve(BenchmarkSelection(name, version))
    oracle = spec["oracle"]
    unsupported = set(oracle.get("unsupported_task_ids", []))
    if "supported_task_ids" in oracle:
        unsupported.update(set(spec["task_ids"]) - set(oracle["supported_task_ids"]))
    assert unsupported
    task_id = sorted(unsupported)[0]
    selection = BenchmarkSelection(name, version, (task_id,))
    resolved = preview(target=candidate, benchmark=selection, output_dir=tmp_path / "candidate-run")
    assert resolved["benchmark"]["task_ids"] == [task_id]
    with pytest.raises(ValueError, match="no declared oracle/reference solution"):
        preview(target=TargetSpec(candidate.artifact_dir, "oracle"), benchmark=selection,
                output_dir=tmp_path / "oracle-run")


def test_drop_keeps_every_released_split_and_duplicate_record():
    spec = resolve(BenchmarkSelection("drop", "1.0-agent"))
    assert sum(task["split"] == "train" for task in spec["tasks"]) == 77409
    assert sum(task["split"] == "dev" for task in spec["tasks"]) == 9536
    assert "dev-28553293-d719-441b-8f00-ce3dc6df5398" in spec["task_ids"]
    assert "dev-28553293-d719-441b-8f00-ce3dc6df5398-occurrence-2" in spec["task_ids"]


def test_mgsm_covers_each_released_row_in_all_eleven_languages():
    spec = resolve(BenchmarkSelection("mgsm", "1.0-agent"))
    languages = {"bn", "de", "en", "es", "fr", "ja", "ru", "sw", "te", "th", "zh"}
    assert {(task["language"], task["record_id"]) for task in spec["tasks"]} == {
        (language, index) for language in languages for index in range(250)}
