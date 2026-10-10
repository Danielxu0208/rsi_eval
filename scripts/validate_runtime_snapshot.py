"""Real Docker regression for explicit runtime capture and replay; no models.

Run with Harbor 0.18.0 installed and PYTHONPATH=src:. This is a synthetic task,
not a benchmark score. It changes build inputs between runs and requires replay
to retain the original image and copied asset bytes without materialization.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate
from rsi_eval.artifacts import artifact_hash
from rsi_eval.benchmarks import register_benchmark
from rsi_eval import materializers
from rsi_eval.runtime_snapshot import capture, restore_images


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--harbor", required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    task = root / "source-task"
    for name in ("environment", "tests", "solution"):
        (task / name).mkdir(parents=True)
    (task / "task.toml").write_text('version = "1.0"\n[agent]\ntimeout_sec = 60\n[verifier]\ntimeout_sec = 60\n[environment]\nbuild_timeout_sec = 600\ncpus = 1\nmemory_mb = 256\n')
    (task / "instruction.md").write_text("Validate a runtime snapshot fixture.")
    (task / "environment/asset.txt").write_text("frozen-asset\n")
    (task / "environment/Dockerfile").write_text('FROM python:3.12-slim-bookworm\nARG FIXTURE_LABEL\nRUN echo "$FIXTURE_LABEL" > /image-marker\nCOPY asset.txt /frozen-asset\n')
    (task / "environment/docker-compose.yaml").write_text('version: "3.8"\nservices:\n  main:\n    build:\n      context: .\n      args:\n        FIXTURE_LABEL: ${RSI_FREEZE_MARKER}\n')
    (task / "solution/solve.sh").write_text('#!/bin/bash\nset -e\n[ "$(cat /image-marker)" = original ]\n[ "$(cat /frozen-asset)" = frozen-asset ]\n')
    (task / "tests/test.sh").write_text('#!/bin/bash\nset -e\nmkdir -p /logs/verifier\n[ "$(cat /image-marker)" = original ]\n[ "$(cat /frozen-asset)" = frozen-asset ]\necho 1 > /logs/verifier/reward.txt\n')
    candidate = root / "candidate"
    candidate.mkdir()
    (candidate / "README.txt").write_text("Oracle fixture; no model calls.")
    register_benchmark({"name": "runtime-freeze-fixture", "version": "1", "dataset_ref": "fixture@1",
        "materializer": "mgsm", "source": {"sha256": "a" * 64}, "protocol": "synthetic-runtime-regression",
        "tasks": [{"id": "fixture-task"}]})

    def prepare(spec, output, **kwargs):
        destination = output / "fixture-task"
        shutil.copytree(task, destination)
        return [{"id": "fixture-task", "path": str(destination), "sha256": artifact_hash(destination)}]
    materializers.prepare = prepare
    selection = BenchmarkSelection("runtime-freeze-fixture", "1")
    target = TargetSpec(candidate, "oracle")
    os.environ["RSI_FREEZE_MARKER"] = "original"
    first = evaluate(target=target, benchmark=selection, options=RunOptions(harbor_executable=args.harbor, retain_runtime_images=True), output_dir=root / "initial")
    if first.status != "completed" or first.trials[0].reward != 1:
        raise RuntimeError(f"Initial real Docker fixture failed: {first.error}; inspect {first.report_path}")
    archive = capture(root / "initial", root / "snapshot")
    restore = restore_images(root / "snapshot")
    shutil.rmtree(task)
    os.environ["RSI_FREEZE_MARKER"] = "would-change-if-rebuilt"
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay tried to materialize source again")
    materializers.prepare = forbidden
    second = evaluate(target=target, benchmark=selection,
        options=RunOptions(harbor_executable=args.harbor, runtime_snapshot=str(root / "snapshot")), output_dir=root / "replay")
    if second.status != "completed" or second.trials[0].reward != 1:
        raise RuntimeError(f"Frozen real Docker replay failed: {second.error}; inspect {second.report_path}")
    old = json.loads((Path(first.trials[0].artifact_dir) / "build-context/runtime-images.json").read_text())
    new = json.loads((Path(second.trials[0].artifact_dir) / "build-context/runtime-images.json").read_text())
    def identities(value):
        return {name: {key: image[key] for key in ("id", "os", "architecture")}
                for name, image in value["services"].items()}
    assert identities(old) == identities(new)
    result = {"status": "passed", "mode": "real-docker-synthetic-task", "model_calls": 0, "judge_calls": 0,
              "initial": first.report_path, "replay": second.report_path, "capture": archive, "restore": restore,
              "same_images": True, "removed_source_assets": True, "changed_build_argument": True,
              "rewards": [t.reward for t in first.trials + second.trials]}
    (root / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
