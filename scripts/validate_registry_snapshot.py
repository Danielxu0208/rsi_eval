"""Replay a pinned Harbor registry task from its recorded files and images.

Runs the official oracle twice, without an agent model or paid judge. Unlike the
synthetic asset fixture, the initial task is resolved by Harbor's real registry.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate
from rsi_eval.runtime_snapshot import capture, load_snapshot, restore_images


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--harbor", required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    candidate = root / "oracle-candidate"
    candidate.mkdir()
    (candidate / "agent.py").write_text("# Native oracle; no model is called.\n")
    target = TargetSpec(candidate, "oracle")
    benchmark = BenchmarkSelection("terminal-bench", "2.0", ("log-summary-date-ranges",))
    initial = evaluate(target=target, benchmark=benchmark,
        options=RunOptions(harbor_executable=args.harbor, timeout=900,
                           retain_runtime_images=True), output_dir=root / "initial")
    assert initial.status == "completed", initial.error
    assert initial.trials[0].reward == 1, initial.to_dict()
    assert not initial.resolved.get("execution_tasks"), "Initial run bypassed the registry"
    captured = capture(root / "initial", root / "snapshot")
    restored = restore_images(root / "snapshot")
    _, frozen = load_snapshot(root / "snapshot", check_images=True, check_archive=True)
    replay = evaluate(target=target, benchmark=benchmark,
        options=RunOptions(harbor_executable=args.harbor, timeout=900,
                           runtime_snapshot=str(root / "snapshot")), output_dir=root / "replay")
    assert replay.status == "completed", replay.error
    assert replay.trials[0].reward == 1, replay.to_dict()
    task = replay.resolved["execution_tasks"][0]
    assert Path(task["path"]).is_relative_to(root / "replay/tasks")
    expected_images = {name: row["id"] for name, row in frozen["tasks"][0]["services"].items()}
    observed = json.loads((Path(replay.trials[0].artifact_dir) / "build-context/runtime-images.json").read_text())
    assert {name: row["id"] for name, row in observed["services"].items()} == expected_images
    report = {"status": "passed", "scope": "real-Harbor-registry-oracle-capture-and-replay",
              "benchmark": benchmark.name, "version": benchmark.version,
              "task_id": benchmark.task_ids[0], "model_calls": 0, "judge_calls": 0,
              "initial_used_registry": True, "replay_used_local_frozen_tasks": True,
              "same_images": True, "rewards": [initial.trials[0].reward, replay.trials[0].reward],
              "capture": captured, "restore": restored,
              "initial_result": str(root / "initial/result.json"),
              "replay_result": str(root / "replay/result.json")}
    (root / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
