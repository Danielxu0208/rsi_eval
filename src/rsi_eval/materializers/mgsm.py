"""MGSM TSV data with a declared, zero-shot agent submission protocol."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
from typing import Any

from .common import download


def prepare(spec: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    source = spec["source"]
    languages = sorted({task["language"] for task in spec["tasks"]})
    records = {}
    for language in languages:
        filename = f"mgsm_{language}.tsv"
        item = source["files"][filename]
        path = download(item["url"], output_dir / ".sources" / filename, item["sha256"])
        rows = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()]
        if len(rows) != 250 or any(len(row) != 2 for row in rows):
            raise ValueError(f"unexpected MGSM source shape: {language}")
        records[language] = rows
    license_source = source["files"]["LICENSE"]
    download(license_source["url"], output_dir / ".sources" / "LICENSE", license_source["sha256"])
    result = []
    for task in spec["tasks"]:
        language, index = task["language"], task["record_id"]
        if task["id"] != f"{language}-{index+1:03}" or not 0 <= index < 250:
            raise ValueError("MGSM task ID does not match its source row")
        question, answer = records[language][index]
        root = output_dir / task["id"]
        for folder in ("environment", "tests", "solution"):
            (root / folder).mkdir(parents=True)
        (root / "instruction.md").write_text(
            question + "\n\nWrite the final numeric answer alone to /app/answer.txt. "
            "Do not include units or an explanation in that file.\n", encoding="utf-8")
        (root / "task.toml").write_text(
            'version = "1.0"\n[agent]\ntimeout_sec = 180.0\n'
            '[verifier]\ntimeout_sec = 30.0\n'
            '[environment]\nbuild_timeout_sec = 600.0\ncpus = 1\nmemory_mb = 1024\nstorage_mb = 2048\n')
        (root / "environment" / "Dockerfile").write_text(
            'FROM python:3.12.12-slim-bookworm\nWORKDIR /app\n')
        (root / "tests" / "expected.json").write_text(json.dumps({"answer": answer, "language": language}) + "\n")
        shutil.copyfile(Path(__file__).with_name("mgsm_verifier.py"), root / "tests" / "verify.py")
        (root / "tests" / "test.sh").write_text(
            '#!/bin/bash\nset -euo pipefail\npython -I /tests/verify.py\n')
        (root / "solution" / "solve.sh").write_text(
            '#!/bin/bash\nset -euo pipefail\nmkdir -p /app\n'
            + "printf '%s\\n' " + shlex.quote(answer) + ' > /app/answer.txt\n')
        (root / "provenance.json").write_text(json.dumps({
            "id": task["id"], "source_commit": source["commit"],
            "source_file": f"mgsm_{language}.tsv", "row_index": index,
            "protocol": spec["protocol"]}, indent=2) + "\n")
        result.append({"id": task["id"], "path": str(root.resolve())})
    return result
