from __future__ import annotations

import csv
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import random
import shutil
import tarfile
import zipfile

import pytest

from rsi_eval.benchmarks import list_benchmarks, resolve
from rsi_eval.types import BenchmarkSelection
from rsi_eval.materializers import reasoning, mce
from rsi_eval.materializers.reasoning_verifier import score
from rsi_eval.materializers.mce_verifier import native_evaluator, score as mce_score

FIXTURES = Path(__file__).parent / "fixtures/mce_native"


def _spec(name):
    return deepcopy(next(spec for spec in list_benchmarks() if spec["name"] == name))


def _source(spec, tasks):
    spec["tasks"] = tasks
    spec["source"]["record_count"] = len(tasks)
    spec["source"]["task_ids_sha256"] = hashlib.sha256("\n".join(sorted(t["id"] for t in tasks)).encode()).hexdigest()
    return spec


def _tar(path, content):
    with tarfile.open(path, "w") as stream:
        for name, value in content.items():
            data = value.encode(); info = tarfile.TarInfo(name); info.size = len(data)
            stream.addfile(info, io.BytesIO(data))


def test_complete_pinned_catalogs_and_selection():
    expected = {"mmlu": [115700], "gpqa": [198, 448, 546], "arc-agi": [800],
                "mce-finer": [400], "mce-uspto": [180], "mce-symptom2disease": [462],
                "mce-lawbench": [350], "mce-aegis": [668]}
    for name, counts in expected.items():
        specs = [s for s in list_benchmarks() if s["name"] == name]
        assert sorted(len(s["tasks"]) for s in specs) == counts
        for spec in specs:
            resolved = resolve(BenchmarkSelection(name, spec["version"]))
            assert resolved["selection"] == "full"
            assert len({task["id"] for task in resolved["tasks"]}) == len(spec["tasks"])
            assert spec["source"]["task_ids_sha256"] == hashlib.sha256("\n".join(sorted(t["id"] for t in spec["tasks"])).encode()).hexdigest()
            selected = resolve(BenchmarkSelection(name, spec["version"], task_ids=(spec["tasks"][-1]["id"], spec["tasks"][0]["id"])))
            assert selected["task_ids"] == [spec["tasks"][-1]["id"], spec["tasks"][0]["id"]]
    assert _spec("mmlu")["source"]["split_counts"] == {"auxiliary_train": 99842, "dev": 285, "test": 14042, "val": 1531}


@pytest.mark.parametrize("response,correct", [(" A\n", True), ("a", False), ("Answer: A", False), ("AB", False), (None, False)])
def test_choice_submission_is_exact(response, correct):
    assert score({"kind": "choice", "answer": "A"}, response)["correct"] is correct


def test_arc_all_test_inputs_and_three_trials():
    expected = {"kind": "arc", "outputs": [[[1]], [[2]]]}
    assert score(expected, json.dumps({"predictions": [[[[0]], [[3]], [[1]]], [[[2]]]]}))["correct"]
    assert not score(expected, json.dumps({"predictions": [[[[1]]], [[[0]]]]}))["correct"]
    assert not score(expected, json.dumps({"predictions": [[[[1]]]]}))["correct"]
    assert not score(expected, json.dumps({"predictions": [[[[1]]] * 4, [[[2]]]]}))["valid_submission"]
    assert not score(expected, json.dumps({"predictions": [[[[True]]], [[[2]]]]}))["valid_submission"]
    assert not score(expected, json.dumps({"predictions": [[[[1], [1, 2]]], [[[2]]]]}))["valid_submission"]


def test_mmlu_archive_all_splits_and_prompts(tmp_path, monkeypatch):
    archive = tmp_path / "source.tar"
    content = {"data/dev/math_dev.csv": "dev question,one,two,three,four,B\n",
               "data/test/math_test.csv": "test question,one,two,three,four,C\n",
               "data/auxiliary_train/race.csv": "aux question,one,two,three,four,D\n",
               "data/val/math_val.csv": "val question,one,two,three,four,A\n"}
    _tar(archive, content)
    monkeypatch.setattr(reasoning, "download", lambda *args: archive)
    tasks = [{"id": f"{split}-{subject}-00001", "split": split, "subject": subject, "record_id": 0}
             for split, subject in [("dev", "math"), ("test", "math"), ("auxiliary_train", "race"), ("val", "math")]]
    spec = _source(_spec("mmlu"), tasks)
    records = reasoning.load_records(spec, tmp_path / "read")
    assert len(records) == 4
    generated = reasoning.prepare(spec, tmp_path / "generated")
    assert [row["id"] for row in generated] == [t["id"] for t in tasks]
    for task in tasks:
        root = tmp_path / "generated" / task["id"]
        prompt = (root / "instruction.md").read_text()
        assert ("Answer: B" in prompt) == (task["split"] in ("test", "val"))
        assert not (root / "environment" / "expected.json").exists()
    spec["source"]["record_count"] = 5
    with pytest.raises(ValueError, match="record count"):
        reasoning.load_records(spec, tmp_path / "bad")


