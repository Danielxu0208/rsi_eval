import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

from rsi_eval import BenchmarkSelection
from rsi_eval.benchmarks import list_benchmarks
from rsi_eval.materializers import drop
from rsi_eval.materializers.drop_verifier import grade, read_prediction


COMMIT = "b1f372248c17ad12684d344955fbcd98e957e77e"
FIXTURES = Path(__file__).parent / "fixtures" / "drop" / COMMIT
DEV_IDS = ("dev-f37e81fa-ef7b-4583-b671-762fc433faa9", "dev-ac6ba235-3024-4f63-a6ab-730a14def4cb")


def catalog():
    return next(spec for spec in list_benchmarks() if spec["name"] == "drop")


@pytest.fixture(scope="module")
def official():
    spec = importlib.util.spec_from_file_location("drop_test_official", FIXTURES / "drop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def annotation(number="", spans=None, day="", month="", year=""):
    return {"number": number, "spans": spans or [], "date": {"day": day, "month": month, "year": year}}


def test_official_fixture_and_catalog_are_pinned():
    spec = catalog()
    assert len(spec["tasks"]) == 86945
    assert sum(t["split"] == "train" for t in spec["tasks"]) == 77409
    assert sum(t["split"] == "dev" for t in spec["tasks"]) == 9536
    assert len({t["id"] for t in spec["tasks"]}) == 86945
    assert spec["aggregation"] == {"type": "drop_em_f1"}
    assert spec["source"]["scorer"]["commit"] == COMMIT
    for name, source in spec["source"]["scorer"]["files"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == source["sha256"]
    assert len(spec["oracle"]["unsupported_task_ids"]) == 9
    assert spec["oracle"]["unsupported_task_ids"] == spec["protocol"]["empty_reference_task_ids"]


@pytest.mark.parametrize(("prediction", "gold", "expected"), [
    ("The Pacific Ocean", "pacific ocean", (1.0, 1.0)),
    ("Pacific", "Pacific Ocean", (0.0, 0.67)),
    ("42.0", "42", (1.0, 1.0)),
    ("brown 4", "brown 5", (0.0, 0.0)),
    (["Bob", "Alice"], ["Alice", "Bob"], (1.0, 1.0)),
    (["Alice"], ["Alice", "Bob"], (0.0, 0.5)),
    (["Alice", "Bob", "Charles"], ["Alice", "Bob"], (0.0, 0.67)),
    ("Alice Bob", ["Alice", "Bob"], (0.0, 0.33)),
])
def test_native_drop_metrics_preserve_fractional_alignment(official, prediction, gold, expected):
    assert official.get_metrics(prediction, gold) == expected


@pytest.mark.parametrize("gold", [
    annotation(number="42"), annotation(spans=["Alice", "Bob"]),
    annotation(day="5", month="June", year="1988"), annotation(month="June"),
    annotation(),
])
def test_oracle_transport_matches_official_gold_conversion(official, gold):
    assert tuple(drop._answer_spans(gold)) == official.answer_json_to_strings(gold)[0]


@pytest.mark.parametrize("payload", ['42', '{}', '[]', '["42", 5]', 'null', '{broken', 'true'])
def test_malformed_json_submission_is_explicit(tmp_path, payload):
    answer = tmp_path / "answer.json"
    answer.write_text(payload)
    prediction, error = read_prediction(answer)
    assert prediction is None
    assert error


def test_invalid_utf8_and_oversize_submissions(tmp_path):
    answer = tmp_path / "answer.json"
    answer.write_bytes(b'"\xff"')
    assert read_prediction(answer)[0] is None
    answer.write_text('"' + 'x' * (1024 * 1024) + '"')
    assert "1 MiB" in read_prediction(answer)[1]


def make_expected(tmp_path, primary, alternatives=()):
    path = tmp_path / "expected.json"
    path.write_text(json.dumps({
        "query_id": "test-question", "split": "dev", "annotations": {
            "fixture-passage": {"qa_pairs": [{"query_id": "test-question", "answer": primary,
                                             "validated_answers": list(alternatives)}]}
        },
    }))
    return path


def test_real_verifier_retains_partial_f1_and_artifacts(tmp_path):
    expected = make_expected(tmp_path, annotation(spans=["Pacific Ocean"]))
    answer = tmp_path / "answer.json"
    answer.write_text('"Pacific"')
    result = grade(answer, expected, FIXTURES / "drop.py", tmp_path / "logs")
    assert result == {"reward": 0.67, "exact_match": 0.0, "f1": 0.67}
    assert json.loads((tmp_path / "logs/reward.json").read_text()) == result
    report = json.loads((tmp_path / "logs/grading.json").read_text())
    assert report["prediction"] == "Pacific"
    assert report["submission_error"] is None
    assert report["metric_scales"]["f1"] == [0, 1]


def test_validated_answers_and_date_are_scored_by_upstream(tmp_path):
    expected = make_expected(tmp_path, annotation(number="1998"),
                             [annotation(day="5", month="June", year="1988")])
    answer = tmp_path / "answer.json"
    answer.write_text('"5 June 1988"')
    assert grade(answer, expected, FIXTURES / "drop.py", tmp_path / "logs")["f1"] == 1.0


def test_missing_submission_uses_native_zero_not_verifier_error(tmp_path):
    expected = make_expected(tmp_path, annotation(number="42"))
    result = grade(tmp_path / "missing", expected, FIXTURES / "drop.py", tmp_path / "logs")
    assert result == {"reward": 0.0, "exact_match": 0.0, "f1": 0.0}
    assert json.loads((tmp_path / "logs/grading.json").read_text())["submission_error"]


def test_empty_reference_retains_upstream_zero(tmp_path):
    expected = make_expected(tmp_path, annotation())
    answer = tmp_path / "answer.json"
    answer.write_text('"anything"')
    assert grade(answer, expected, FIXTURES / "drop.py", tmp_path / "logs")["f1"] == 0.0


def test_missing_scorer_is_infrastructure_error_without_reward(tmp_path):
    expected = make_expected(tmp_path, annotation(number="42"))
    with pytest.raises(FileNotFoundError):
        grade(tmp_path / "missing", expected, tmp_path / "missing.py", tmp_path / "logs")
    assert not (tmp_path / "logs/reward.json").exists()


def prepare_fixture(monkeypatch, tmp_path, task_ids=DEV_IDS, data=None):
    spec = copy.deepcopy(catalog())
    spec["tasks"] = [next(t for t in spec["tasks"] if t["id"] == task_id) for task_id in task_ids]
    payload = (FIXTURES / "sample.json").read_bytes() if data is None else json.dumps(data).encode()
    spec["source"]["splits"]["dev"]["task_count"] = 2
    spec["source"]["splits"]["dev"]["sha256"] = hashlib.sha256(payload).hexdigest()
    def fixture_download(url, destination, digest):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.name == "drop_dataset.zip":
            with zipfile.ZipFile(destination, "w") as archive:
                archive.writestr("drop_dataset/drop_dataset_dev.json", payload)
                archive.writestr("drop_dataset/license.txt", (FIXTURES / "data-license.txt").read_bytes())
        else:
            destination.write_bytes((FIXTURES / destination.name).read_bytes())
        return destination
    monkeypatch.setattr(drop, "download", fixture_download)
    return spec


def test_materializer_hides_gold_and_preserves_selected_records(monkeypatch, tmp_path):
    spec = prepare_fixture(monkeypatch, tmp_path)
    tasks = drop.prepare(spec, tmp_path / "tasks")
    assert [t["id"] for t in tasks] == list(DEV_IDS)
    for task in tasks:
        root = Path(task["path"])
        assert "COPY" not in (root / "environment/Dockerfile").read_text()
        assert 'python -I /tests/verify.py' in (root / "tests/test.sh").read_text()
        assert (root / "tests/official_drop.py").read_bytes() == (FIXTURES / "drop.py").read_bytes()
        assert set((root / "environment").iterdir()) == {root / "environment/Dockerfile"}
        provenance = json.loads((root / "provenance.json").read_text())
        assert provenance["id"] == task["id"]
        assert provenance["scorer_commit"] == COMMIT
        expected = json.loads((root / "tests/expected.json").read_text())
        original = json.loads((FIXTURES / "sample.json").read_text())["nfl_1184"]
        assert expected["annotations"]["nfl_1184"]["qa_pairs"][0] in original["qa_pairs"]
        assert original["passage"] in (root / "instruction.md").read_text()
    assert 'JaMarcus Russell' in (Path(tasks[0]["path"]) / "tests/expected.json").read_text()


def test_materializer_rejects_changed_source_content(monkeypatch, tmp_path):
    spec = prepare_fixture(monkeypatch, tmp_path)
    spec["source"]["splits"]["dev"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        drop.prepare(spec, tmp_path / "tasks")


def test_duplicate_source_query_keeps_both_records(monkeypatch, tmp_path):
    duplicate = "28553293-d719-441b-8f00-ce3dc6df5398"
    ids = (f"dev-{duplicate}", f"dev-{duplicate}-occurrence-2")
    data = json.loads((FIXTURES / "sample.json").read_text())
    for question in data["nfl_1184"]["qa_pairs"]:
        question["query_id"] = duplicate
    spec = prepare_fixture(monkeypatch, tmp_path, ids, data)
    tasks = drop.prepare(spec, tmp_path / "tasks")
    assert [t["id"] for t in tasks] == list(ids)
    first, second = [json.loads((Path(t["path"]) / "tests/expected.json").read_text()) for t in tasks]
    assert first["query_id"] == second["query_id"]
    assert first["annotations"] != second["annotations"]


def test_materializer_rejects_wrong_split_and_unknown_id(monkeypatch, tmp_path):
    spec = prepare_fixture(monkeypatch, tmp_path)
    spec["tasks"][0]["split"] = "train"
    with pytest.raises(ValueError, match="declared split"):
        drop.prepare(spec, tmp_path / "wrong-split")
    spec["tasks"][0]["split"] = "dev"
    spec["tasks"][0]["id"] = "dev-00000000-0000-0000-0000-000000000000"
    with pytest.raises(ValueError, match="pinned dataset"):
        drop.prepare(spec, tmp_path / "unknown-id")
