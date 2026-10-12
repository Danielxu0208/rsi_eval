"""Current benchmark compatibility, runtime requirements and objective rules."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.resources import files
import json
from typing import Any

from .benchmarks import list_benchmarks


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _bundled_catalog() -> list[dict]:
    rows = []
    for path in files("rsi_eval").joinpath("data").iterdir():
        if path.name.startswith("catalog") and path.name.endswith(".json"):
            rows.extend(json.loads(path.read_text()))
    return rows


def validate_support_data(data: dict, *, catalog: list[dict] | None = None) -> None:
    """Keep compatibility declarations aligned with catalog and method identities."""
    from .methods import METHODS
    catalog = _bundled_catalog() if catalog is None else catalog
    if data.get("schema_version") != 2:
        raise ValueError("Unsupported support-status schema")
    if set(data) != {"schema_version", "scope", "common_prerequisites", "methods", "benchmarks"}:
        raise ValueError("Support status requires configuration declarations")
    specs = {(s["name"], s["version"]): s for s in catalog}
    rows = {(r["name"], r["version"]): r for r in data["benchmarks"]}
    if (len(specs) != len(catalog) or len(rows) != len(data["benchmarks"])
            or set(specs) != set(rows)):
        raise ValueError("Support status must cover every bundled benchmark version exactly once")
    if set(data["methods"]) != set(METHODS):
        raise ValueError("Support status must cover every managed method exactly once")
    benchmark_fields = {"name", "version", "catalog_sha256", "dataset_ref", "task_count",
                        "lifecycle", "catalog_scope", "protocol_summary", "reward_domain",
                        "prerequisites", "limitations"}
    for key, row in rows.items():
        if set(row) != benchmark_fields:
            raise ValueError(f"Invalid benchmark compatibility declaration: {key}")
        spec = specs[key]
        if row.get("catalog_sha256") != _digest(spec):
            raise ValueError(f"Catalog changed; update support status for {key[0]}@{key[1]}")
        if (row.get("dataset_ref") != spec["dataset_ref"] or row.get("task_count") != len(spec["tasks"])
                or row.get("lifecycle") != spec.get("lifecycle", "active")):
            raise ValueError(f"Support status identity differs from catalog: {key}")
        if row.get("reward_domain") not in {"binary", "unit_interval", "heterogeneous"}:
            raise ValueError(f"Unknown score domain: {key}")
        if not all(row.get(k) for k in ("catalog_scope", "protocol_summary")):
            raise ValueError(f"Missing coverage or protocol scope: {key}")
        for field in ("prerequisites", "limitations"):
            if not isinstance(row[field], list) or any(not isinstance(v, str) or not v for v in row[field]):
                raise ValueError(f"Invalid {field}: {key}")
    method_fields = {"upstream_commit", "variant", "execution_route", "score_rules", "selection",
                     "input_modalities", "execution_limits"}
    for name, row in data["methods"].items():
        if set(row) != method_fields:
            raise ValueError(f"Invalid method compatibility declaration: {name}")
        if row.get("upstream_commit") != METHODS[name]["commit"]:
            raise ValueError(f"Method changed; update support status for {name}")
        if set(row["score_rules"]) != {"binary", "unit_interval", "heterogeneous"}:
            raise ValueError(f"Missing score compatibility rules for {name}")
        if set(row["input_modalities"]) != {"text", "image", "audio", "video"}:
            raise ValueError(f"Missing input modality declarations for {name}")
        if any(not isinstance(v, str) or not v for v in row["score_rules"].values()):
            raise ValueError(f"Invalid score compatibility rules for {name}")


def support_status(*, benchmark: str | None = None, method: str | None = None) -> dict:
    """Return current compatibility declarations for exact versions or a family.

    Unknown names/versions raise ValueError. Custom registrations expose their
    caller-declared configuration. Use check() to inspect the current runtime.
    This function performs no model, Docker, credential, or network operation.
    """
    data = json.loads(files("rsi_eval").joinpath("data/support_status.json").read_text())
    validate_support_data(data)
    if method is not None and method not in data["methods"]:
        raise ValueError(f"Unknown managed method: {method}")
    bundled = {(r["name"], r["version"]): r for r in data["benchmarks"]}
    rows = []
    for spec in list_benchmarks():
        key = (spec["name"], spec["version"])
        if benchmark is not None and benchmark not in {spec["name"], f"{key[0]}@{key[1]}"}:
            continue
        row = deepcopy(bundled.get(key))
        if row is None:
            row = {"name":key[0], "version":key[1], "dataset_ref":spec["dataset_ref"],
                   "task_count":len(spec["tasks"]), "lifecycle":spec.get("lifecycle", "custom"),
                   "catalog_scope":"caller-declared", "protocol_summary":"Caller-declared Harbor configuration",
                   "reward_domain":"unknown", "prerequisites":[],
                   "limitations":["Registered in the current process; declare its reward domain and objective."]}
        row["registration_source"] = "bundled" if key in bundled else "process_local"
        row["method_compatibility"] = {}
        for name, details in data["methods"].items():
            if method is not None and method != name:
                continue
            row["method_compatibility"][name] = {
                "execution_route":details["execution_route"],
                "adapter_status":"available_subject_to_prerequisites",
                "score_requirement":details["score_rules"].get(
                    row["reward_domain"], "Declare the benchmark reward domain and objective"),
            }
        rows.append(row)
    if benchmark is not None and not rows:
        raise ValueError(f"Unknown benchmark name or exact version: {benchmark}")
    data["benchmarks"] = rows
    if method is not None:
        data["methods"] = {method:data["methods"][method]}
    return data


def format_status(data: dict, *, detailed: bool = True) -> str:
    lines = ["Benchmark compatibility and runtime requirements", data["scope"],
             "Use check before run to inspect the configured runtime."]
    if not detailed:
        lines += ["", "CONFIGURATION | TASKS | LIFECYCLE | REWARD DOMAIN"]
        for row in data["benchmarks"]:
            lines.append(f"{row['name']}@{row['version']} | {row['task_count']} | {row['lifecycle']} | {row['reward_domain']}")
        lines += ["", "Use --benchmark NAME[@VERSION] or --method NAME for prerequisites and objective rules.",
                  "Use --json for dataset identities and structured compatibility declarations."]
        return "\n".join(lines)
    for name, method in data["methods"].items():
        lines += ["", f"{name}: {method['execution_route']}"]
        lines += [f"  {modality}: {support}" for modality, support in method["input_modalities"].items()]
        lines += [f"  Limit: {limit}" for limit in method["execution_limits"]]
    for row in data["benchmarks"]:
        lines += ["", f"{row['name']}@{row['version']} | {row['task_count']} selectable tasks | {row['lifecycle']}",
                  f"  Coverage: {row['catalog_scope']}; protocol: {row['protocol_summary']}",
                  f"  Reward domain: {row['reward_domain']}"]
        for name, compatibility in row["method_compatibility"].items():
            lines.append(f"  {name}: {compatibility['score_requirement']}")
        if row["prerequisites"]:
            lines.append("  Extra prerequisites: " + "; ".join(row["prerequisites"]))
        for limit in row["limitations"]:
            lines.append("  Limit: " + limit)
    lines.append("\nUse --json for dataset identities and structured compatibility declarations.")
    return "\n".join(lines)


def render_markdown(data: dict) -> str:
    """Generate the configuration guide shared by the SDK and CLI."""
    lines = ["# Compatibility and runtime requirements", "",
             data["scope"], "",
             "Query the packaged configuration from the CLI or Python. These commands make no model calls, start no containers and inspect no credentials.", "",
             "```sh", "rsi-eval status --benchmark terminal-bench@2.0 --method rrsi",
             "rsi-eval status --benchmark gpqa --json", "```", "",
             "```python", "from rsi_eval import support_status", "",
             'record = support_status(benchmark="drop@1.0-agent", method="ahe")', "```", "",
             "An exact `name@version` selects one configuration. A bare benchmark name returns every catalog version, including labeled legacy subsets. Unknown names and versions fail explicitly. Process-local custom registrations expose their caller-declared configuration. Use `check` to inspect the configured runtime before execution.", "",
             "## Shared runtime requirements", ""]
    lines += [f"- {p}." for p in data["common_prerequisites"]]
    lines += ["", "## Configuration overview", "", "Task counts describe selectable entries in each declared configuration.", "",
              "| Exact configuration | Tasks | Catalog scope | Reward domain |",
              "| --- | ---: | --- | --- |"]
    for row in data["benchmarks"]:
        lines.append(f"| `{row['name']}@{row['version']}` | {row['task_count']} | {row['catalog_scope']} | {row['reward_domain']} |")
    lines += ["", "## Method objectives and input capabilities", ""]
    for name, row in data["methods"].items():
        lines += [f"### {name}", "", f"Codex adaptation of upstream `{row['upstream_commit']}`. {row['execution_route'].rstrip('.')}.", "",
                  "| Input modality | Transport support |", "| --- | --- |"]
        lines += [f"| {modality} | {support} |" for modality, support in row["input_modalities"].items()]
        lines += ["", "Execution limits:", ""]
        lines += [f"- {limit}" for limit in row["execution_limits"]]
        lines += ["", "| Benchmark reward domain | Requirement |", "| --- | --- |"]
        lines += [f"| {key} | {value} |" for key,value in row["score_rules"].items()]
        lines += ["", row["selection"], ""]
    lines += ["## Benchmark protocols and prerequisites", ""]
    for row in data["benchmarks"]:
        lines += [f"### {row['name']}@{row['version']}", "", f"Dataset identity: `{row['dataset_ref']}`.", "",
                  f"Protocol: {row['protocol_summary'].rstrip('.')}.", "",
                  f"Reward domain: `{row['reward_domain']}`. Lifecycle: `{row['lifecycle']}`.", ""]
        if row["prerequisites"]:
            lines += ["Additional requirements: " + "; ".join(row["prerequisites"]).rstrip('.') + ".", ""]
        lines += [f"- {limit}" for limit in row["limitations"]]
        if row["limitations"]:
            lines.append("")
    return "\n".join(lines)
