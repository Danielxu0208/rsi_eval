"""Materialize pinned LiveCodeBench code-generation problems as Harbor tasks.

Only the official grading utility is downloaded. Model generation remains the
caller's agent, and each Harbor trial submits one Python program.
"""
from __future__ import annotations

import base64
import io
import json
import pickle
import re
import shutil
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import download


# These reviewed general solutions validate the integration. The dataset does
# not publish canonical solutions, so these must never be labelled official.
_INTEGRATION_REFERENCES = {
    "1873_A": """import sys
words = iter(sys.stdin.read().split())
for _ in range(int(next(words))):
    text = next(words)
    print("YES" if any(a == b for a, b in zip(text, "abc")) else "NO")
""",
    "2727": """class Solution:
    def countSeniors(self, details):
        return sum(int(detail[11:13]) > 60 for detail in details)
""",
}


class _StringUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        raise ValueError("LiveCodeBench private-test pickle must contain data, not classes")


def _test_cases(value: str, *, private: bool) -> list[dict[str, Any]]:
    """Decode upstream JSON or its compressed, pickled JSON string format."""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        if not private:
            raise
        raw = zlib.decompress(base64.b64decode(value, validate=True))
        unpacked = _StringUnpickler(io.BytesIO(raw)).load()
        if not isinstance(unpacked, str):
            raise ValueError("LiveCodeBench private-test payload is not a JSON string")
        decoded = json.loads(unpacked)
    if not isinstance(decoded, list) or not decoded:
        raise ValueError("LiveCodeBench test cases must be a nonempty list")
    for case in decoded:
        if not isinstance(case, dict) or not all(isinstance(case.get(k), str) for k in ("input", "output")):
            raise ValueError("LiveCodeBench test case requires string input and output")
        if case.get("testtype") not in {"stdin", "functional"}:
            raise ValueError("unsupported LiveCodeBench test type")
    return decoded


