"""Complete pinned MMLU, GPQA and original ARC public releases."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import random
import shlex
import shutil
import tarfile
from typing import Any
import zipfile

from .common import download


def preflight(spec: dict[str, Any]) -> dict[str, Any]:
    options = spec.get("runtime_options", {})
    allowed = {"agent_timeout", "few_shot", "choice_seed"}
    unknown = sorted(set(options) - allowed)
    errors = []
    if unknown:
        errors.append(f"unknown reasoning runtime options: {unknown}")
    if "few_shot" in options and (spec["name"] != "mmlu" or type(options["few_shot"]) is not int
                                  or not 0 <= options["few_shot"] <= 5):
        errors.append("few_shot is supported only for MMLU and must be an integer from 0 to 5")
    if "choice_seed" in options and (spec["name"] != "gpqa" or type(options["choice_seed"]) is not int):
        errors.append("choice_seed is supported only for GPQA and must be an integer")
    timeout = options.get("agent_timeout", 300)
    if type(timeout) not in (float, int) or not 0 < timeout <= 604800:
        errors.append("agent_timeout must be positive and at most 604800 seconds")
    return {"reasoning_options": {"ok": not errors, "detail": "; ".join(errors) or "public pinned sources; no model judge"}}


def _csv(data: bytes) -> list[list[str]]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    if any(len(row) != 6 or row[-1] not in "ABCD" or len(row[-1]) != 1 for row in rows):
        raise ValueError("invalid MMLU choice data")
    return rows


def load_records(spec: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Read every record from checksummed source bytes without extracting archive paths."""
    source = spec["source"]
    archive = download(source["url"], output_dir / ".sources" / source["filename"], source["sha256"])
    result = {}
    if spec["name"] == "mmlu":
        with tarfile.open(archive) as stream:
            for member in stream.getmembers():
                parts = Path(member.name).parts
                if not member.isfile() or len(parts) != 3 or parts[0] != "data" or not parts[-1].endswith(".csv"):
                    continue
                split = parts[1]
                if split not in ("auxiliary_train", "dev", "val", "test"):
                    raise ValueError(f"unexpected MMLU split {split}")
                subject = parts[-1].removesuffix(".csv").removesuffix(f"_{split}")
                rows = _csv(stream.extractfile(member).read())
                for index, row in enumerate(rows):
                    result[f"{split}-{subject}-{index + 1:05d}"] = {
                        "row": row, "split": split, "subject": subject, "record_id": index,
                        "source_path": member.name}
    elif spec["name"] == "gpqa":
        with zipfile.ZipFile(archive) as stream:
            data = stream.read(source["member"], pwd=b"deserted-untie-orchid")
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        rng = random.Random(spec.get("runtime_options", {}).get("choice_seed", 0))
        for index, row in enumerate(rows):
            choices = [row[f"Incorrect Answer {i}"] for i in (1, 2, 3)] + [row["Correct Answer"]]
            if not row["Question"] or any(not choice for choice in choices):
                raise ValueError("empty GPQA question or choice")
            rng.shuffle(choices)  # Match upstream load_examples, including the entire release order.
            result[f"{source['configuration']}-{index + 1:04d}"] = {
                "row": [row["Question"], *choices, "ABCD"[choices.index(row["Correct Answer"])]],
                "source_record_id": row["Record ID"], "record_id": index,
                "split": source["configuration"], "subject": row["Subdomain"],
                "source_path": source["member"]}
    elif spec["name"] == "arc-agi":
        with tarfile.open(archive) as stream:
            for member in stream.getmembers():
                parts = Path(member.name).parts
                if (not member.isfile() or len(parts) != 4 or parts[1] != "data"
                        or parts[2] not in ("training", "evaluation") or not parts[3].endswith(".json")):
                    continue
                record = json.loads(stream.extractfile(member).read())
                from .reasoning_verifier import _grid
                if not {"train", "test"} <= record.keys() or any(not record[key] for key in ("train", "test")):
                    raise ValueError("invalid ARC train/test structure")
                if any(set(pair) != {"input", "output"} or not _grid(pair["input"]) or not _grid(pair["output"])
                       for key in ("train", "test") for pair in record[key]):
                    raise ValueError("invalid ARC grid")
                result[f"{parts[2]}-{Path(parts[3]).stem}"] = {
                    "arc": record, "split": parts[2], "source_path": "/".join(parts[1:])}
    else:
        raise ValueError("unsupported reasoning benchmark")
    if len(result) != source["record_count"]:
        raise ValueError(f"source record count changed: expected {source['record_count']}, got {len(result)}")
    source_ids_digest = hashlib.sha256("\n".join(sorted(result)).encode()).hexdigest()
    if source_ids_digest != source["task_ids_sha256"]:
        raise ValueError("source task identities differ from the pinned full manifest")
    return result


