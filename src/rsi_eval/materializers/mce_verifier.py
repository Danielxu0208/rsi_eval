"""Invoke the pinned Meta-Harness MCE evaluator without changing its parsing rules."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


def native_evaluator(root: Path):
    # A private package retains original relative imports and source bytes.
    package_name = "rsi_mce_native"
    spec = importlib.util.spec_from_file_location(package_name, root / "__init__.py",
                                                submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    spec = importlib.util.spec_from_file_location(package_name + ".evaluators", root / "evaluators.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.get_evaluator


def score(get_evaluator, task: str, target: str, prediction: str | None) -> dict:
    if prediction is None:
        return {"was_correct": False, "metrics": {}, "missing_submission": True}
    result = get_evaluator(task)(prediction, target)
    if isinstance(result, bool):
        return {"was_correct": result, "metrics": {}}
    if not isinstance(result, dict) or type(result.get("was_correct")) is not bool:
        raise ValueError("unexpected original MCE grader result")
    return result


def main() -> None:
    expected = json.loads(Path("/tests/expected.json").read_text())
    path = Path("/app/answer.txt")
    prediction = None
    try:
        if path.is_file() and path.stat().st_size <= 4 * 1024 * 1024:
            prediction = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    result = score(native_evaluator(Path("/tests/native")), expected["dataset"], expected["target"], prediction)
    output = Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    (output / "grading.json").write_text(json.dumps({"task_id": expected["task_id"], "prediction": prediction,
        "expected": expected["target"], **result}, ensure_ascii=False, allow_nan=False) + "\n")
    (output / "reward.json").write_text(json.dumps({"reward": int(result["was_correct"]),
        "exact_match": int(result["was_correct"]), **result["metrics"]}, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
