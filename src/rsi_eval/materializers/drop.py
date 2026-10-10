"""Official DROP data and scorer with an explicit file-based agent protocol."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
import re
import shlex
import shutil
from typing import Any
from zipfile import ZipFile

from .common import download


def _answer_spans(answer: dict[str, Any]) -> list[str]:
    """Transport a gold annotation using the official answer field precedence."""
    if answer.get("number"):
        return [str(answer["number"])]
    if answer.get("spans"):
        return answer["spans"]
    date = answer["date"]
    return ["{day} {month} {year}".format(**date)]


def prepare(spec: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    source = spec["source"]
    archive = download(source["url"], output_dir / ".sources" / "drop_dataset.zip", source["sha256"])
    files = {}
    for name, item in source["scorer"]["files"].items():
        files[name] = download(item["url"], output_dir / ".sources" / name, item["sha256"])
    selected = {task["id"] for task in spec["tasks"]}
    for task in spec["tasks"]:
        if task["split"] not in {"train", "dev"} or not re.fullmatch(
            rf"{task['split']}-[0-9a-f]{{8}}(?:-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}}(?:-occurrence-[2-9][0-9]*)?", task["id"]
        ):
            raise ValueError("DROP task ID does not match its declared split and query ID")
    records = {}
    with ZipFile(archive) as bundle:
        license_bytes = bundle.read("drop_dataset/license.txt")
        if hashlib.sha256(license_bytes).hexdigest() != source["license_sha256"]:
            raise ValueError("DROP data license checksum mismatch")
        (output_dir / ".sources" / "data-license.txt").write_bytes(license_bytes)
        for split in sorted({task["split"] for task in spec["tasks"]}):
            item = source["splits"][split]
            payload = bundle.read(item["archive_path"])
            if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                raise ValueError(f"DROP {split} data checksum mismatch")
            passages = json.loads(payload)
            seen = Counter()
            for passage_id, passage in passages.items():
                for question in passage["qa_pairs"]:
                    query_id = question["query_id"]
                    seen[query_id] += 1
                    task_id = f"{split}-{query_id}"
                    if seen[query_id] > 1:
                        task_id += f"-occurrence-{seen[query_id]}"
                    if task_id in selected:
                        records[task_id] = passage_id, passage["passage"], question
            if sum(seen.values()) != item["task_count"]:
                raise ValueError(f"unexpected DROP {split} question count")
    if records.keys() != selected:
        raise ValueError("selected DROP task IDs do not match the pinned dataset")
    result = []
    for task in spec["tasks"]:
        passage_id, passage, question = records[task["id"]]
        root = output_dir / task["id"]
        for folder in ("environment", "tests", "solution"):
            (root / folder).mkdir(parents=True)
        (root / "instruction.md").write_text(
            "Read the passage and answer the question.\n\nPassage:\n" + passage
            + "\n\nQuestion:\n" + question["question"]
            + "\n\nWrite your final answer to /app/answer.json as a JSON string, "
            'for example "42", or a JSON array of strings for multiple answer spans, '
            'for example ["Alice", "Bob"]. '
            "For a date, provide one string in day month year order and omit unknown components. "
            "Include only the answer, without an explanation, units that were not requested, "
            "or Markdown fences. Numeric answers must be strings, not JSON numbers.\n",
            encoding="utf-8",
        )
        (root / "task.toml").write_text(
            'version = "1.0"\n[agent]\ntimeout_sec = 180.0\n'
            '[verifier]\ntimeout_sec = 60.0\n'
            '[environment]\nbuild_timeout_sec = 600.0\ncpus = 1\nmemory_mb = 1024\nstorage_mb = 2048\n'
        )
        (root / "environment" / "Dockerfile").write_text(
            'FROM python:3.12.12-slim-bookworm\n'
            'RUN pip install --no-cache-dir numpy==2.2.6 scipy==1.15.3\nWORKDIR /app\n'
        )
        (root / "tests" / "expected.json").write_text(json.dumps({
            "query_id": question["query_id"], "split": task["split"],
            "annotations": {passage_id: {"passage": passage, "qa_pairs": [question]}},
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        shutil.copyfile(files["drop.py"], root / "tests" / "official_drop.py")
        shutil.copyfile(files["LICENSE"], root / "tests" / "LICENSE")
        shutil.copyfile(Path(__file__).with_name("drop_verifier.py"), root / "tests" / "verify.py")
        (root / "tests" / "test.sh").write_text(
            '#!/bin/bash\nset -euo pipefail\npython -I /tests/verify.py\n'
        )
        answers = [question["answer"], *question.get("validated_answers", [])]
        spans = [_answer_spans(answer) for answer in answers]
        gold = next((answer for answer in spans if answer[0].strip()), [""])
        submission = json.dumps(gold[0] if len(gold) == 1 else gold, ensure_ascii=False)
        (root / "solution" / "solve.sh").write_text(
            '#!/bin/bash\nset -euo pipefail\nmkdir -p /app\n'
            + "printf '%s\\n' " + shlex.quote(submission) + ' > /app/answer.json\n', encoding="utf-8"
        )
        (root / "provenance.json").write_text(json.dumps({
            "id": task["id"], "split": task["split"], "query_id": question["query_id"],
            "passage_id": passage_id, "dataset_sha256": source["sha256"],
            "scorer_commit": source["scorer"]["commit"],
            "scorer_sha256": source["scorer"]["files"]["drop.py"]["sha256"],
            "protocol": spec["protocol"],
        }, indent=2) + "\n")
        result.append({"id": task["id"], "path": str(root.resolve())})
    return result
