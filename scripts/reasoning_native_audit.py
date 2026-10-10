#!/usr/bin/env python3
"""Download every pinned public record and exercise every objective/native scorer.

Run from an installed SDK: python scripts/reasoning_native_audit.py --output .runtime/reasoning-audit
No models or Docker are required. Source bytes remain in the selected output path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

from rsi_eval.benchmarks import list_benchmarks
from rsi_eval.materializers import reasoning, mce
from rsi_eval.materializers.reasoning_verifier import score
from rsi_eval.materializers.mce_verifier import native_evaluator, score as mce_score


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "model_calls": 0, "scope": "all public source records, native answer replay; not model accuracy", "profiles": []}
    for spec in list_benchmarks():
        if spec.get("materializer") not in ("reasoning", "mce"):
            continue
        destination = args.output / (spec["name"] + "-" + spec["version"])
        destination.mkdir(exist_ok=True)
        if spec["materializer"] == "reasoning":
            records = reasoning.load_records(spec, destination)
            native = None
        else:
            records, source_root = mce.load_records(spec, destination)
            (source_root / "__init__.py").write_text("")
            native = native_evaluator(source_root)
        expected_ids = {task["id"] for task in spec["tasks"]}
        assert set(records) == expected_ids
        split_counts = {}
        counts = {"correct_gold": 0, "rejected_missing": 0}
        for task_id, record in records.items():
            split_counts[record["split"]] = split_counts.get(record["split"], 0) + 1
            if native is not None:
                expected = record["target"]
                response = json.dumps({"final_answer": expected})
                positive = mce_score(native, spec["source"]["configuration"], expected, response)
                negative = mce_score(native, spec["source"]["configuration"], expected, None)
                passed = positive["was_correct"]
                missing_rejected = not negative["was_correct"]
            else:
                if "arc" in record:
                    expected = {"kind": "arc", "outputs": [pair["output"] for pair in record["arc"]["test"]]}
                    response = json.dumps({"predictions": [[grid] for grid in expected["outputs"]]})
                else:
                    expected = {"kind": "choice", "answer": record["row"][-1]}
                    response = expected["answer"]
                passed = score(expected, response)["correct"]
                missing_rejected = not score(expected, None)["correct"]
            if not passed or not missing_rejected:
                raise ValueError(f"native self-answer or missing-answer audit failed: {spec['name']} {task_id}")
            counts["correct_gold"] += int(passed)
            counts["rejected_missing"] += int(missing_rejected)
        profile = {"name": spec["name"], "version": spec["version"], "source": spec["source"],
                   "record_count": len(records), "split_counts": split_counts, **counts,
                   "protocol": spec["protocol"], "status": "passed"}
        report["profiles"].append(profile)
        print(spec["name"], spec["version"], len(records), "passed", flush=True)
    modules = (reasoning, mce, sys.modules[score.__module__], sys.modules[mce_score.__module__])
    report["implementation_sha256"] = {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for module in modules}
    path = args.report or args.output / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