def _format(row: list[str], answer: bool = False) -> str:
    prompt = row[0] + "".join(f"\n{label}. {choice}" for label, choice in zip("ABCD", row[1:5])) + "\nAnswer:"
    return prompt + (f" {row[-1]}\n\n" if answer else "")


def write_task(root: Path, instruction: str, expected: dict, oracle: str,
               provenance: dict, *, timeout: float = 300) -> None:
    for folder in ("environment", "tests", "solution"):
        (root / folder).mkdir(parents=True)
    (root / "instruction.md").write_text(instruction, encoding="utf-8")
    (root / "task.toml").write_text(
        f'version = "1.0"\n[agent]\ntimeout_sec = {float(timeout)}\n'
        '[verifier]\ntimeout_sec = 30.0\n[environment]\nbuild_timeout_sec = 600.0\n'
        'cpus = 1\nmemory_mb = 1024\nstorage_mb = 2048\n')
    (root / "environment/Dockerfile").write_text('FROM python:3.12.12-slim-bookworm\nWORKDIR /app\n')
    (root / "tests/expected.json").write_text(json.dumps(expected, ensure_ascii=False, allow_nan=False) + "\n")
    shutil.copyfile(Path(__file__).with_name("reasoning_verifier.py"), root / "tests/verify.py")
    (root / "tests/test.sh").write_text('#!/bin/bash\nset -euo pipefail\npython -I /tests/verify.py\n')
    filename = "answer.json" if expected["kind"] == "arc" else "answer.txt"
    (root / "solution/solve.sh").write_text('#!/bin/bash\nset -euo pipefail\nmkdir -p /app\n'
        + "printf '%s\\n' " + shlex.quote(oracle) + f" > /app/{filename}\n")
    (root / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n")


def prepare(spec: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    checks = preflight(spec)
    if not all(check["ok"] for check in checks.values()):
        raise ValueError(checks["reasoning_options"]["detail"])
    records = load_records(spec, output_dir)
    options = spec.get("runtime_options", {})
    dev = {}
    if spec["name"] == "mmlu":
        for row in records.values():
            if row["split"] == "dev":
                dev.setdefault(row["subject"], []).append(row["row"])
    result = []
    for task in spec["tasks"]:
        record = records[task["id"]]
        for key in ("split", "subject", "record_id", "source_record_id"):
            if key in task and task[key] != record.get(key):
                raise ValueError(f"task {task['id']} does not match its source {key}")
        expected = {"task_id": task["id"]}
        extra = {}
        if "arc" in record:
            public = {"train": record["arc"]["train"], "test": [{"input": p["input"]} for p in record["arc"]["test"]]}
            instruction = ("Infer the grid transformation from the training examples and produce outputs for every test input.\n"
                "Each test input permits at most three attempts. A task is solved only when all test inputs have a correct attempt.\n"
                + json.dumps(public, separators=(",", ":")) + "\n\n"
                'Write /app/answer.json as {"predictions": [[grid_attempt_1, grid_attempt_2], ...]}, '
                "with one list of one to three grids per test input, in order. Grid cells must be integers from 0 to 9.\n")
            expected.update(kind="arc", outputs=[p["output"] for p in record["arc"]["test"]])
            oracle = json.dumps({"predictions": [[grid] for grid in expected["outputs"]]})
        else:
            row = record["row"]
            instruction = ""
            if spec["name"] == "mmlu":
                # Original five-shot dev context for held-out splits; never reveal a dev query's gold label.
                k = options.get("few_shot", 5) if record["split"] in ("test", "val") else 0
                examples = dev.get(record["subject"], [])[:k]
                instruction = "The following are multiple choice questions (with answers) about " + record["subject"].replace("_", " ") + ".\n\n"
                instruction += "".join(_format(example, answer=True) for example in examples)
                extra["few_shot_examples"] = len(examples)
            instruction += _format(row) + "\n\nWrite only the chosen uppercase letter A, B, C, or D to /app/answer.txt.\n"
            expected.update(kind="choice", answer=row[-1])
            oracle = row[-1]
        root = output_dir / task["id"]
        provenance = {"task_id": task["id"], "source_sha256": spec["source"]["sha256"],
            "source_path": record["source_path"], "protocol": spec["protocol"], "split": record["split"],
            "runtime_options": options, "record_sha256": hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(), **extra}
        write_task(root, instruction, expected, oracle, provenance, timeout=options.get("agent_timeout", 300))
        result.append({"id": task["id"], "path": str(root.resolve())})
    return result
