"""Harbor verifier transport around the unchanged, separately installed LAB scorer."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import traceback


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_scores(scores: dict, judges: list[str], expected_n: int) -> dict[str, float]:
    """Use native task all-pass values; reject absent or failed judge criteria."""
    dual = len(judges) == 2
    per_judge = scores.get("per_judge", {}) if dual else {judges[0]: scores}
    if set(per_judge) != set(judges):
        raise RuntimeError("native judge results are missing")
    for model, row in per_judge.items():
        results = row.get("criteria_results")
        if not isinstance(results, list) or len(results) != expected_n or row.get("n_criteria") != expected_n:
            raise RuntimeError("native judge returned an incomplete criterion set: " + model)
        if row.get("n_grading_errors") or any(c.get("verdict") not in ("pass", "fail") for c in results):
            raise RuntimeError("native judge grading error; no benchmark reward is emitted")
        passed = sum(c["verdict"] == "pass" for c in results)
        if row.get("n_passed") != passed or row.get("score") != float(passed == expected_n):
            raise RuntimeError("native judge score is inconsistent with its verdicts")
    reward = scores["dual_all_pass_rate"] if dual else scores["score"]
    criterion_rate = scores["dual_criterion_pass"] if dual else scores["n_passed"] / expected_n
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in (reward, criterion_rate)):
        raise RuntimeError("native judge returned invalid aggregate metrics")
    expected_reward = sum(row["score"] for row in per_judge.values()) / len(judges)
    expected_rate = sum(row["n_passed"] / expected_n for row in per_judge.values()) / len(judges)
    if reward != expected_reward or criterion_rate != expected_rate:
        raise RuntimeError("native judge aggregate differs from native per-judge results")
    return {"reward": reward, "criterion_pass_rate": criterion_rate, "strict_all_pass": float(reward == 1),
            "n_criteria": expected_n * len(judges),
            "n_passed": sum(row["n_passed"] for row in per_judge.values()), "judge_count": len(judges)}


def output_manifest(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("Harbor output artifact is missing or not a directory")
    manifest = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise RuntimeError("output artifact contains an unsafe non-regular entry")
        if path.is_file():
            manifest[path.relative_to(root).as_posix()] = sha256(path)
    return manifest


def _redact(text: str) -> str:
    for key, value in os.environ.items():
        if any(part in key for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")) and len(value) >= 6:
            text = text.replace(value, "[REDACTED]")
    return text


def main(config_path: Path = Path("/tests/config.json"), output: Path = Path("/workspace/output"),
         logs: Path = Path("/logs/verifier"), root: Path = Path("/opt/harvey")) -> int:
    logs.mkdir(parents=True, exist_ok=True)
    reward_file = logs / "reward.json"
    reward_file.unlink(missing_ok=True)
    cfg = json.loads(config_path.read_text())
    run_dir = root / "results/rsi-eval"
    try:
        for relative, expected in cfg["core_sha256"].items():
            if sha256(root / relative) != expected:
                raise RuntimeError("native LAB source checksum mismatch: " + relative)
        task_path = root / "tasks" / cfg["task_id"] / "task.json"
        if sha256(task_path) != cfg["task_sha256"]:
            raise RuntimeError("native LAB rubric checksum mismatch")
        manifest = output_manifest(output)
        run_dir.mkdir(parents=True, exist_ok=False)
        shutil.copytree(output, run_dir / "output")
        (logs / "output-sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
        from lab_core.evaluation.run_eval import evaluate_run, evaluate_run_dual
        from lab_core.evaluation.judge import Judge
        from lab_core.evaluation.report import generate_report
        os.environ["LAB_ROOT"] = str(root)
        if len(cfg["judges"]) == 2:
            scores = evaluate_run_dual("rsi-eval", cfg["task_id"], parallel=cfg["parallel"], judge_models=tuple(cfg["judges"]))
        else:
            scores = evaluate_run("rsi-eval", cfg["task_id"], Judge(model=cfg["judges"][0]), parallel=cfg["parallel"])
        metrics = validate_scores(scores, cfg["judges"], cfg["expected_n_criteria"])
        generate_report(run_id="rsi-eval")
        (logs / "harvey-lab.json").write_text(json.dumps({"source_commit": cfg["source_commit"],
            "task_id": cfg["task_id"], "judges": cfg["judges"], "native_scores": scores,
            "documents_sha256": cfg["document_tree_sha256"], "output_sha256": manifest,
            "judge_usage": None, "judge_usage_note": "The pinned native Judge does not expose provider usage; unknown is not zero."}, indent=2) + "\n")
        reward_file.write_text(json.dumps(metrics, allow_nan=False) + "\n")
        return 0
    except Exception as error:
        message = _redact(type(error).__name__ + ": " + str(error))
        (logs / "grading-error.json").write_text(json.dumps({"error": message, "source_commit": cfg["source_commit"], "reward_emitted": False}, indent=2) + "\n")
        print("Harvey LAB infrastructure/grading error: " + message, file=sys.stderr)
        print(_redact(traceback.format_exc()), file=sys.stderr)
        reward_file.unlink(missing_ok=True)
        return 1
    finally:
        if run_dir.is_dir():
            for path in run_dir.iterdir():
                if path.is_file():
                    if path.suffix in (".json", ".html", ".txt", ".md"):
                        (logs / path.name).write_text(_redact(path.read_text(errors="replace")))
                    else:
                        shutil.copyfile(path, logs / path.name)
        for path in logs.glob("*.json"):
            path.write_text(_redact(path.read_text()))
        freeze = Path("/opt/harvey-runtime-packages.txt")
        if freeze.is_file():
            shutil.copyfile(freeze, logs / "native-runtime-packages.txt")


if __name__ == "__main__":
    raise SystemExit(main())
