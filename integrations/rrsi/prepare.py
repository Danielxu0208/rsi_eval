"""Create a disposable, pinned RRSI integration checkout without editing upstream."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import DOMAIN_NAME, UPSTREAM_COMMIT
from integrations.runtime_options import normalize_run_options, normalize_selection
from rsi_eval.method_policy import normalize_policy
import rsi_eval.method_policy as method_policy_module

TOOLKIT_ROOT = Path(__file__).resolve().parents[2]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def validate_evolve_scoring(selection: dict, policy: dict | None = None) -> None:
    """Require a score scale compatible with the unchanged RRSI cost gate."""
    from rsi_eval import BenchmarkSelection
    from rsi_eval.benchmarks import resolve

    spec = resolve(BenchmarkSelection(name=selection["name"], version=selection["version"]))
    if spec.get("aggregation", {}).get("type") == "frontier_native" and not (policy or {}).get("score"):
        raise ValueError(
            f"RRSI evolve cannot use {selection['name']}@{selection['version']}: "
            "its native selection requires comparable rewards in [0, 1]. "
            "Provide method_policy.score with an explicit task-specific mapping. "
            "Smoke and final test selections remain supported."
        )


def freeze_selection(selection: dict) -> tuple[dict, dict]:
    """Resolve once, preserve ordering and pin exact selected task content."""
    from rsi_eval import BenchmarkSelection
    from rsi_eval.benchmarks import resolve

    requested = copy.deepcopy(selection)
    spec = resolve(BenchmarkSelection(selection["name"], selection["version"],
                   tuple(selection["task_ids"]) if selection.get("task_ids") is not None else None,
                   Path(selection["manifest"]) if selection.get("manifest") else None))
    frozen = {**selection, "task_ids": list(spec["task_ids"])}
    frozen.pop("manifest", None)
    source_identity = {k: spec.get(k) for k in ("name", "version", "dataset_ref", "source", "tasks", "protocol", "aggregation")}
    provenance = {"requested": requested, "selection": spec["selection"],
                  "task_ids": spec["task_ids"], "source_identity": source_identity,
                  "sha256": hashlib.sha256(json.dumps(source_identity, sort_keys=True).encode()).hexdigest()}
    if requested.get("manifest"):
        provenance["manifest_sha256"] = hashlib.sha256(Path(requested["manifest"]).read_bytes()).hexdigest()
    return frozen, provenance


def verify_frozen_selections(config: dict) -> None:
    for role, saved in config.get("selection_provenance", {}).items():
        _, current = freeze_selection(config[role])
        if current["sha256"] != saved["sha256"] or current["task_ids"] != saved["task_ids"]:
            raise ValueError(f"Frozen {role} benchmark content changed; prepare a new experiment")


def normalize_config(data: dict, base: Path) -> dict:
    config = copy.deepcopy(data)
    codex = config.setdefault("codex", {})
    if not codex.get("model") or not codex.get("auth_dir") or not codex.get("expected_version"):
        raise ValueError("codex.model, codex.auth_dir and codex.expected_version are required")
    auth_dir = Path(codex["auth_dir"]).expanduser()
    codex["auth_dir"] = str((base / auth_dir).resolve() if not auth_dir.is_absolute() else auth_dir.resolve())
    if "/" in codex.get("executable", "codex"):
        executable = Path(codex["executable"]).expanduser()
        codex["executable"] = str((base / executable).resolve() if not executable.is_absolute() else executable.resolve())
    config.setdefault("policy_model", codex["model"])
    for role in ("evolve", "test", "smoke"):
        if role not in config:
            continue
        selection = config[role]
        if not selection.get("name") or not selection.get("version"):
            raise ValueError(f"{role}.name and {role}.version are required")
        if selection.get("manifest"):
            path = Path(selection["manifest"]).expanduser()
            selection["manifest"] = str((base / path).resolve() if not path.is_absolute() else path.resolve())
        ids = selection.get("task_ids")
        if ids is not None and (not isinstance(ids, (list, tuple)) or not ids or not all(isinstance(x, str) and x for x in ids) or len(ids) != len(set(ids))):
            raise ValueError(f"{role}.task_ids must be a nonempty unique list")
        config[role] = normalize_selection(selection, base)
    if "evolve" not in config:
        raise ValueError("evolve benchmark selection is required")
    provenance = copy.deepcopy(config.get("selection_provenance", {}))
    if provenance:
        verify_frozen_selections(config)
    for role in ("evolve", "smoke"):
        if role in config and role not in provenance:
            config[role], provenance[role] = freeze_selection(config[role])
    if "smoke" not in config:
        config["smoke"] = {**config["evolve"], "task_ids": config["evolve"]["task_ids"][:1]}
        _, provenance["smoke"] = freeze_selection(config["smoke"])
        provenance["smoke"]["origin"] = "first_frozen_evolve_task"
    config["selection_provenance"] = provenance
    if config.get("method_policy"):
        config["method_policy"] = normalize_policy(config["method_policy"], base)
    validate_evolve_scoring(config["evolve"], config.get("method_policy"))
    defaults = {"T": 2, "k": 1, "m": 1, "eval_parallel": 1, "delta": 0.017,
                "beta0": 0.1, "beta1": 44.5, "w_s": 0.0, "w_c": 15.0, "w_n": 0.5}
    config["rrsi"] = {**defaults, **config.get("rrsi", {})}
    for key in ("T", "k", "m", "eval_parallel"):
        if not isinstance(config["rrsi"][key], int) or config["rrsi"][key] < 1:
            raise ValueError(f"rrsi.{key} must be a positive integer")
    if config["rrsi"]["m"] > 8:
        raise ValueError("Upstream RRSI supports at most eight candidate labels")
    for role in ("proposer_model", "analyst_model", "critic_model"):
        config["rrsi"][role] = codex["model"]
    config.setdefault("run_options", {})
    config["run_options"].setdefault("concurrency", 1)
    config["run_options"] = normalize_run_options(config["run_options"], base)
    return config


def patch_transport(path: Path) -> None:
    """Replace only the implementation of llm.generate; preserve its signature."""
    source = path.read_text()
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "generate")
    first_body_line = function.body[0].lineno - 1
    lines = source.splitlines(keepends=True)
    replacement = '''    # rsi-eval transport bridge; search/action/selection code is upstream.
    from pathlib import Path
    from integrations.rrsi.backend import generate as codex_generate
    text = codex_generate(
        prompt, system=system, max_retries=max_retries, json_only=json_only,
        model=model, max_tokens=max_tokens, cache_prefix=cache_prefix,
        config_path=Path(__file__).resolve().parent.parent / "domains" / "coding_codex" / "experiment.json",
    )
    return extract_json(text) if json_only else text
'''
    path.write_text("".join(lines[:first_body_line]) + replacement + "".join(lines[function.end_lineno:]))


def prepare(source: Path, checkout: Path, config: dict, *, seed: Path | None = None) -> dict:
    source, checkout = source.resolve(), checkout.resolve()
    if checkout == source or source in checkout.parents:
        raise ValueError("Managed checkout must be outside the original RRSI checkout")
    actual = git(source, "rev-parse", f"{UPSTREAM_COMMIT}^{{commit}}")
    if actual != UPSTREAM_COMMIT:
        raise ValueError("Pinned RRSI commit is not available in source")
    from rsi_eval.resources import codex_template
    seed = (seed or codex_template()).resolve()
    if config.get("method_policy"):
        config = copy.deepcopy(config)
        config["method_policy"] = normalize_policy(config["method_policy"], TOOLKIT_ROOT, candidate_roots=[seed])
    verify_frozen_selections(config)
    if not (seed / "agent.py").is_file() or not (seed / "prompt.md").is_file():
        raise ValueError(f"Codex seed template is missing: {seed}")
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    bridge_hash = hashlib.sha256()
    for base, paths in (
        (Path(__file__).parent, [Path(__file__), Path(__file__).with_name("__main__.py"), Path(__file__).with_name("backend.py"), Path(__file__).with_name("domain_bridge.py"), Path(__file__).with_name("failures.py"), Path(__file__).with_name("selection.py"), *sorted((Path(__file__).parent / "domain").rglob("*"))]),
        (TOOLKIT_ROOT / "integrations", [TOOLKIT_ROOT / "integrations/runtime_options.py"]),
        (Path(method_policy_module.__file__).parent, [Path(method_policy_module.__file__)]),
        (seed, sorted(seed.rglob("*"))),
    ):
        for path in paths:
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                bridge_hash.update(str(path.relative_to(base)).encode() + b"\0" + path.read_bytes() + b"\0")
    integration_hash = bridge_hash.hexdigest()
    marker_name = ".rsi-eval-integration.json"
    if checkout.exists():
        marker = checkout / marker_name
        if not marker.is_file():
            raise FileExistsError(f"Refusing to overwrite existing directory {checkout}")
        saved = json.loads(marker.read_text())
        if (saved.get("upstream_commit") != UPSTREAM_COMMIT or saved.get("config_sha256") != fingerprint
                or saved.get("integration_sha256") != integration_hash):
            raise ValueError("Existing checkout has a different configuration, seed or bridge; choose a new path")
        if git(checkout, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Existing managed checkout has tracked changes")
        return {**saved, "checkout": str(checkout), "integration_commit": git(checkout, "rev-parse", "HEAD")}
    checkout.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(source), str(checkout)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Cannot create managed checkout: {result.stderr}")
    git(checkout, "checkout", "--detach", UPSTREAM_COMMIT)
    domain = checkout / "domains" / DOMAIN_NAME
    shutil.copytree(Path(__file__).parent / "domain", domain)
    shutil.copytree(seed, domain / "harness", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (domain / "experiment.json").write_text(json.dumps(config, indent=2) + "\n")
    (domain / "rrsi.json").write_text(json.dumps(config["rrsi"], indent=2) + "\n")
    patch_transport(checkout / "rrsi" / "llm.py")
    if config.get("method_policy", {}).get("selection"):
        loop = checkout / "rrsi/loop.py"
        text = loop.read_text()
        anchor = "from .selection import Candidate, select_round"
        if text.count(anchor) != 1:
            raise RuntimeError("Pinned RRSI selection hook no longer applies")
        loop.write_text(text.replace(anchor, "from .selection import Candidate\nfrom integrations.rrsi.selection import select_round"))
    marker_data = {"format": 1, "upstream_commit": UPSTREAM_COMMIT, "config_sha256": fingerprint,
                   "integration_sha256": integration_hash}
    (checkout / marker_name).write_text(json.dumps(marker_data, indent=2) + "\n")
    git(checkout, "add", "--", f"domains/{DOMAIN_NAME}", "rrsi/llm.py", "rrsi/loop.py", marker_name)
    env = {**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"}
    commit = subprocess.run(["git", "-C", str(checkout), "-c", "user.name=rsi-eval", "-c", "user.email=rsi-eval@localhost", "commit", "-m", "Add pinned Codex evaluation integration"], env=env, capture_output=True, text=True)
    if commit.returncode:
        raise RuntimeError(f"Integration commit failed: {commit.stderr}")
    return {**marker_data, "checkout": str(checkout), "integration_commit": git(checkout, "rev-parse", "HEAD")}