def test_gpqa_shuffle_uses_entire_release_before_subset(tmp_path, monkeypatch):
    archive = tmp_path / "data.zip"
    names = ["Question", "Correct Answer", "Incorrect Answer 1", "Incorrect Answer 2", "Incorrect Answer 3", "Record ID", "Subdomain"]
    stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=names); writer.writeheader()
    for index in range(5):
        writer.writerow(dict(zip(names, [f"Question {index}", "correct", "wrong1", "wrong2", "wrong3", f"original-{index}", "physics"])))
    spec = _spec("gpqa")
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(spec["source"]["member"], stream.getvalue())
    monkeypatch.setattr(reasoning, "download", lambda *args: archive)
    tasks = [{"id": f"main-{i + 1:04d}", "split": "main", "record_id": i, "source_record_id": f"original-{i}"} for i in range(5)]
    spec = _source(spec, tasks)
    records = reasoning.load_records(spec, tmp_path)
    rng = random.Random(0)
    for index in range(5):
        choices = ["wrong1", "wrong2", "wrong3", "correct"]; rng.shuffle(choices)
        assert records[f"main-{index + 1:04d}"]["row"][1:5] == choices
    spec["tasks"] = [tasks[-1]]
    result = reasoning.prepare(spec, tmp_path / "generated")
    gold = json.loads((Path(result[0]["path"]) / "tests/expected.json").read_text())
    assert gold["answer"] == records["main-0005"]["row"][-1]


def test_arc_gold_outputs_are_private(tmp_path, monkeypatch):
    archive = tmp_path / "arc.tar"
    data = {"name": "original ARC metadata", "train": [{"input": [[1]], "output": [[2]]}],
            "test": [{"input": [[3]], "output": [[8, 9]]}, {"input": [[4]], "output": [[7, 6]]}]}
    _tar(archive, {"upstream/data/evaluation/abc.json": json.dumps(data)})
    monkeypatch.setattr(reasoning, "download", lambda *args: archive)
    spec = _source(_spec("arc-agi"), [{"id": "evaluation-abc", "split": "evaluation"}])
    row = reasoning.prepare(spec, tmp_path / "generated")[0]
    root = Path(row["path"])
    prompt = (root / "instruction.md").read_text()
    assert '[[8,9]]' not in prompt and '[[7,6]]' not in prompt
    assert json.loads((root / "tests/expected.json").read_text())["outputs"] == [[[8, 9]], [[7, 6]]]


@pytest.mark.parametrize("name,options", [("mmlu", {"few_shot": 6}), ("gpqa", {"few_shot": 1}),
                                          ("mmlu", {"choice_seed": 1}), ("arc-agi", {"agent_timeout": 0}),
                                          ("gpqa", {"choice_seed": True}), ("mmlu", {"unknown": 1})])
def test_reasoning_runtime_options_are_not_silently_ignored(name, options):
    spec = _spec(name); spec["runtime_options"] = options
    assert not reasoning.preflight(spec)["reasoning_options"]["ok"]


def _mce_fixture(tmp_path):
    root = tmp_path / "native"; shutil.copytree(FIXTURES, root)
    (root / "__init__.py").write_text("")
    return root


def test_original_mce_scorers_preserve_partial_metrics(tmp_path):
    native = native_evaluator(_mce_fixture(tmp_path))
    result = mce_score(native, "USPTO", "CC.O", '{"final_answer":"CC"}')
    assert not result["was_correct"] and result["metrics"]["jaccard_similarity"] == 0.5
    result = mce_score(native, "LawBench", "盗窃;诈骗", "[罪名]盗窃<eoa>")
    assert not result["was_correct"] and result["metrics"]["f1"] == pytest.approx(2 / 3)
    assert mce_score(native, "Symptom2Disease", "diabetes", "[DIAGNOSIS]Diabetes.[/DIAGNOSIS]")["was_correct"]
    assert mce_score(native, "AEGIS", "unsafe", '{"final_answer":"unsafe"}')["was_correct"]
    assert mce_score(native, "FiNER", "Revenue", '{"final_answer":"revenue"}')["was_correct"]
    assert not mce_score(native, "FiNER", "", None)["was_correct"]


def test_original_mce_prompts_and_full_record_materialization(tmp_path, monkeypatch):
    source = _mce_fixture(tmp_path)
    (source / "crime_prediction").mkdir()
    for split in ("train", "val", "test"):
        (source / "crime_prediction" / f"{split}.jsonl").write_text(json.dumps({"question": "case facts", "answer": "罪名:synthetic_charge_one;synthetic_charge_two"}) + "\n")
    spec = _spec("mce-lawbench")
    tasks = [{"id": f"{split}-0001", "split": split, "record_id": 0} for split in ("train", "val", "test")]
    _source(spec, tasks); spec["source"]["split_counts"] = {split: 1 for split in ("train", "val", "test")}
    def copy(url, destination, sha):
        relative = url.split("/data/")[-1] if "/data/" in url else "LICENSE"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)
        return destination
    monkeypatch.setattr(mce, "download", copy)
    result = mce.prepare(spec, tmp_path / "generated")
    for row in result:
        root = Path(row["path"])
        prompt = (root / "instruction.md").read_text()
        assert "case facts" in prompt and "[罪名]" in prompt
        assert "synthetic_charge_one;synthetic_charge_two" not in prompt
        assert (root / "tests/native/evaluators.py").read_bytes() == (source / "evaluators.py").read_bytes()
        assert json.loads((root / "tests/expected.json").read_text())["target"] == "synthetic_charge_one;synthetic_charge_two"