def _evaluation_sample(record: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    public = _test_cases(record["public_test_cases"], private=False)
    private = _test_cases(record["private_test_cases"], private=True)
    metadata = json.loads(record["metadata"])
    function = metadata.get("func_name")
    if function is not None and (not isinstance(function, str) or not function.isidentifier()):
        raise ValueError("invalid LiveCodeBench function name")
    expected_type = "functional" if function is not None else "stdin"
    cases = public + private
    if any(case["testtype"] != expected_type for case in cases):
        raise ValueError("LiveCodeBench metadata and test types disagree")
    # This matches CodeGenerationProblem.get_evaluation_sample upstream. Neither
    # malformed private tests nor unsupported test types are silently discarded.
    return {"input_output": json.dumps({
        "inputs": [case["input"] for case in cases],
        "outputs": [case["output"] for case in cases],
        "fn_name": function,
    })}, public


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _source_file(source: dict[str, Any], destination: Path) -> Path:
    digest = source.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("LiveCodeBench source requires a SHA256 digest")
    return download(source["url"], destination, digest)


def _instruction(record: dict[str, Any], function: str | None) -> str:
    interface = (
        "Implement the supplied function interface, including the Solution class when present."
        if function is not None else
        "Read from standard input and write the answer to standard output."
    )
    return (
        f"# {record['question_title']}\n\n{record['question_content']}\n\n"
        "## Submission\n\n"
        "Write your final Python 3 solution to `/app/solution.py`. "
        f"{interface} Your final chat message is not the submission.\n\n"
        "Public examples are in `/app/public_tests.json`. You may run local checks. "
        "The verifier evaluates one final program against all public and private tests. "
        "It provides no private-test feedback during this attempt.\n"
    )


def prepare(spec: dict[str, Any], output_dir: Path) -> list[dict[str, str]]:
    """Download and verify sources, then create only the selected Harbor tasks."""
    source, protocol = spec["source"], spec["protocol"]
    if protocol.get("scenario") != "codegeneration" or protocol.get("language") != "python":
        raise ValueError("this materializer supports Python codegeneration only")
    if protocol.get("attempts_per_trial") != 1:
        raise ValueError("LiveCodeBench requires one submitted program per trial")
    if protocol.get("release_version") != "release_v1":
        raise ValueError("this materializer is pinned to release_v1")
    timeout = protocol["per_test_timeout_sec"]
    if type(timeout) is not int or timeout < 1:
        raise ValueError("per_test_timeout_sec must be a positive integer")
    for item in (source, source["grader"]):
        if not re.fullmatch(r"[0-9a-f]{40}", item.get("revision", "")):
            raise ValueError("LiveCodeBench sources require full immutable revisions")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    tasks = spec["tasks"]
    selected: dict[str, dict[str, str]] = {}
    for task in tasks:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", task["id"]):
            raise ValueError("invalid LiveCodeBench task ID")
        if not isinstance(task.get("record_id"), str) or task["record_id"] in selected:
            raise ValueError("duplicate or missing LiveCodeBench record ID")
        if (output_dir / task["id"]).exists():
            raise FileExistsError(output_dir / task["id"])
        selected[task["record_id"]] = task
    if not selected:
        raise ValueError("LiveCodeBench selection is empty")
    sources = output_dir / ".sources"
    data = _source_file(source["dataset"], sources / "test.jsonl")
    grader = _source_file(source["grader"]["utility"], sources / "testing_util.py")
    license_file = _source_file(source["grader"]["license"], sources / "LICENSE")
    start, end = datetime.fromisoformat(protocol["start_date"]), datetime.fromisoformat(protocol["end_date"])
    records: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    with data.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            record_id = record["question_id"]
            if record_id in seen:
                raise ValueError(f"duplicate LiveCodeBench dataset ID: {record_id}")
            seen.add(record_id)
            if record_id in selected:
                if not start <= datetime.fromisoformat(record["contest_date"]) <= end:
                    raise ValueError(f"LiveCodeBench task outside pinned date range: {record_id}")
                records[record_id] = record
    if records.keys() != selected.keys():
        raise ValueError(f"LiveCodeBench records not found: {sorted(selected.keys() - records.keys())}")
    result = []
    for record_id, task in selected.items():
        record = records[record_id]
        sample, public = _evaluation_sample(record)
        cases = json.loads(sample["input_output"])
        root = output_dir / task["id"]
        environment, tests = root / "environment", root / "tests"
        environment.mkdir(parents=True)
        tests.mkdir()
        (root / "instruction.md").write_text(_instruction(record, cases["fn_name"]), encoding="utf-8")
        (environment / "solution.py").write_text(record["starter_code"] or "# Write your solution here.\n", encoding="utf-8")
        _write_json(environment / "public_tests.json", public)
        (environment / "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")
        _write_json(tests / "cases.json", sample)
        shutil.copyfile(grader, tests / "testing_util.py")
        shutil.copyfile(license_file, tests / "LIVECODEBENCH_LICENSE")
        (tests / "grade.py").write_text(_GRADER, encoding="utf-8")
        (tests / "test.sh").write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            f"/opt/lcb/bin/python -I /tests/grade.py --timeout {timeout}\n",
            encoding="utf-8",
        )
        (tests / "test.sh").chmod(0o755)
        _write_json(root / "provenance.json", {
            "benchmark": spec["name"], "version": spec["version"],
            "task_id": task["id"], "record_id": record_id, "source": source,
            "protocol": protocol, "public_test_count": len(public),
            "private_test_count": len(cases["inputs"]) - len(public),
        })
        # Match the upstream outer correctness timeout, with room for process
        # startup and evidence collection in Harbor's verifier phase.
        verifier_timeout = (timeout + 1) * len(cases["inputs"]) + 35
        (root / "task.toml").write_text(
            'version = "1.0"\n\n[metadata]\n'
            f'benchmark = "livecodebench"\nrecord_id = {json.dumps(record_id)}\n\n'
            f'[agent]\ntimeout_sec = {protocol["agent_timeout_sec"]}\n\n'
            f'[verifier]\ntimeout_sec = {verifier_timeout}\n\n'
            '[environment]\nbuild_timeout_sec = 600\ncpus = 1\nmemory_mb = 4096\nstorage_mb = 4096\n',
            encoding="utf-8",
        )
        if record_id in _INTEGRATION_REFERENCES:
            solution = root / "solution"
            solution.mkdir()
            (solution / "solve.sh").write_text(
                "#!/bin/bash\nset -euo pipefail\n"
                "# Integration reference, not an upstream canonical solution.\n"
                "cat > /app/solution.py <<'LIVECODEBENCH_REFERENCE'\n"
                + _INTEGRATION_REFERENCES[record_id]
                + "LIVECODEBENCH_REFERENCE\n", encoding="utf-8",
            )
            (solution / "solve.sh").chmod(0o755)
        result.append({"id": task["id"], "path": str(root)})
    return result


_DOCKERFILE = """FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git \\
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/lcb && /opt/lcb/bin/pip install --no-cache-dir numpy==2.2.6
WORKDIR /app
COPY solution.py public_tests.json /app/
"""


