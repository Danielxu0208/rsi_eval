"""Run inside the verifier container; gold answers are uploaded after solving."""
from __future__ import annotations

import json
from pathlib import Path


def score(target: str, prediction: str) -> bool:
    """ADAS numeric-string comparison after stripping file transport whitespace."""
    prediction = prediction.strip()
    if "." in prediction:
        prediction = prediction.rstrip("0").rstrip(".")
    return target.replace(",", "") == prediction.replace(",", "")


def main() -> None:
    expected = json.loads(Path("/tests/expected.json").read_text())
    answer = Path("/app/answer.txt")
    prediction = None
    if answer.is_file():
        try:
            prediction = answer.read_text(encoding="utf-8")
        except UnicodeError:
            pass
    passed = prediction is not None and score(expected["answer"], prediction)
    output = Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    (output / "grading.json").write_text(json.dumps({
        "prediction": prediction, "expected": expected["answer"],
        "language": expected["language"], "correct": passed}, ensure_ascii=False) + "\n")
    (output / "reward.json").write_text(json.dumps({"reward": int(passed), "exact_match": int(passed)}) + "\n")


if __name__ == "__main__":
    main()
