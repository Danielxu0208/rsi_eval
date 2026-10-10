"""Benchmark-specific summaries alongside untouched native Harbor metrics."""
from __future__ import annotations

import math
from typing import Any

from .types import TrialResult


def _finite(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def metric_error(metrics: dict[str, Any], spec: dict[str, Any], task_id: str) -> str | None:
    """Validate a declared score contract without modifying native metrics."""
    kind = spec.get("aggregation", {}).get("type")
    reward = metrics.get("reward")
    if kind in {"pass_at_k", "accuracy_by_language", "accuracy_by_group"}:
        if reward is not None and (_finite(reward) and reward not in (0, 1)):
            return "nonbinary reward for a binary benchmark"
    elif kind == "rubric_mean":
        if not _finite(reward) or not 0 <= reward <= 1:
            return "rubric reward must be finite and in [0, 1]"
    elif kind == "drop_em_f1":
        em, f1 = metrics.get("exact_match"), metrics.get("f1")
        if not (_finite(em) and em in (0, 1) and _finite(f1) and 0 <= f1 <= 1):
            return "DROP requires exact_match in {0, 1} and f1 in [0, 1]"
        if not _finite(reward) or not math.isclose(reward, f1, rel_tol=0, abs_tol=1e-12):
            return "DROP reward must equal its native f1"
    elif kind == "frontier_native":
        native_score, valid = metrics.get("native_score"), metrics.get("valid")
        if not (_finite(reward) and _finite(native_score) and _finite(valid) and valid in (0, 1)):
            return "Frontier-Eng requires finite reward/native_score and valid in {0, 1}"
        if reward != native_score:
            return "Frontier-Eng reward must preserve its native_score"
    return None


def benchmark_metrics(trials: list[TrialResult], spec: dict[str, Any], repetitions: int,
                      *, valid_run: bool = True) -> dict[str, Any]:
    config = spec["aggregation"]
    measured = {"completed", "task_timeout", "agent_error"}
    groups = {task["id"]: [] for task in spec["tasks"]}
    for trial in trials:
        if trial.task_id in groups:
            groups[trial.task_id].append(trial)
    binary = config["type"] in {"pass_at_k", "accuracy_by_language", "accuracy_by_group"}
    def task_complete(rows: list[TrialResult]) -> bool:
        return len(rows) == repetitions and {row.repetition for row in rows} == set(range(repetitions)) and all(
            row.status in measured and _finite(row.reward)
            and (not binary or row.reward in (0, 1))
            and not metric_error({**row.metrics, "reward": row.reward}, spec, row.task_id)
            for row in rows)

    def observed(rows: list[TrialResult]) -> int:
        # collect_result fills absent native reports with explicit placeholders.
        # They retain expected coverage, but are not observed trial executions.
        return sum(bool(row.artifact_dir) or row.error != "trial result was not produced" for row in rows)

    valid_structure = valid_run and all(trial.task_id in groups for trial in trials)
    complete = valid_structure and len(trials) == len(groups) * repetitions and all(
        task_complete(rows) for rows in groups.values())
    result: dict[str, Any] = {"complete": complete, "selection": spec["selection"],
                              "task_count": len(groups), "repetitions": repetitions,
                              "expected_trials": len(groups) * repetitions,
                              "observed_trials": observed(trials)}
    if config["type"] == "pass_at_k":
        result["pass_at_k"] = {}
        for k in config.get("k", [1]):
            if not isinstance(k, int) or isinstance(k, bool) or k < 1:
                raise ValueError("pass@k requires positive integer k")
            if not complete or k > repetitions:
                result["pass_at_k"][str(k)] = None
                continue
            estimates = []
            for rows in groups.values():
                successes = sum(row.reward == 1 for row in rows)
                estimates.append(1 - (math.comb(repetitions - successes, k)
                    if repetitions - successes >= k else 0) / math.comb(repetitions, k))
            result["pass_at_k"][str(k)] = sum(estimates) / len(estimates)
    elif config["type"] == "accuracy_by_language":
        languages = {task["id"]: task["language"] for task in spec["tasks"]}
        totals: dict[str, list[TrialResult]] = {}
        for task_id, rows in groups.items():
            totals.setdefault(languages[task_id], []).extend(rows)
        result["accuracy"] = sum(t.reward for t in trials) / len(trials) if complete else None
        result["accuracy_by_language"] = {
            language: sum(t.reward for t in rows) / len(rows) if complete else None
            for language, rows in sorted(totals.items())}
    elif config["type"] in {"accuracy_by_group", "rubric_mean"}:
        key = "accuracy" if binary else "mean_reward"
        result[key] = sum(t.reward for t in trials) / len(trials) if complete else None
        result["scale"] = "0_to_1"
        if not binary:
            result["score_100"] = result[key] * 100 if complete else None
        for field in config.get("group_by", []):
            subsets: dict[str, list[str]] = {}
            for task in spec["tasks"]:
                subsets.setdefault(str(task[field]), []).append(task["id"])
            summaries = {}
            for group, task_ids in sorted(subsets.items()):
                rows = [row for task_id in task_ids for row in groups[task_id]]
                group_complete = valid_structure and all(task_complete(groups[task_id]) for task_id in task_ids)
                average = sum(row.reward for row in rows) / len(rows) if group_complete else None
                summary = {key: average, "task_count": len(task_ids), "complete": group_complete,
                           "expected_trials": len(task_ids) * repetitions, "observed_trials": observed(rows)}
                if not binary:
                    summary["score_100"] = average * 100 if group_complete else None
                summaries[group] = summary
            result["by_" + field] = summaries
        result["aggregation_note"] = "Equal weight per selected task. Published splits remain separate; combined selections are not official test-only scores."
    elif config["type"] == "drop_em_f1":
        def average(rows: list[TrialResult]) -> dict[str, float | None]:
            return {key: sum(t.metrics[key] for t in rows) / len(rows) if complete else None
                    for key in ("exact_match", "f1")}

        result.update(average(trials))
        result["scale"] = "0_to_1"
        splits: dict[str, list[TrialResult]] = {}
        for task in spec["tasks"]:
            splits.setdefault(task["split"], []).extend(groups[task["id"]])
        result["by_split"] = {split: average(rows) for split, rows in sorted(splits.items())}
    elif config["type"] == "frontier_native":
        # Native objectives have different units and directions. Preserve each
        # trial and task instead of presenting Harbor's mean as a suite score.
        result["aggregate_score"] = None
        result["aggregation_note"] = "Native task objectives are not averaged across tasks."
        result["per_task"] = {
            task["id"]: {"objective": task.get("objective"),
                         "valid_rate": sum(row.metrics["valid"] for row in groups[task["id"]]) / repetitions
                                       if complete else None,
                         "trials": [{"repetition": row.repetition, "status": row.status,
                                     "reward": row.reward, "metrics": row.metrics}
                                    for row in groups[task["id"]]]}
            for task in spec["tasks"]}
    else:
        raise ValueError(f"unknown benchmark aggregation: {config['type']}")
    return result
