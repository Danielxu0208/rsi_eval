"""File submission transport for exact choice accuracy and ARC's three-trial rule."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _grid(value: Any) -> bool:
    return (isinstance(value, list) and 1 <= len(value) <= 30
            and all(isinstance(row, list) and 1 <= len(row) <= 30 for row in value)
            and len({len(row) for row in value}) == 1
            and all(type(cell) is int and 0 <= cell <= 9 for row in value for cell in row))


def score(expected: dict, prediction: str | None) -> dict:
    if expected["kind"] == "choice":
        answer = prediction.strip() if prediction is not None else None
        correct = answer in ("A", "B", "C", "D") and answer == expected["answer"]
        return {"correct": correct, "prediction": answer, "expected": expected["answer"]}
    if expected["kind"] != "arc":
        raise ValueError("unknown grading protocol")
    try:
        value = json.loads(prediction) if prediction is not None else None
        if not isinstance(value, dict) or set(value) != {"predictions"}:
            raise ValueError("answer must contain only a predictions list")
        attempts = value["predictions"]
        if not isinstance(attempts, list) or len(attempts) != len(expected["outputs"]):
            raise ValueError("one attempt list is required per test input")
        if any(not isinstance(trials, list) or not 1 <= len(trials) <= 3
               or any(not _grid(grid) for grid in trials) for trials in attempts):
            raise ValueError("each test input permits one to three 1..30 by 1..30 integer grids")
    except (ValueError, TypeError) as error:
        return {"correct": False, "valid_submission": False, "error": str(error)}
    passed = [gold in trials for gold, trials in zip(expected["outputs"], attempts)]
    return {"correct": all(passed), "valid_submission": True,
            "test_inputs_correct": passed, "attempts_per_input": [len(x) for x in attempts],
            "prediction": attempts, "expected": expected["outputs"]}


def main() -> None:
    expected = json.loads(Path("/tests/expected.json").read_text())
    path = Path("/app/answer.json" if expected["kind"] == "arc" else "/app/answer.txt")
    prediction = None
    try:
        if path.is_file() and path.stat().st_size <= 4 * 1024 * 1024:
            prediction = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    detail = score(expected, prediction)
    detail["task_id"] = expected["task_id"]
    output = Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    (output / "grading.json").write_text(json.dumps(detail, ensure_ascii=False, allow_nan=False) + "\n")
    (output / "reward.json").write_text(json.dumps({"reward": int(detail["correct"]),
                                                    "exact_match": int(detail["correct"])}) + "\n")


if __name__ == "__main__":
    main()