# This wrapper supplies process isolation and Harbor's reward file. All answer
# comparison and per-test timeouts remain in the unmodified official utility.
# Like upstream, the utility and submission share one worker interpreter. The
# pipe removes a writable result-file protocol; it is not a hostile-code sandbox.
_GRADER = r'''"""Run one final program through the pinned official LiveCodeBench grader."""
import argparse
import importlib.util
import json
import multiprocessing
import os
import sys
import tempfile
import time
from pathlib import Path

sys.set_int_max_str_digits(50000)


def worker(sample, code, utility_path, timeout, directory, connection):
    # Match the official compute_code_generation_metrics module in both the
    # parent and this spawned interpreter. Do not inherit verifier CLI paths as
    # a submission's argv, and do not put outcomes in candidate-writable files.
    sys.set_int_max_str_digits(50000)
    sys.argv = ["livecodebench-submission"]
    try:
        os.chdir(directory)
        module_spec = importlib.util.spec_from_file_location("lcb_official", utility_path)
        utility = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(utility)
        connection.send_bytes(b'{"kind":"ready"}')
        try:
            tests, metadata = utility.run_test(sample, test=code, debug=False, timeout=timeout)
        except (SystemExit, KeyboardInterrupt) as error:
            tests, metadata = [-4], {"error_code": -4, "error_message": type(error).__name__}
        # Match evaluate_generations_by_problem's NumPy scalar normalization.
        normalized = []
        for value in tests:
            if isinstance(value, utility.np.ndarray):
                value = value.item(0)
            if isinstance(value, utility.np.bool_):
                value = bool(value)
            normalized.append(value)
        connection.send_bytes(json.dumps({"kind": "result", "outcome": {
            "test_results": normalized, "metadata": metadata}}).encode())
    except BaseException as error:
        connection.send_bytes(json.dumps({"kind": "error", "message": repr(error)}).encode())
    finally:
        connection.close()


def failed_submission(message, error_code=-4, **detail):
    return {"test_results": [error_code], "metadata": {
        "error_code": error_code, "error_message": message, **detail}, "reward": 0}


def grade(solution, cases_path, utility, timeout):
    # Validate grader inputs before touching the candidate. Missing/corrupt test
    # data is infrastructure failure, even when the candidate submitted no file.
    sample = json.loads(cases_path.read_text())
    cases = json.loads(sample["input_output"])
    if not cases["inputs"] or len(cases["inputs"]) != len(cases["outputs"]):
        raise ValueError("invalid verifier cases")
    if not utility.is_file():
        raise FileNotFoundError("missing official grader")
    if not solution.is_file() or solution.is_symlink():
        return {"test_results": [False], "metadata": {"error_message": "Missing submission"}, "reward": 0}
    try:
        code = solution.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"test_results": [False], "metadata": {"error_message": "Invalid UTF-8 submission"}, "reward": 0}
    with tempfile.TemporaryDirectory(prefix="lcb-verifier-") as temporary:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=worker, args=(sample, code, str(utility.resolve()),
                                  timeout, temporary, sender))
        process.start()
        sender.close()
        ready, outcome = False, None
        deadline = time.monotonic() + (timeout + 1) * len(cases["inputs"]) + 5
        try:
            while outcome is None:
                if not receiver.poll(max(0, deadline - time.monotonic())):
                    if ready:
                        return failed_submission("Global time limit exceeded", -3)
                    raise RuntimeError("LiveCodeBench worker failed to initialize before its deadline")
                try:
                    message = json.loads(receiver.recv_bytes())
                except EOFError:
                    process.join(timeout=1)
                    if ready:
                        return failed_submission("Submission process exited without a result",
                                                 exit_code=process.exitcode)
                    raise RuntimeError("LiveCodeBench worker exited before grader initialization")
                if message.get("kind") == "ready" and not ready:
                    ready = True
                elif message.get("kind") == "result" and ready:
                    outcome = message["outcome"]
                elif message.get("kind") == "error":
                    raise RuntimeError("LiveCodeBench grader process failed: " + message["message"])
                else:
                    raise ValueError("invalid LiveCodeBench worker message")
        finally:
            receiver.close()
            if process.is_alive():
                process.kill()
            process.join()
    results = outcome["test_results"]
    if not isinstance(results, list) or not results or any(type(v) not in (bool, int) for v in results):
        raise ValueError("invalid official grader result")
    if len(results) > len(cases["inputs"]):
        raise ValueError("official grader returned too many test results")
    outcome["reward"] = int(len(results) == len(cases["inputs"]) and all(v is True or v == 1 for v in results))
    return outcome


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--solution", type=Path, default=Path("/app/solution.py"))
    parser.add_argument("--cases", type=Path, default=Path("/tests/cases.json"))
    parser.add_argument("--utility", type=Path, default=Path("/tests/testing_util.py"))
    parser.add_argument("--output", type=Path, default=Path("/logs/verifier"))
    parser.add_argument("--timeout", type=int, default=6)
    args = parser.parse_args()
    if args.timeout < 1:
        raise ValueError("timeout must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    reward = args.output / "reward.txt"
    reward.unlink(missing_ok=True)
    (args.output / "reward.json").unlink(missing_ok=True)
    try:
        outcome = grade(args.solution, args.cases, args.utility, args.timeout)
    finally:
        # Only the parent publishes a reward after successful verification.
        # Remove files that a failed submission may have written itself.
        reward.unlink(missing_ok=True)
        (args.output / "reward.json").unlink(missing_ok=True)
    (args.output / "livecodebench.json").write_text(json.dumps(outcome, indent=2) + "\n")
    reward.write_text(str(outcome["reward"]) + "\n")


if __name__ == "__main__":
    main()
'''
