"""Transport DROP submissions to an unchanged official grader in the container."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def read_prediction(path: Path) -> tuple[str | list[str] | None, str | None]:
    if not path.is_file():
        return None, "missing answer.json"
    if path.stat().st_size > 1024 * 1024:
        return None, "answer.json exceeds 1 MiB"
    try:
        prediction = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError):
        return None, "answer.json is not valid UTF-8 JSON"
    if isinstance(prediction, str) or (
        isinstance(prediction, list) and prediction and all(isinstance(span, str) for span in prediction)
    ):
        return prediction, None
    return None, "answer.json must contain a string or a nonempty array of strings"


def grade(
    answer_path: Path, expected_path: Path, scorer_path: Path, output_dir: Path
) -> dict[str, Any]:
    expected = json.loads(expected_path.read_text())
    spec = importlib.util.spec_from_file_location("official_drop", scorer_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("official DROP scorer could not be loaded")
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    prediction, error = read_prediction(answer_path)
    # Missing or malformed transport uses the official missing-prediction branch.
    predictions = {} if prediction is None else {expected["query_id"]: prediction}
    exact_match, f1 = scorer.evaluate_json(expected["annotations"], predictions)
    rewards = {"reward": float(f1), "exact_match": float(exact_match), "f1": float(f1)}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "grading.json").write_text(json.dumps({
        "query_id": expected["query_id"], "split": expected["split"],
        "prediction": prediction, "submission_error": error,
        "metrics": rewards, "metric_scales": {"exact_match": [0, 1], "f1": [0, 1]},
    }, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "reward.json").write_text(json.dumps(rewards, allow_nan=False) + "\n")
    return rewards


def main() -> None:
    grade(Path("/app/answer.json"), Path("/tests/expected.json"),
          Path("/tests/official_drop.py"), Path("/logs/verifier"))


if __name__ == "__main__":
    main()
