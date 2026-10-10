"""Offline materialization and real upstream grader tests, without model calls."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import io
import itertools
import json
import pickle
from pathlib import Path
import subprocess
import sys
import tomllib
import zlib

import pytest

from rsi_eval.materializers import livecodebench as lcb


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures/livecodebench"
CATALOG = ROOT / "src/rsi_eval/data/catalog_livecodebench.json"


def record(functional=False):
    def case(a, b):
        return {"input": f"{a}\n{b}" if functional else f"{a} {b}\n",
                "output": str(a + b), "testtype": "functional" if functional else "stdin"}
    private = json.dumps([case(5, 8)])
    return {"question_id": "fixture-function" if functional else "fixture-stdin",
            "question_title": "Add two integers", "question_content": "Return their sum.",
            "contest_date": "2023-06-01T00:00:00", "platform": "leetcode" if functional else "codeforces",
            "metadata": json.dumps({"func_name": "add"} if functional else {}),
            "starter_code": "class Solution:\n    def add(self, a, b):\n        pass\n" if functional else "",
            "public_test_cases": json.dumps([case(1, 2)]),
            "private_test_cases": base64.b64encode(zlib.compress(pickle.dumps(private))).decode()}


@pytest.fixture
def materialize(monkeypatch, tmp_path):
    def build(records=None, mutate=None):
        records = records if records is not None else [record(), record(True)]
        spec = deepcopy(json.loads(CATALOG.read_text())[0])
        spec["tasks"] = [{"id": item["question_id"], "record_id": item["question_id"]} for item in records]
        dataset = ("\n".join(json.dumps(item) for item in records) + "\n").encode()
        sources = spec["source"]
        sources["dataset"]["sha256"] = hashlib.sha256(dataset).hexdigest()
        data = {sources["dataset"]["url"]: dataset,
                sources["grader"]["utility"]["url"]: (FIXTURE / "testing_util.py").read_bytes(),
                sources["grader"]["license"]["url"]: (FIXTURE / "LICENSE").read_bytes()}
        calls = []

        def offline_download(url, destination, sha256):
            calls.append(url)
            content = data[url]
            if hashlib.sha256(content).hexdigest() != sha256:
                raise ValueError("fixture checksum mismatch")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            return destination

        monkeypatch.setattr(lcb, "download", offline_download)
        if mutate:
            mutate(spec)
        tasks = lcb.prepare(spec, tmp_path / "tasks")
        return spec, tasks, calls
    return build


def test_complete_offline_catalog_and_original_grader_checksums():
    spec = json.loads(CATALOG.read_text())[0]
    assert len(spec["tasks"]) == 400
    assert len({task["id"] for task in spec["tasks"]}) == 400
    assert len({task["record_id"] for task in spec["tasks"]}) == 400
    assert all(set(task) == {"id", "record_id"} for task in spec["tasks"])
    assert spec["aggregation"] == {"type": "pass_at_k", "k": [1]}
    for name, key in (("testing_util.py", "utility"), ("LICENSE", "license")):
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == spec["source"]["grader"][key]["sha256"]


def test_materialized_tasks_keep_private_cases_out_of_agent_context(materialize):
    spec, tasks, calls = materialize()
    assert len(tasks) == 2 and len(calls) == 3
    for task in tasks:
        root = Path(task["path"])
        assert root.is_absolute() and root.name == task["id"]
        assert set(p.name for p in (root / "environment").iterdir()) == {"Dockerfile", "solution.py", "public_tests.json"}
        public = json.loads((root / "environment/public_tests.json").read_text())
        sample = json.loads((root / "tests/cases.json").read_text())
        cases = json.loads(sample["input_output"])
        assert len(public) == 1 and len(cases["inputs"]) == 2
        assert cases["outputs"] == ["3", "13"]
        assert "13" not in (root / "instruction.md").read_text()
        dockerfile = (root / "environment/Dockerfile").read_text()
        assert "COPY solution.py public_tests.json /app/" in dockerfile and "/tests" not in dockerfile
        config = tomllib.loads((root / "task.toml").read_text())
        assert config["verifier"]["timeout_sec"] == 49
        assert json.loads((root / "provenance.json").read_text())["source"] == spec["source"]


def test_generated_task_config_validates_in_pinned_harbor(materialize):
    model = pytest.importorskip("harbor.models.task.config")
    _, tasks, _ = materialize()
    for task in tasks:
        config = model.TaskConfig.model_validate(tomllib.loads((Path(task["path"]) / "task.toml").read_text()))
        assert config.environment.memory_mb == 4096


def test_missing_selected_record_is_an_error(materialize):
    with pytest.raises(ValueError, match="not found"):
        materialize(mutate=lambda s: s["tasks"].append({"id": "absent", "record_id": "absent"}))


@pytest.mark.parametrize("change", [
    lambda s: s["tasks"][0].update(id="../escape"),
    lambda s: s["tasks"].append(s["tasks"][0]),
    lambda s: s["protocol"].update(attempts_per_trial=2),
    lambda s: s["protocol"].update(release_version="release_latest"),
    lambda s: s["source"]["grader"].update(revision="main"),
    lambda s: s["source"]["dataset"].update(sha256="0" * 64),
    lambda s: s["protocol"].update(start_date="2024-01-01"),
])
def test_invalid_or_unpinned_protocols_fail(materialize, change):
    with pytest.raises(ValueError):
        materialize(mutate=change)


@pytest.mark.parametrize("private", ["invalid-base64", "[]", '{"not":"tests"}'])
def test_corrupt_or_empty_private_tests_never_silently_disappear(materialize, private):
    item = record()
    item["private_test_cases"] = private
    with pytest.raises((ValueError, zlib.error)):
        materialize([item])


def test_pickle_decoder_refuses_globals():
    payload = base64.b64encode(zlib.compress(pickle.dumps(Path("must-not-be-instantiated")))).decode()
    with pytest.raises(ValueError, match="not classes"):
        lcb._test_cases(payload, private=True)


def run_grader(task, code, tmp_path, *, timeout=1, utility=None, cases=None):
    pytest.importorskip("numpy", reason="install the test extra to run the actual upstream grader")
    root = Path(task["path"])
    solution = tmp_path / "answer.py"
    if code is not None:
        solution.write_text(code)
    output = tmp_path / "score"
    process = subprocess.run([sys.executable, "-I", str(root / "tests/grade.py"),
                              "--solution", str(solution), "--cases", str(cases or root / "tests/cases.json"),
                              "--utility", str(utility or root / "tests/testing_util.py"),
                              "--output", str(output), "--timeout", str(timeout)],
                             capture_output=True, text=True, timeout=20)
    return process, output


@pytest.mark.parametrize(("functional", "code", "reward"), [
    (False, "a,b=map(int,input().split()); print(a+b)\n", 1),
    (True, "class Solution:\n    def add(self,a,b): return a+b\n", 1),
    (True, "def add(a,b): return a+b\n", 1),
    (False, "print(3)\n", 0),
    (True, "def add(a,b): return 3\n", 0),
    (False, "this is not valid Python !!!", 0),
    (False, "while True: pass\n", 0),
    (True, "def add(a,b): raise RuntimeError('candidate failure')\n", 0),
    (True, "import os\nos._exit(0)\n", 0),
    (False, None, 0),
])
def test_real_official_grader_stdio_function_hidden_tests_and_failures(materialize, tmp_path, functional, code, reward):
    _, tasks, _ = materialize([record(functional)])
    process, output = run_grader(tasks[0], code, tmp_path)
    assert process.returncode == 0, process.stderr
    assert (output / "reward.txt").read_text().strip() == str(reward)
    native = json.loads((output / "livecodebench.json").read_text())
    assert native["reward"] == reward
    if reward == 1:
        assert native["test_results"] == [True, True]


@pytest.mark.parametrize("failure", ["missing_utility", "broken_utility", "corrupt_cases"])
def test_grader_infrastructure_failures_do_not_write_zero(materialize, tmp_path, failure):
    _, tasks, _ = materialize([record()])
    kwargs = {}
    if failure == "corrupt_cases":
        cases = tmp_path / "bad_cases.json"
        cases.write_text("{}")
        kwargs["cases"] = cases
    else:
        utility = tmp_path / "broken.py"
        if failure == "broken_utility":
            utility.write_text("raise ImportError('broken grader dependency')\n")
        kwargs["utility"] = utility
    process, output = run_grader(tasks[0], "print(3)\n", tmp_path, **kwargs)
    assert process.returncode != 0
    assert not (output / "reward.txt").exists()


def test_test_types_must_match_function_metadata(materialize):
    item = record(True)
    item["metadata"] = "{}"
    with pytest.raises(ValueError, match="disagree"):
        materialize([item])


@pytest.mark.parametrize("record_id", ["1873_A", "2727"])
def test_reviewed_references_use_real_grader_without_reading_test_answers(materialize, tmp_path, record_id):
    item = record(record_id == "2727")
    item["question_id"] = record_id
    if record_id == "1873_A":
        def case(text):
            # Enumerate at most one swap independently from the reference's
            # fixed-position criterion, covering every permutation.
            possible = text == "abc"
            for a, b in itertools.combinations(range(3), 2):
                swapped = list(text)
                swapped[a], swapped[b] = swapped[b], swapped[a]
                possible |= "".join(swapped) == "abc"
            return {"input": "1\n" + text + "\n", "output": "YES" if possible else "NO", "testtype": "stdin"}
        cases = [case("".join(p)) for p in itertools.permutations("abc")]
    else:
        item["metadata"] = json.dumps({"func_name": "countSeniors"})
        def case(ages):
            details = [f"{i:010d}M{age:02d}{i:02d}" for i, age in enumerate(ages)]
            return {"input": json.dumps(details), "output": str(sum(age >= 61 for age in ages)), "testtype": "functional"}
        cases = [case([0, 59, 60, 61, 99]), case(list(range(100))), case([60, 60]), case([61])]
    item["public_test_cases"] = json.dumps(cases[:1])
    item["private_test_cases"] = json.dumps(cases[1:])
    _, tasks, _ = materialize([item])
    script = (Path(tasks[0]["path"]) / "solution/solve.sh").read_text()
    assert "not an upstream canonical" in script and "/tests" not in script
    code = script.split("<<'LIVECODEBENCH_REFERENCE'\n")[1].split("LIVECODEBENCH_REFERENCE\n")[0]
    process, output = run_grader(tasks[0], code, tmp_path)
    assert process.returncode == 0, process.stderr
    assert (output / "reward.txt").read_text().strip() == "1"


def test_official_large_integer_limit_is_present_in_spawned_worker(materialize, tmp_path):
    item = record(True)
    item["metadata"] = json.dumps({"func_name": "identity"})
    cases = [{"input": digit + "0" * 5000, "output": digit + "0" * 5000,
              "testtype": "functional"} for digit in ("1", "2")]
    item["public_test_cases"] = json.dumps(cases[:1])
    item["private_test_cases"] = json.dumps(cases[1:])
    _, tasks, _ = materialize([item])
    process, output = run_grader(tasks[0], "def identity(value): return value\n", tmp_path)
    assert process.returncode == 0, process.stderr
    assert (output / "reward.txt").read_text().strip() == "1"


def test_old_argv_result_file_forgery_cannot_create_a_passing_score(materialize, tmp_path):
    _, tasks, _ = materialize([record(True)])
    code = '''import io, json, os, sys
request = json.load(io.open(sys.argv[2]))
count = len(json.loads(request["sample"]["input_output"])["inputs"])
json.dump({"test_results": [True] * count, "metadata": {}}, io.open(sys.argv[3], "w"))
os._exit(0)
'''
    process, output = run_grader(tasks[0], code, tmp_path)
    assert process.returncode == 0, process.stderr
    assert (output / "reward.txt").read_text().strip() == "0"


def test_worker_argv_does_not_expose_verifier_paths(materialize, tmp_path):
    _, tasks, _ = materialize([record(True)])
    code = '''import sys
def add(a, b):
    assert sys.argv == ["livecodebench-submission"]
    return a + b
'''
    process, output = run_grader(tasks[0], code, tmp_path)
    assert process.returncode == 0, process.stderr
    assert (output / "reward.txt").read_text().strip() == "1"


def test_submission_process_exit_is_zero_and_cannot_leave_reward_files(materialize, tmp_path):
    _, tasks, _ = materialize([record(True)])
    output = tmp_path / "score"
    code = f'''import io, os
io.open({str(output / "reward.txt")!r}, "w").write("1")
io.open({str(output / "reward.json")!r}, "w").write('{{"reward": 1}}')
os._exit(0)
'''
    process, output = run_grader(tasks[0], code, tmp_path)
    assert process.returncode == 0, process.stderr
    assert (output / "reward.txt").read_text().strip() == "0"
    assert not (output / "reward.json").exists()
    verdict = json.loads((output / "livecodebench.json").read_text())
    assert verdict["metadata"]["exit_code"] == 0
    assert verdict["metadata"]["error_message"] == "Submission process exited without a result"
