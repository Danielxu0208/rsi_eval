"""Dispatch full EngDesign-Open and preserve the historical subset protocol."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from .common import download

SOURCE_COMMIT = "c8ec338cc5f846d792d93728b00c9448111139e5"
REPOSITORY = "https://github.com/AGI4Engineering/EngDesign"
_RAW = "https://raw.githubusercontent.com/AGI4Engineering/EngDesign"
_TASKS = {
    "AB_01": {"inputs": (), "grader_files": (),
              "requirements": ("numpy==2.2.4", "scipy==1.15.2", "matplotlib==3.10.1")},
    "XY_01": {"inputs": ("font_rom.sv",), "grader_files": ("font_rom_data.py",),
              "requirements": ()},
}

_GRADE = r'''"""Run the pinned upstream schema and grader on a JSON deliverable."""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def scalar(value):
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("upstream grader returned a nonnumeric score")
    if not math.isfinite(value):
        raise ValueError("upstream grader returned a nonfinite score")
    return value


def plain(value):
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if hasattr(value, "item"):
        return plain(value.item())
    return value


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def reject_constant(value):
    raise ValueError("nonfinite JSON number: " + value)


def grade(submission, tests_dir, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    # A retry must never inherit a previous result after verifier failure.
    for name in ("reward.json", "reward.txt", "verdict.json"):
        (output_dir / name).unlink(missing_ok=True)
    manifest = json.loads((tests_dir / "source.json").read_text())
    verdict = {"source": manifest, "status": "verifier_error"}
    try:
        upstream = tests_dir / "upstream"
        for name, expected in manifest["files"].items():
            actual = hashlib.sha256((upstream / name).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError("upstream verifier checksum mismatch: " + name)
        # Isolated Python excludes the agent's working directory and PYTHONPATH.
        sys.path.insert(0, str(upstream))
        schema = load("_engdesign_output_structure", upstream / "output_structure.py")
        evaluator = load("_engdesign_evaluator", upstream / "evaluate.py")
        from pydantic import ValidationError
        try:
            if submission.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("submission exceeds the 4 MiB JSON transport limit")
            raw = json.loads(submission.read_text(), parse_constant=reject_constant)
            response = schema.Response_structure.model_validate(raw)
        except (FileNotFoundError, IsADirectoryError, PermissionError,
                UnicodeError, ValueError, ValidationError) as error:
            verdict.update(status="invalid_submission", passed=False, native_score=None,
                           confidence=None, details=str(error))
            write(output_dir / "verdict.json", verdict)
            write(output_dir / "reward.json", {"reward": 0.0})
            return 0
        os.chdir(upstream)
        passed, details, score, confidence = evaluator.evaluate_llm_response(response)
        if type(passed) is not bool:
            raise ValueError("upstream grader did not return a boolean pass result")
        score = scalar(score)
        confidence = scalar(confidence)
        verdict.update(status="graded", passed=passed, native_score=score,
                       confidence=confidence, details=plain(details))
        write(output_dir / "verdict.json", verdict)
        # EngDesign reports pass rate and numerical score separately. The fourth
        # upstream return value is confidence, not a normalization denominator.
        write(output_dir / "reward.json", {"reward": float(passed),
                                           "native_score": score,
                                           "confidence": confidence})
        return 0
    except Exception as error:
        verdict.update(status="verifier_error", error=type(error).__name__ + ": " + str(error))
        write(output_dir / "verdict.json", verdict)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--submission", type=Path, default=Path("/workspace/output.json"))
    parser.add_argument("--tests-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output-dir", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    raise SystemExit(grade(args.submission.resolve(), args.tests_dir.resolve(), args.output_dir.resolve()))
'''

_ORACLE_AB = r'''"""Verifier smoke oracle using the upstream pixel-center convention."""
import json
from pathlib import Path
import sys
import numpy as np
from scipy.stats import skew, kurtosis

# The frozen evaluator samples pixel centers at index + 0.5, giving rows and
# columns 2 through 6. The upstream prose and solution.txt contain a different
# boundary interpretation. This fixture tests the actual frozen verifier.
values = np.array([15 + 2 * row + 3 * col for row in range(2, 7) for col in range(2, 7)], float)
result = {"reasoning": "Use the frozen evaluator's pixel-center convention.",
          "config": {"mean": float(values.mean()), "variance": float(values.var(ddof=1)),
                     "skewness": float(skew(values, bias=False)),
                     "kurtosis": float(kurtosis(values, fisher=True, bias=False)),
                     "contrast": 9.0}}
path = Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace/output.json")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(result) + "\n")
'''

_ORACLE_XY = r'''"""Verifier smoke oracle for the I tetromino at rotation 1."""
import json
from pathlib import Path
import sys

result = {"reasoning": "Read the four vertical cells from the task's I/1 ROM pattern.",
          "config": {"tetromino_type": "I", "rotation": 1},
          "tetromino_pattern": {"bit_grid": [[0, 1, 0, 0]] * 4,
                                 "visual": [".#.."] * 4}}
path = Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace/output.json")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(result) + "\n")
'''


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _source_file(source: dict[str, Any], relative: str, destination: Path) -> Path:
    expected = source.get("files", {}).get(relative)
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(f"EngDesign source requires a SHA-256 for {relative}")
    if destination.exists():
        if not destination.is_file() or destination.is_symlink():
            raise ValueError(f"invalid cached EngDesign source: {destination}")
        if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
            raise ValueError(f"cached EngDesign source checksum mismatch: {relative}")
        return destination
    return download(f"{_RAW}/{SOURCE_COMMIT}/{relative}", destination, expected)


def _task(source: dict[str, Any], task_id: str, source_dir: Path, destination: Path) -> None:
    definition = _TASKS[task_id]
    names = ("LLM_prompt.txt", "output_structure.py", "evaluate.py", "domain_topic.txt",
             *definition["inputs"], *definition["grader_files"])
    upstream = source_dir / task_id
    for name in names:
        _source_file(source, f"EngDesign-Open/{task_id}/{name}", upstream / name)
    if destination.exists():
        raise FileExistsError(f"EngDesign task output already exists: {destination}")
    destination.mkdir(parents=True)
    shutil.copyfile(source_dir / "LICENSE", destination / "LICENSE")
    public = ("output_structure.py", *definition["inputs"])
    for name in public:
        target = destination / "environment" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(upstream / name, target)
    instruction = (upstream / "LLM_prompt.txt").read_text()
    instruction += ("\n\n## Submission\n\n"
                    "Write your final answer as a JSON object to `/workspace/output.json`. "
                    "It must match `Response_structure` in `/workspace/output_structure.py`, "
                    "including its `reasoning` and task-specific fields. "
                    "The evaluator reads this file after your run ends.\n")
    if definition["inputs"]:
        instruction += "\nTask input files: " + ", ".join(f"`/workspace/{name}`" for name in definition["inputs"]) + ".\n"
    _write(destination / "instruction.md", instruction)
    requirements = "\n".join(("pydantic==2.11.3", *definition["requirements"])) + "\n"
    _write(destination / "environment" / "requirements.txt", requirements)
    copies = "\n".join(f"COPY {name} /workspace/{name}" for name in public)
    _write(destination / "environment" / "Dockerfile", f"""FROM python:3.12.9-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git procps \\
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/engdesign-requirements.txt
RUN pip install --no-cache-dir -r /tmp/engdesign-requirements.txt
RUN mkdir -p /workspace && chmod 0777 /workspace
{copies}
ENV MPLBACKEND=Agg
WORKDIR /workspace
""")
    _write(destination / "task.toml", f'''version = "1.0"

[metadata]
benchmark = "engdesign-open"
scope = "reviewed-open-subset"
record_id = "{task_id}"
source_commit = "{SOURCE_COMMIT}"
primary_metric = "native_passed"

[agent]
timeout_sec = 600.0

[verifier]
timeout_sec = 120.0

[environment]
build_timeout_sec = 900.0
cpus = 1
memory_mb = 2048
storage_mb = 4096
''')
    private = ("output_structure.py", "evaluate.py", *definition["grader_files"])
    for name in private:
        target = destination / "tests" / "upstream" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(upstream / name, target)
    provenance = {"repository": REPOSITORY, "commit": SOURCE_COMMIT, "record_id": task_id,
                  "files": {name: source["files"][f"EngDesign-Open/{task_id}/{name}"] for name in private}}
    _write(destination / "tests" / "source.json", json.dumps(provenance, indent=2) + "\n")
    _write(destination / "tests" / "grade.py", _GRADE)
    _write(destination / "tests" / "test.sh", "#!/bin/bash\nset -euo pipefail\npython -I /tests/grade.py\n")
    _write(destination / "solution" / "solve.py", _ORACLE_AB if task_id == "AB_01" else _ORACLE_XY)
    _write(destination / "solution" / "solve.sh", "#!/bin/bash\nset -euo pipefail\npython -I /solution/solve.py\n")
    for path in (destination / "tests" / "test.sh", destination / "solution" / "solve.sh"):
        path.chmod(0o755)


def prepare(spec: dict[str, Any], output_dir: Path) -> list[dict[str, str]]:
    """Fetch checksummed upstream files and return local Harbor task paths.

    Only catalogued, reviewed tasks are accepted. Existing source downloads can
    be reused after checksum validation; generated task directories must be new.
    """
    if spec.get("version") == "open-c8ec338-v1":
        from .engdesign_full import prepare as prepare_full
        return prepare_full(spec, output_dir)
    source = spec.get("source", {})
    if (spec.get("materializer") != "engdesign" or source.get("repository") != REPOSITORY
            or source.get("commit") != SOURCE_COMMIT):
        raise ValueError("EngDesign requires its reviewed official source commit")
    tasks = spec.get("tasks", [])
    if not tasks or len({task.get("id") for task in tasks}) != len(tasks):
        raise ValueError("EngDesign task selection must be nonempty and unique")
    for task in tasks:
        if task.get("id") not in _TASKS or task.get("record_id") != task["id"]:
            raise ValueError(f"unsupported EngDesign task: {task.get('id')}")
    root = Path(output_dir).resolve()
    source_dir = root / "source" / SOURCE_COMMIT
    _source_file(source, "LICENSE", source_dir / "LICENSE")
    result = []
    for task in tasks:
        destination = root / "tasks" / ("engdesign-" + task["id"].lower().replace("_", "-"))
        _task(source, task["id"], source_dir, destination)
        result.append({"id": task["id"], "path": str(destination)})
    return result


def preflight(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("version") == "open-c8ec338-v1":
        from .engdesign_full import preflight as check_full
        return check_full(spec)
    return {}
