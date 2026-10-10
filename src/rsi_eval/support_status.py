"""Recorded support evidence, separate from live preflight or score claims."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re
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


def validate_support_data(data: dict, *, catalog: list[dict] | None = None,
                          evidence_root: Path | None = None) -> None:
    """Reject stale catalog/method identities and invalid evidence references.

    Source-tree tests additionally verify receipt hashes. Installed wheels expose
    the index without needing the repository's historical validation documents.
    """
    from .methods import METHODS
    catalog = _bundled_catalog() if catalog is None else catalog
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported support-status schema")
    specs = {(s["name"], s["version"]): s for s in catalog}
    rows = {(r["name"], r["version"]): r for r in data["benchmarks"]}
    if (len(specs) != len(catalog) or len(rows) != len(data["benchmarks"])
            or set(specs) != set(rows)):
        raise ValueError("Support status must cover every bundled benchmark version exactly once")
    if set(data["methods"]) != set(METHODS):
        raise ValueError("Support status must cover every managed method exactly once")
    for key, value in data["evidence"].items():
        path = Path(value["path"])
        if path.is_absolute() or ".." in path.parts or path.parts[0] != "docs":
            raise ValueError(f"Invalid evidence path: {key}")
        if not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
            raise ValueError(f"Invalid evidence hash: {key}")
        if evidence_root is not None:
            actual = hashlib.sha256((evidence_root / path).read_bytes()).hexdigest()
            if actual != value["sha256"]:
                raise ValueError(f"Evidence changed; review support status: {path}")

    def validate_record(observation: dict) -> None:
        if observation.get("status") not in {"observed", "blocked", "unavailable", "not_recorded"}:
            raise ValueError("Unknown validation observation status")
        if not observation.get("summary"):
            raise ValueError("Validation observations require a scope summary")
        if not set(observation.get("evidence", [])) <= data["evidence"].keys():
            raise ValueError("Validation observation refers to unknown evidence")
        if observation["status"] != "not_recorded" and not observation.get("evidence"):
            raise ValueError("A validation claim requires a recorded source")

    def validate_observation(observation: dict, spec: dict) -> None:
        validate_record(observation)
        task_ids = observation.get("task_ids")
        if not isinstance(task_ids, list):
            raise ValueError("Validation observations require a task list")
        if len(task_ids) != len(set(task_ids)) or not set(task_ids) <= {t["id"] for t in spec["tasks"]}:
            raise ValueError("Validation scope contains unknown or duplicate task IDs")

    for key, row in rows.items():
        spec = specs[key]
        if row.get("catalog_sha256") != _digest(spec):
            raise ValueError(f"Catalog changed; review support status for {key[0]}@{key[1]}")
        if (row.get("dataset_ref") != spec["dataset_ref"] or row.get("task_count") != len(spec["tasks"])
                or row.get("lifecycle") != spec.get("lifecycle", "active")):
            raise ValueError(f"Support status identity differs from catalog: {key}")
        if row.get("reward_domain") not in {"binary", "unit_interval", "heterogeneous"}:
            raise ValueError(f"Unknown score domain: {key}")
        if not all(row.get(k) for k in ("catalog_scope", "protocol_summary")):
            raise ValueError(f"Missing coverage or protocol scope: {key}")
        if set(row["validation"]) != {"environment", "reference", "target"}:
            raise ValueError(f"Missing validation layer: {key}")
        for observations in row["validation"].values():
            if not observations:
                raise ValueError("Use explicit not_recorded observations for missing evidence")
            for observation in observations:
                validate_observation(observation, spec)
    for name, row in data["methods"].items():
        if row.get("upstream_commit") != METHODS[name]["commit"]:
            raise ValueError(f"Method changed; review support status for {name}")
        if set(row["score_rules"]) != {"binary", "unit_interval", "heterogeneous"}:
            raise ValueError(f"Missing score compatibility rules for {name}")
        if set(row["input_modalities"]) != {"text", "image", "audio", "video"}:
            raise ValueError(f"Missing input modality declarations for {name}")
        for capability in row["capability_validation"]:
            validate_record(capability)
            if not capability.get("capability") or not capability.get("scope"):
                raise ValueError("Capability evidence requires a name and scope")
            if set(capability) & {"benchmark", "version", "task_ids", "stage"}:
                raise ValueError("Capability evidence must remain separate from benchmark observations")
        for observation in row["observations"]:
            key = (observation["benchmark"], observation["version"])
            if key not in specs:
                raise ValueError("Method observation names an unknown benchmark version")
            if observation.get("stage") not in {"evolve", "final_test", "functional_changed_candidate", "tool_route_only"}:
                raise ValueError("Unknown method validation stage")
            validate_observation(observation, specs[key])


def support_status(*, benchmark: str | None = None, method: str | None = None) -> dict:
    """Return offline recorded evidence for exact versions or a benchmark family.

    Unknown names/versions raise ValueError. Process-local custom registrations
    remain selectable but have explicitly unreviewed compatibility and evidence.
    This function performs no model, Docker, credential, or network operation.
    """
    data = json.loads(files("rsi_eval").joinpath("data/support_status.json").read_text())
    validate_support_data(data)
    if method is not None and method not in data["methods"]:
        raise ValueError(f"Unknown managed method: {method}")
    reviewed = {(r["name"], r["version"]): r for r in data["benchmarks"]}
    rows = []
    for spec in list_benchmarks():
        key = (spec["name"], spec["version"])
        if benchmark is not None and benchmark not in {spec["name"], f"{key[0]}@{key[1]}"}:
            continue
        row = deepcopy(reviewed.get(key))
        if row is None:
            row = {"name":key[0], "version":key[1], "dataset_ref":spec["dataset_ref"],
                   "task_count":len(spec["tasks"]), "lifecycle":spec.get("lifecycle", "custom"),
                   "review_status":"unreviewed_custom_registration", "catalog_scope":"caller-declared",
                   "protocol_summary":"Not reviewed", "reward_domain":"unknown", "prerequisites":[],
                   "limitations":["Registered in this process; no bundled compatibility or validation claims."],
                   "validation":{layer:[{"status":"not_recorded", "summary":"No bundled evidence.",
                                         "task_ids":[], "evidence":[]}]
                                 for layer in ("environment", "reference", "target")}}
        else:
            row["review_status"] = "recorded"
        row["method_compatibility"] = {}
        for name, details in data["methods"].items():
            if method is not None and name != method:
                continue
            observed = [o for o in details["observations"]
                        if (o["benchmark"], o["version"]) == key]
            row["method_compatibility"][name] = {
                "execution_route":details["execution_route"],
                "adapter_status":"available_subject_to_prerequisites" if key in reviewed else "unreviewed",
                "score_requirement":details["score_rules"].get(row["reward_domain"], "Unknown; caller must check the objective"),
                "evolve_observed":any(o["status"] == "observed" and o["stage"] == "evolve" for o in observed),
                "scored_trial_observed":any(o["status"] == "observed" and o["stage"] != "tool_route_only" for o in observed),
                "observations":observed,
            }
        rows.append(row)
    if benchmark is not None and not rows:
        raise ValueError(f"Unknown benchmark name or exact version: {benchmark}")
    data["benchmarks"] = rows
    if method is not None:
        data["methods"] = {method:data["methods"][method]}
    return data


def _label(row: list[dict]) -> str:
    statuses = {o["status"] for o in row}
    return "observed scoped evidence" if "observed" in statuses else ", ".join(sorted(statuses))


def format_status(data: dict, *, detailed: bool = True) -> str:
    lines = [f"Recorded support status reviewed {data['reviewed_on']}", data["scope"],
             "This is an evidence index, not a live machine preflight. Use check before run."]
    if not detailed:
        lines += ["", "CONFIGURATION | TASKS | LIFECYCLE | ENVIRONMENT / REFERENCE / TARGET"]
        for row in data["benchmarks"]:
            labels = " / ".join(_label(row["validation"][layer]) for layer in ("environment", "reference", "target"))
            lines.append(f"{row['name']}@{row['version']} | {row['task_count']} | {row['lifecycle']} | {labels}")
        lines += ["", "Observed evidence is scoped; it is not a full-dataset or all-methods claim.",
                  "Use --benchmark NAME[@VERSION] or --method NAME for task scopes, prerequisites and objective rules.",
                  "Use --json for the complete index, dataset identities and receipt paths/hashes."]
        return "\n".join(lines)
    for name, method in data["methods"].items():
        lines += ["", f"{name} input capabilities:"]
        lines += [f"  {modality}: {support}" for modality, support in method["input_modalities"].items()]
        lines += [f"  Limit: {limit}" for limit in method["execution_limits"]]
        lines += [f"  {c['capability']}, {c['status']}: {c['summary']} Scope: {c['scope']}"
                  for c in method["capability_validation"]]
    for row in data["benchmarks"]:
        lines += ["", f"{row['name']}@{row['version']} | {row['task_count']} selectable tasks | {row['lifecycle']}",
                  f"  Coverage: {row['catalog_scope']}; protocol: {row['protocol_summary']}"]
        for layer, observations in row["validation"].items():
            lines.append(f"  {layer}: {_label(observations)}")
            for o in observations:
                scope = ", ".join(o["task_ids"]) or "See stated scope"
                lines.append(f"    {o['summary']} Tasks: {scope}.")
        for name, compatibility in row["method_compatibility"].items():
            scope = "; ".join(f"{o['status']} {o['stage']}: {', '.join(o['task_ids'])}" for o in compatibility["observations"])
            lines.append(f"  {name}: {compatibility['adapter_status']}; {compatibility['score_requirement']}; "
                         + (scope or "no recorded method/benchmark execution"))
        if row["prerequisites"]:
            lines.append("  Extra prerequisites: " + "; ".join(row["prerequisites"]))
        for limit in row["limitations"]:
            lines.append("  Limit: " + limit)
    lines.append("\nUse --json for exact dataset identities, task scopes and receipt paths/hashes.")
    return "\n".join(lines)


def render_markdown(data: dict) -> str:
    """Generate docs/support-status.md from the same index used by the SDK/CLI."""
    def links(ids: list[str]) -> str:
        return ", ".join(f"[{key}]({data['evidence'][key]['path'].removeprefix('docs/')})" for key in ids)
    lines = ["# Compatibility and recorded validation", "",
             "<!-- Generated by scripts/update_support_status.py; edit src/rsi_eval/data/support_status.json. -->", "",
             f"Reviewed {data['reviewed_on']}. {data['scope']}", "",
             "Use the same packaged index from the CLI or Python. These commands make no model calls, start no containers and inspect no credentials.", "",
             "```sh", "rsi-eval status --benchmark terminal-bench@2.0 --method rrsi",
             "rsi-eval status --benchmark gpqa --json", "```", "",
             "```python", "from rsi_eval import support_status", "",
             'record = support_status(benchmark="drop@1.0-agent", method="ahe")', "```", "",
             "An exact `name@version` selects one configuration. A bare benchmark name returns every catalog version, including labeled legacy subsets. Unknown names and versions fail explicitly. Process-local custom registrations appear as unreviewed. This index describes recorded evidence; use `check` to inspect the current runtime.", "",
             "`observed` means the stated check executed for the named scope. It does not imply a passing task reward, score improvement, a full dataset run or a new check of the current source tree. `not_recorded` means this index has no such execution evidence. `blocked` names an unmet prerequisite; `unavailable` identifies an absent native reference.", "",
             "All bundled methods have reusable execution adapters. That establishes an integration route, subject to task requirements and explicit objective mapping. It does not establish a run of every method and benchmark pair. Tool-only evidence does not count as a scored trial or an evolution loop.", "",
             "## Shared runtime requirements", ""]
    lines += [f"- {p}." for p in data["common_prerequisites"]]
    lines += ["", "## Configuration overview", "", "Task counts describe selectable catalog entries, not verified model scores. Detailed scopes and limitations follow the table.", "",
              "| Exact configuration | Tasks | Catalog scope | Environment | Reference | Codex target |",
              "| --- | ---: | --- | --- | --- | --- |"]
    for row in data["benchmarks"]:
        lines.append(f"| `{row['name']}@{row['version']}` | {row['task_count']} | {row['catalog_scope']} | " + " | ".join(_label(row['validation'][k]) for k in ('environment','reference','target')) + " |")
    lines += ["", "## Method objectives and actual runs", ""]
    for name, row in data["methods"].items():
        lines += [f"### {name}", "", f"Codex adaptation of upstream `{row['upstream_commit']}`. {row['execution_route'].rstrip('.')}.", "",
                  "| Input modality | Transport support |", "| --- | --- |"]
        lines += [f"| {modality} | {support} |" for modality, support in row["input_modalities"].items()]
        lines += ["", "Execution limits:", ""]
        lines += [f"- {limit}" for limit in row["execution_limits"]]
        for capability in row["capability_validation"]:
            lines += ["", f"Capability evidence, `{capability['capability']}`, `{capability['status']}`: {capability['summary']} Scope: {capability['scope'].rstrip('.')}. {links(capability['evidence'])}."]
        lines += ["",
                  "| Benchmark reward domain | Requirement |", "| --- | --- |"]
        lines += [f"| {key} | {value} |" for key,value in row["score_rules"].items()]
        lines += ["", row["selection"], "", "Recorded scopes:", ""]
        for o in row["observations"]:
            lines.append(f"- `{o['benchmark']}@{o['version']}`, `{o['status']}`, `{o['stage']}`, tasks " + ", ".join(f"`{t}`" for t in o['task_ids']) + f": {o['summary']} {links(o['evidence'])}.")
        lines += ["", "Every other method/configuration pair has no recorded execution in this index. The shared adapter and objective policy remain available for user-selected experiments.", ""]
    lines += ["## Benchmark scopes and evidence", ""]
    for row in data["benchmarks"]:
        lines += [f"### {row['name']}@{row['version']}", "", f"Dataset identity: `{row['dataset_ref']}`.", "", f"Protocol: {row['protocol_summary'].rstrip('.')}.", "", f"Reward domain: `{row['reward_domain']}`. Lifecycle: `{row['lifecycle']}`.", ""]
        if row["prerequisites"]:
            lines += ["Additional requirements: " + "; ".join(row["prerequisites"]) + ".", ""]
        for layer, observations in row["validation"].items():
            for o in observations:
                tasks = ", ".join(f"`{t}`" for t in o['task_ids']) or "Scope described in the observation and receipt"
                lines.append(f"- **{layer}, {o['status']}**: {o['summary']} Tasks: {tasks}." + (f" Evidence: {links(o['evidence'])}." if o['evidence'] else ""))
        lines += [f"- Limit: {limit}" for limit in row["limitations"]]
        lines.append("")
    lines += ["## Maintain the index", "",
              "The packaged source is `src/rsi_eval/data/support_status.json`. It pins each complete catalog specification and method upstream revision. Catalog changes, missing configurations, unknown task scopes and broken evidence references fail validation. Source-tree tests also verify each historical receipt hash. Review evidence before updating these values.", "",
              "```sh", "python scripts/update_support_status.py", "python scripts/update_support_status.py --check", "```", "",
              "The generator never reruns an experiment. CI checks the generated page against the packaged data. Evidence links refer to repository summaries; some raw logs named in those receipts remain in private local run directories. The index packages their paths and hashes, not those raw artifacts. Wheels work without the documentation directory.", ""]
    return "\n".join(lines)
