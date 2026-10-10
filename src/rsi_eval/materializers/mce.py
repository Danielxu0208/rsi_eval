"""The five complete MCE data configurations published by Meta-Harness."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from .common import download
from .reasoning import write_task

_DATASETS = {"FiNER": "finer", "USPTO": "uspto", "Symptom2Disease": "symptom_diagnosis",
             "LawBench": "crime_prediction", "AEGIS": "aegis2"}
_LOADERS = {"_load_jsonl", "_get_mce_split_path", "load_mce_dataset", "_load_finer", "_load_uspto",
            "_load_symptom2disease", "_load_lawbench", "_load_aegis"}


def preflight(spec: dict[str, Any]) -> dict[str, Any]:
    options = spec.get("runtime_options", {})
    timeout = options.get("agent_timeout", 300)
    ok = (set(options) <= {"agent_timeout"} and type(timeout) in (int, float) and 0 < timeout <= 604800)
    return {"mce_options": {"ok": ok, "detail": "Only agent_timeout is configurable, positive and at most 604800 seconds. "
                                            "The original dataset prompts and scorers are pinned."}}


def native_loader(root: Path):
    """Load only the original MCE functions; avoid importing optional OOD dataset packages."""
    source = (root / "loaders.py").read_text()
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in _LOADERS]
    if {node.name for node in functions} != _LOADERS:
        raise ValueError("pinned MCE loader structure changed")
    constants = ast.parse((root / "constants.py").read_text())
    context_nodes = [node for node in constants.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id in {"FINER_XBRL_TAGS", "FINER_CONTEXT"}
                             for target in node.targets)]
    context_namespace = {"__builtins__": {}}
    exec(compile(ast.Module(body=context_nodes, type_ignores=[]), str(root / "constants.py"), "exec"), context_namespace)
    context = context_namespace["FINER_CONTEXT"]
    namespace = {"json": json, "Path": Path, "MCE_TASKS": list(_DATASETS), "FINER_CONTEXT": context,
                 "MCE_DATA_PATH": str(root)}
    # Function ASTs are byte-for-byte source semantics. Imports alone are supplied locally.
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(root / "loaders.py"), "exec"), namespace)
    return namespace["load_mce_dataset"]


def load_records(spec: dict[str, Any], output_dir: Path) -> tuple[dict[str, dict], Path]:
    source = spec["source"]
    root = output_dir / ".sources"
    for name, item in source["files"].items():
        download(item["url"], root / name, item["sha256"])
    loader = native_loader(root)
    records = {}
    dataset = source["configuration"]
    if dataset not in _DATASETS:
        raise ValueError("unknown MCE configuration")
    for split, count in source["split_counts"].items():
        rows = loader(dataset, split=split, limit=None)
        if len(rows) != count:
            raise ValueError(f"MCE split count changed: {split}")
        for index, row in enumerate(rows):
            if not isinstance(row["input"], str) or not isinstance(row["target"], str):
                raise ValueError("MCE records require string input and target")
            records[f"{split}-{index + 1:04d}"] = {**row, "split": split, "record_id": index}
    digest = hashlib.sha256("\n".join(sorted(records)).encode()).hexdigest()
    if len(records) != source["record_count"] or digest != source["task_ids_sha256"]:
        raise ValueError("MCE full source manifest changed")
    return records, root


def prepare(spec: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    if not preflight(spec)["mce_options"]["ok"]:
        raise ValueError(preflight(spec)["mce_options"]["detail"])
    records, source_root = load_records(spec, output_dir)
    result = []
    for task in spec["tasks"]:
        record = records[task["id"]]
        if record["split"] != task["split"] or record["record_id"] != task["record_id"]:
            raise ValueError("MCE task identity does not match source record")
        root = output_dir / task["id"]
        expected = {"kind": "mce", "task_id": task["id"], "dataset": spec["source"]["configuration"], "target": record["target"]}
        provenance = {"task_id": task["id"], "source_commit": spec["source"]["commit"],
            "source_files": spec["source"]["files"], "protocol": spec["protocol"],
            "split": record["split"], "record_index": record["record_id"],
            "prompt_sha256": hashlib.sha256(record["input"].encode()).hexdigest(),
            "record_sha256": hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()}
        write_task(root, record["input"] + "\n\nWrite your complete response in the requested format to /app/answer.txt.\n",
                   expected, json.dumps({"final_answer": record["target"]}), provenance,
                   timeout=spec.get("runtime_options", {}).get("agent_timeout", 300))
        native = root / "tests/native"
        native.mkdir()
        (native / "__init__.py").write_text("")
        for filename in ("constants.py", "evaluators.py"):
            shutil.copyfile(source_root / filename, native / filename)
        shutil.copyfile(Path(__file__).with_name("mce_verifier.py"), root / "tests/verify.py")
        result.append({"id": task["id"], "path": str(root.resolve())})
    return result
