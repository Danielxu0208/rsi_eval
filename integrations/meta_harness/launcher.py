"""Run upstream Meta-Harness with Codex proposals and the shared evaluation SDK.

The upstream search/history/frontier functions are executed directly. This module
replaces execution boundaries, not the search algorithm.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import importlib.util
import io
import inspect
import json
import os
import re
import shutil
import subprocess
import tarfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from integrations.runtime_options import effective_options, normalize_run_options, normalize_selection
from integrations.meta_harness.state import RunStore, write_json

UPSTREAM_COMMIT = "8123ccabe2b19fa1123090b2e5f1bacc20963ce8"
EXAMPLE = Path("reference_examples/terminal_bench_2")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = "rsi_eval.harbor_codex:Agent"


class IntegrationError(RuntimeError):
    """The method did not complete a valid evaluation chain."""


class CandidateSmokeFailed(IntegrationError):
    """A measured candidate execution failed the native liveness check."""


def _write_json(path: Path, value: Any) -> None:
    write_json(path, value)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path, *, exclude_runs: bool = False) -> dict[str, str]:
    """Hash code, not generated runs, for immutable-history checks."""
    found = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(p in {"__pycache__", ".git"} for p in relative.parts):
            continue
        if exclude_runs and relative.parts[0] in {"logs", "jobs"}:
            continue
        if path.is_symlink():
            raise IntegrationError(f"Symlink not allowed in candidate workspace: {relative}")
        if path.is_file():
            found[str(relative)] = _hash(path)
    return found


def _evidence_state(*roots: Path) -> dict[Path, tuple[str, str]]:
    """Protect existing feedback, including native trials and past sessions."""
    state = {}
    for root in roots:
        for path in root.rglob("*"):
            if path.is_symlink():
                state[path] = ("symlink", os.readlink(path))
            elif path.is_file():
                state[path] = ("file", _hash(path))
    return state


def prepare_checkout(source: Path, destination: Path) -> Path:
    """Export the pinned commit without writing anything to the source clone."""
    if destination.exists():
        raise IntegrationError(f"Managed checkout already exists: {destination}; use a new output directory")
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", f"{UPSTREAM_COMMIT}^{{commit}}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if revision != UPSTREAM_COMMIT:
        raise IntegrationError("Upstream revision does not match the pinned commit")
    archive = subprocess.run(
        ["git", "-C", str(source), "archive", UPSTREAM_COMMIT, str(EXAMPLE), "LICENSE"],
        check=True, capture_output=True,
    ).stdout
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            target = destination / member.name
            if not target.resolve().is_relative_to(destination.resolve()):
                raise IntegrationError(f"Unsafe archive path: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = tar.extractfile(member)
                assert stream is not None
                target.write_bytes(stream.read())
                target.chmod(member.mode)
            else:
                raise IntegrationError(f"Unsupported archive member: {member.name}")
    example = destination / EXAMPLE
    script = example / "meta_harness.py"
    original = script.read_text()
    # These dependencies only serve the replaced Claude execution boundary and
    # implicit credential loading. Inherited Codex auth is configured explicitly.
    patched = original.replace("import claude_wrapper\n", "", 1).replace(
        "from dotenv import load_dotenv\n", "", 1
    ).replace("load_dotenv(override=True)\n", "", 1)
    if original == patched or "load_dotenv" in patched or "import claude_wrapper" in patched:
        raise IntegrationError("Pinned upstream import patch no longer applies")
    script.write_text(patched)
    _write_json(destination / "managed_checkout.json", {
        "upstream": "https://github.com/stanford-iris-lab/meta-harness",
        "commit": UPSTREAM_COMMIT,
        "source_clone": str(source.resolve()),
        "export": str(EXAMPLE),
        "patches": ["remove unused Claude wrapper import", "disable implicit dotenv loading"],
        "original_script_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "patched_script_sha256": _hash(script),
    })
    return example


def load_upstream(example: Path):
    name = "_meta_harness_" + hashlib.sha256(str(example).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, example / "meta_harness.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _selection_list(value: Any) -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else [value]
    if not values or any(not isinstance(v, dict) for v in values):
        raise IntegrationError("evolve and test must contain benchmark selection objects")
    return values


def normalize_config(value: dict[str, Any], base: Path) -> dict[str, Any]:
    """Resolve runtime paths before the method changes working directories."""
    from rsi_eval.config import _path
    from rsi_eval.method_policy import normalize_policy
    config = deepcopy(value)
    for key in ("evolve", "test", "smoke"):
        if key in config:
            selections = [normalize_selection(item, base) for item in _selection_list(config[key])]
            config[key] = selections if isinstance(config[key], list) else selections[0]
    config["run_options"] = normalize_run_options(config.get("run_options", {}), base)
    config["method_policy"] = normalize_policy(config.get("method_policy"), base)
    if config.get("codex", {}).get("auth_dir"):
        config["codex"]["auth_dir"] = str(_path(config["codex"]["auth_dir"], base))
    if "/" in config.get("harbor_executable", "harbor"):
        config["harbor_executable"] = str(_path(config["harbor_executable"], base))
    return config


def validate_evolve_scoring(value: Any, policy: dict | None = None) -> None:
    """Check benchmark-wide score semantics before starting the native loop."""
    from rsi_eval import BenchmarkSelection
    from rsi_eval.benchmarks import resolve

    for selection in _selection_list(value):
        spec = resolve(BenchmarkSelection(name=selection["name"], version=selection["version"]))
        if not (policy or {}).get("score") and spec.get("aggregation", {}).get("type") in {"drop_em_f1", "frontier_native", "rubric_mean"}:
            raise IntegrationError(
                f"Meta-Harness evolve cannot use {selection['name']}@{selection['version']}: "
                "its native selection counts reward > 0 as a pass. "
                "Configure method_policy.score to map raw metrics to binary outcomes explicitly. "
                "Smoke and final test selections remain supported."
            )


class Bindings:
    def __init__(self, module, config: dict[str, Any], settings, evaluate_fn: Callable,
                 codex_fn: Callable, sdk_types: tuple, output_dir: Path, store: RunStore):
        self.module = module
        self.config = config
        self.settings = settings
        self.evaluate_fn = evaluate_fn
        self.codex_fn = codex_fn
        self.TargetSpec, self.BenchmarkSelection, self.RunOptions = sdk_types
        self.output_dir = output_dir
        self.store = store
        saved = store.data.get("bindings", {})
        self.evaluations: list[dict[str, Any]] = saved.get("evaluations", [])
        self.candidates: dict[str, str] = saved.get("candidates", {"codex-baseline": "agents.baseline_codex:AgentHarness"})
        self.validated: dict[str, dict[str, str]] = saved.get("validated", {})
        self.proposed: dict[str, dict[str, str]] = saved.get("proposed", {})
        self.measured: dict[str, dict[str, Any]] = saved.get("measured", {})
        self.policy_records: list[dict] = saved.get("policy_records", [])
        self.frontier_applied: list[str] = saved.get("frontier_applied", [])
        self.settled_rounds: dict[str, dict] = saved.get("settled_rounds", {})
        self.native_parser = module.parse_job_results
        self.native_frontier = module.update_frontier
        self.native_history = module.update_evolution_summary

    def state(self) -> dict:
        return {"evaluations": self.evaluations, "candidates": self.candidates,
                "validated": self.validated, "proposed": self.proposed,
                "measured": self.measured, "policy_records": self.policy_records,
                "frontier_applied": self.frontier_applied, "settled_rounds": self.settled_rounds}

    def checkpoint(self) -> None:
        self.store.save(self.state())

    def _native_write(self, attribute: str, function, *args, **kwargs) -> None:
        path = getattr(self.module, attribute)
        staged = self.output_dir / f"native-{attribute}.staged"
        if path.exists():
            shutil.copyfile(path, staged)
        elif staged.exists():
            staged.unlink()
        setattr(self.module, attribute, staged)
        try:
            function(*args, **kwargs)
            content = staged.read_text()
        finally:
            setattr(self.module, attribute, path)
        self.store.commit_native(path, content, self.state())
        staged.unlink(missing_ok=True)

    def update_history(self, *args, **kwargs) -> None:
        iteration, candidates, results = args[:3]
        self.settled_rounds[str(iteration)] = {
            "iteration": iteration, "status": "evaluated" if results else "rejected",
            "candidates": [candidate["name"] for candidate in candidates],
            "evaluated": list(results),
        }
        self._native_write("EVOLUTION_SUMMARY", self.native_history, *args, **kwargs)

    def count_iterations(self) -> int:
        return max(map(int, self.settled_rounds), default=0)

    def update_frontier(self, candidates_results) -> None:
        from rsi_eval.method_policy import select_candidate
        transaction_key = hashlib.sha256(json.dumps(candidates_results, sort_keys=True).encode()).hexdigest()
        if transaction_key in self.frontier_applied:
            return
        policy = self.config.get("method_policy", {})
        before = json.loads(self.module.FRONTIER_VAL.read_text()) if self.module.FRONTIER_VAL.exists() else {}
        for name, (per_task, avg) in candidates_results.items():
            self.measured[name] = {"id": name, "score": avg, "per_task": per_task,
                                   "artifact_dir": str(self.artifact(self.candidates[name])),
                                   "evaluations": [row["result"] for row in self.evaluations
                                                   if row["candidate"] == self.candidates[name]
                                                   and row["phase"] == "evolve" and row.get("execution_complete")]}

        def write():
            self.native_frontier(candidates_results)
            if not policy.get("selection"):
                return
            frontier = json.loads(self.module.FRONTIER_VAL.read_text())
            native = frontier.get("_best", {}).get("agent")
            winner = select_candidate(policy, list(self.measured.values()), native,
                                      context={"method": "meta-harness", "incumbent": before.get("_best", {}).get("agent")})
            if winner is None:
                winner = before.get("_best", {}).get("agent")
            if winner is None:
                raise IntegrationError("Selection policy must choose a measured baseline before search")
            frontier["_best"] = {"agent": winner, "avg_pass_rate": self.measured[winner]["score"]}
            _write_json(self.module.FRONTIER_VAL, frontier)
            self.policy_records.append({"kind": "selection", "native_choice": native,
                                        "selected": winner, "eligible": list(self.measured)})
        self.frontier_applied.append(transaction_key)
        self._native_write("FRONTIER_VAL", write)
        _write_json(self.output_dir / "method-policy.json", self.policy_records)

    def parse_job_results(self, directory, expected_trials=None):
        """Expose mapped values only to method selection; preserve raw files."""
        from rsi_eval.method_policy import score_trial
        policy = self.config.get("method_policy", {})
        if not policy.get("score"):
            return self.native_parser(directory, expected_trials)
        task_rewards: dict[str, list[float]] = {}
        for trial_dir in sorted(directory.iterdir()):
            if not trial_dir.is_dir() or "__" not in trial_dir.name:
                continue
            native_dir = trial_dir.resolve().parent
            record = next((row for row in reversed(self.evaluations)
                           if Path(row["result"]["artifact_dir"]).resolve() == native_dir
                           or any(item.get("artifact_dir") and Path(item["artifact_dir"]).resolve() == trial_dir.resolve()
                                  for item in row["result"]["trials"])), None)
            if record is None:
                raise IntegrationError(f"Score policy cannot identify evaluation evidence: {trial_dir}")
            raw = json.loads((trial_dir / "result.json").read_text())
            rewards = (raw.get("verifier_result") or {}).get("rewards") or {}
            trial = next((item for item in record["result"]["trials"]
                          if item.get("artifact_dir") and Path(item["artifact_dir"]).resolve() == trial_dir.resolve()), None)
            if trial is None:
                trial = {"reward": rewards.get("reward"), "metrics": rewards,
                         "task_id": raw.get("task_name"), "status": "completed"}
            value = score_trial(policy, trial, record["selection"], require_binary=True)
            task = trial_dir.name.rsplit("__", 1)[0]
            task_rewards.setdefault(task, []).append(value)
            self.policy_records.append({"kind": "score", "artifact_dir": str(trial_dir.resolve()),
                                        "raw_reward": trial.get("reward"), "method_score": value})
        _write_json(self.output_dir / "method-policy.json", self.policy_records)
        return task_rewards

    def artifact(self, import_path: str) -> Path:
        match = re.fullmatch(r"agents\.([a-z][a-z0-9_]*):AgentHarness", import_path)
        if not match:
            raise IntegrationError(f"Invalid native candidate reference: {import_path}")
        result = self.module.AGENTS_DIR / match.group(1)
        if not result.is_dir() or result.is_symlink():
            raise IntegrationError(f"Missing candidate artifact directory: {result}")
        _files(result)
        return result

    def selection(self, value: dict[str, Any]):
        spec = dict(value)
        spec.pop("benchmark_runtime", None)
        spec.pop("runtime_snapshot", None)
        if spec.get("task_ids") is not None:
            spec["task_ids"] = tuple(spec["task_ids"])
        if spec.get("manifest") is not None:
            spec["manifest"] = Path(spec["manifest"]).resolve()
        return self.BenchmarkSelection(**spec)

    def target(self, import_path: str):
        return self.TargetSpec(
            artifact_dir=self.artifact(import_path), entrypoint=ENTRYPOINT,
            model=self.settings.model,
            config={"codex_auth_dir": str(self.settings.auth_dir.resolve()),
                    "codex_version": self.settings.expected_version,
                    "reasoning_effort": self.settings.reasoning_effort},
        )

    def evaluate_many(self, import_path: str, job_name: str, selections: list[dict],
                      repetitions: int, concurrency: int, phase: str) -> Path:
        native_dirs = []
        for index, selection in enumerate(selections):
            output = self.module.JOBS_DIR / job_name / f"benchmark-{index}"
            option_data = effective_options(self.config.get("run_options", {}), selection)
            option_data.setdefault("harbor_executable", self.config.get("harbor_executable", "harbor"))
            option_data.setdefault("environment", self.config.get("environment", "docker"))
            option_data.setdefault("timeout", self.config.get("evaluation_timeout"))
            options = self.RunOptions(**{**option_data, "repetitions": repetitions, "concurrency": concurrency})
            previous = next((row for row in reversed(self.evaluations) if row["output_dir"] == str(output)), None)
            if previous and previous.get("execution_complete"):
                data = previous["result"]
                result = SimpleNamespace(**{**data, "trials": [SimpleNamespace(**trial) for trial in data["trials"]]},
                                         to_dict=lambda data=data: data)
            else:
                kwargs = {"target": self.target(import_path), "benchmark": self.selection(selection),
                          "options": options, "output_dir": output}
                if output.exists() and any(output.iterdir()):
                    if "resume" not in inspect.signature(self.evaluate_fn).parameters:
                        raise IntegrationError("Incomplete evaluation requires an evaluate callable supporting resume=True")
                    kwargs["resume"] = True
                try:
                    result = self.evaluate_fn(**kwargs)
                except BaseException:
                    # SDK owns this output tree and its per-trial recovery. No
                    # candidate or successful history is reconstructed here.
                    self.checkpoint()
                    raise
            record = {"phase": phase, "candidate": import_path, "selection": selection,
                      "output_dir": str(output), "result": result.to_dict(),
                      "execution_complete": result.status == "completed" and bool(result.trials) and all(
                          trial.status in {"completed", "task_timeout", "agent_error"} and trial.reward is not None
                          for trial in result.trials)}
            if not previous or not previous.get("execution_complete"):
                record["attempt"] = 1 + sum(row["output_dir"] == str(output) for row in self.evaluations)
                self.evaluations.append(record)
                _write_json(self.output_dir / "evaluations.json", self.evaluations)
                self.checkpoint()
            if result.status != "completed" or not result.trials or any(
                trial.status not in {"completed", "task_timeout", "agent_error"}
                or trial.reward is None for trial in result.trials
            ):
                raise IntegrationError(f"{phase} evaluation did not complete: {output / 'result.json'}")
            if phase == "smoke" and any(trial.status != "completed" for trial in result.trials):
                raise CandidateSmokeFailed(f"Candidate failed smoke liveness: {output / 'result.json'}")
            native_dirs.append(Path(result.artifact_dir))
        if len(native_dirs) == 1:
            return native_dirs[0]
        # The native loop expects one directory of Harbor trial directories.
        # Symlink only; raw results remain untouched in their SDK output trees.
        view = self.module.JOBS_DIR / job_name / "native-view"
        view.mkdir(parents=True, exist_ok=True)
        for index, native in enumerate(native_dirs):
            selection = selections[index]
            prefix = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"{index}-{selection['name']}@{selection.get('version', '')}")
            for trial_dir in native.iterdir():
                if trial_dir.is_dir() and "__" in trial_dir.name:
                    link = view / f"{prefix}--{trial_dir.name}"
                    if not link.exists():
                        link.symlink_to(trial_dir.resolve(), target_is_directory=True)
        self.checkpoint()
        return view

    def harbor_run(self, import_path, job_name, n_trials=1, n_concurrent=1):
        directory = self.evaluate_many(import_path, job_name, _selection_list(self.config["evolve"]),
                                       n_trials, n_concurrent, "evolve")
        return directory, True

    def smoke_test(self, name, import_path, timeout=1800):
        smoke = self.config.get("smoke")
        if smoke is None:
            smoke = []
            for selection in _selection_list(self.config["evolve"]):
                if not selection.get("task_ids"):
                    raise IntegrationError("Specify smoke selection when evolve uses a manifest or all tasks")
                smoke.append({**selection, "task_ids": [selection["task_ids"][0]]})
        try:
            self.evaluate_many(import_path, f"smoke-{name}", _selection_list(smoke), 1, 1, "smoke")
        except CandidateSmokeFailed as exc:
            print(str(exc))
            return False
        return True

    def validate_candidate(self, name, import_path):
        try:
            artifact = self.artifact(import_path)
            files = _files(artifact)
            source = artifact / "agent.py"
            tree = ast.parse(source.read_text(), filename=str(source))
            if not any(isinstance(node, ast.FunctionDef) and node.name == "build_prompt"
                       for node in tree.body):
                raise IntegrationError("agent.py must define synchronous build_prompt(instruction, artifact_dir)")
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in {"prepare_environment", "after_run"}:
                    raise IntegrationError(f"Optional hook {node.name} must be async")
            for file in artifact.rglob("*.py"):
                compile(file.read_text(), str(file), "exec")
            parent_snapshots = self.proposed.get(name, {})
            if not parent_snapshots:
                raise IntegrationError("Candidate was not registered by the proposer")
            digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            if digest in parent_snapshots.values():
                raise IntegrationError("Candidate is an unchanged copy of a previous artifact")
            self.validated[name] = files
            self.candidates[name] = import_path
            self.checkpoint()
            return True
        except (IntegrationError, SyntaxError, OSError) as exc:
            print(f"Candidate {name} rejected: {exc}")
            return False

    def render_task_prompt(self, iteration, n_trials):
        from rsi_eval.method_policy import policy_identity
        module = self.module
        skill = (Path(__file__).parent / "SKILL.md").read_text()
        return (f"{skill}\n\nRun iteration {iteration}. Produce exactly ONE new candidate.\n"
                f"Model: {self.settings.model}; evolve trials per task: {n_trials}.\n"
                f"Evolve selections: {json.dumps(self.config['evolve'])}\n"
                f"Explicit method policy: {json.dumps(policy_identity(self.config.get('method_policy')))}\n"
                f"Read history: {module.EVOLUTION_SUMMARY}\nRead frontier: {module.FRONTIER_VAL}\n"
                f"Settled round outcomes (including no proposals): {json.dumps(self.settled_rounds)}\n"
                f"Read successful and failed native trajectories under: {module.JOBS_DIR}\n"
                f"Write pending JSON at: {module.PENDING_EVAL}\n"
                "Baseline artifact: agents/baseline_codex/. History candidate references "
                "agents.NAME:AgentHarness refer to full artifact directories agents/NAME/.\n")

    def propose(self, task_prompt, iteration, timeout=2400):
        module = self.module
        saved = self.store.data["proposals"].get(str(iteration))
        if saved:
            self.proposed[saved["candidate"]["name"]] = saved["previous_artifacts"]
            _write_json(module.PENDING_EVAL, {"iteration": iteration, "candidates": [saved["candidate"]]})
            return True
        before = _files(module.EVOLVE_DIR, exclude_runs=True)
        evidence_roots = (module.EVOLVE_DIR / "logs", module.EVOLVE_DIR / "jobs")
        evidence_before = _evidence_state(*evidence_roots)
        attempt = 1 + sum(row["iteration"] == iteration for row in self.store.data["proposer_attempts"])
        session_dir = module.LOGS_DIR / "codex_sessions" / f"iter{iteration}-attempt{attempt}"
        prior = {}
        for directory in module.AGENTS_DIR.iterdir():
            if directory.is_dir() and (directory / "agent.py").exists():
                prior[directory.name] = hashlib.sha256(json.dumps(_files(directory), sort_keys=True).encode()).hexdigest()
        self.store.begin_proposer(iteration, session_dir, before)
        try:
            result = self.codex_fn(task_prompt, settings=self.settings, cwd=module.EVOLVE_DIR,
                                   mode="edit", log_dir=session_dir)
            self.store.data["active_proposer"]["usage"] = getattr(result, "usage", None)
            _write_json(self.store.path, self.store.data)
        except BaseException as exc:
            # Keep the active write-ahead receipt. Resume archives partial new
            # candidates and retains session logs before a fresh attempt.
            self.store.data["active_proposer"].update(
                status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                error=f"{type(exc).__name__}: {exc}")
            _write_json(self.store.path, self.store.data)
            raise
        after = _files(module.EVOLVE_DIR, exclude_runs=True)
        changed = [path for path, digest in before.items() if after.get(path) != digest]
        if changed:
            raise IntegrationError(f"Proposer modified immutable upstream/history files: {changed}")
        evidence_after = _evidence_state(*evidence_roots)
        changed_evidence = [str(path) for path, identity in evidence_before.items()
                            if evidence_after.get(path) != identity]
        if changed_evidence:
            raise IntegrationError(f"Proposer modified existing evaluation evidence: {changed_evidence}")
        extra_evidence = [str(path) for path in evidence_after.keys() - evidence_before.keys()
                          if path != module.PENDING_EVAL and not path.is_relative_to(session_dir)]
        if extra_evidence:
            raise IntegrationError(f"Proposer created unexpected evaluation evidence: {extra_evidence}")
        if not module.PENDING_EVAL.exists():
            self.settled_rounds[str(iteration)] = {
                "iteration": iteration, "status": "no_candidate", "candidates": [], "evaluated": []}
            self.store.data["bindings"] = self.state()
            self.store.finish_proposer(status="no_candidate", usage=getattr(result, "usage", None))
            return False
        pending = json.loads(module.PENDING_EVAL.read_text())
        candidates = pending.get("candidates", [])
        if pending.get("iteration") != iteration or len(candidates) != 1:
            raise IntegrationError("Each iteration must submit exactly one candidate with matching iteration")
        candidate = candidates[0]
        name, reference = candidate.get("name", ""), candidate.get("import_path", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or reference != f"agents.{name}:AgentHarness":
            raise IntegrationError("Candidate name and import_path must identify the same artifact directory")
        if name in prior:
            raise IntegrationError("Candidate must use a new immutable artifact directory")
        self.artifact(reference)
        extra = [path for path in after.keys() - before.keys() if not Path(path).is_relative_to(Path("agents") / name)]
        if extra:
            raise IntegrationError(f"Proposer created files outside the new candidate artifact: {extra}")
        self.proposed[name] = prior
        receipt = {
            "iteration": iteration, "candidate": candidate, "previous_artifacts": prior,
            "codex_usage": getattr(result, "usage", None),
            "artifact_files": _files(self.artifact(reference)),
        }
        _write_json(module.LOGS_DIR / f"proposal-{iteration}.json", receipt)
        self.store.data["proposals"][str(iteration)] = receipt
        self.store.data["bindings"] = self.state()
        self.store.finish_proposer(status="completed", usage=getattr(result, "usage", None))
        return True


def _run_experiment(config: dict[str, Any], output_dir: Path, *, source: Path | None = None,
                   template: Path | None = None, settings=None, evaluate_fn=None,
                   codex_fn=None, sdk_types=None, resume: bool = False) -> dict[str, Any]:
    from rsi_eval.method_policy import normalize_policy, policy_identity
    config = normalize_config(config, Path.cwd())
    validate_evolve_scoring(config["evolve"], config.get("method_policy"))
    if evaluate_fn is None or sdk_types is None:
        from rsi_eval import evaluate, TargetSpec, BenchmarkSelection, RunOptions
        evaluate_fn = evaluate_fn or evaluate
        sdk_types = sdk_types or (TargetSpec, BenchmarkSelection, RunOptions)
    if codex_fn is None or settings is None:
        from rsi_eval.codex import CodexSettings, run_codex, codex_version
        codex_fn = codex_fn or run_codex
        if settings is None:
            codex_config = dict(config["codex"])
            codex_config["auth_dir"] = Path(codex_config["auth_dir"]).expanduser().resolve()
            settings = CodexSettings(**codex_config)
        settings = replace(settings, expected_version=codex_version(settings))
    output_dir = output_dir.resolve()
    if not resume and output_dir.exists() and any(path.name != ".meta-run.lock" for path in output_dir.iterdir()):
        raise IntegrationError("Use a fresh output directory, or --resume to continue a recorded experiment")
    if resume and not output_dir.exists():
        raise IntegrationError("Cannot resume a missing experiment directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    iterations = config.get("iterations", 2)
    if not isinstance(iterations, int) or iterations < 1:
        raise IntegrationError("iterations must be a positive integer")
    _selection_list(config["evolve"])
    _selection_list(config["test"])
    if template is None:
        from rsi_eval.resources import codex_template
        template = codex_template()
    from rsi_eval import method_policy
    from rsi_eval.benchmarks import list_benchmarks
    from rsi_eval.resume import implementation_identity
    manifests = {str(item["manifest"]): _hash(Path(item["manifest"]))
                 for key in ("evolve", "smoke", "test") if key in config
                 for item in _selection_list(config[key]) if item.get("manifest")}
    identity = {"upstream_commit": UPSTREAM_COMMIT, "config": config,
                "template": _files(template), "settings": vars(settings),
                "sdk_implementation": implementation_identity(), "manifests": manifests,
                "catalog_sha256": hashlib.sha256(json.dumps(list_benchmarks(), sort_keys=True).encode()).hexdigest(),
                "integration_sources": {str(path.name): _hash(path) for path in (
                    Path(__file__), Path(__file__).with_name("state.py"), Path(__file__).with_name("SKILL.md"),
                    Path(__file__).parent.parent / "runtime_options.py", Path(method_policy.__file__))}}
    identity = json.loads(json.dumps(identity, default=str))
    config["method_policy"] = normalize_policy(config.get("method_policy"), Path.cwd(),
        candidate_roots=(output_dir / "upstream" / EXAMPLE / "agents", template))
    try:
        store = RunStore(output_dir, identity, resume=resume)
    except ValueError as exc:
        raise IntegrationError(str(exc)) from exc
    if resume:
        completed = output_dir / "result.json"
        committed_summary = store.data.get("summary")
        if committed_summary is not None:
            if not completed.exists() or json.loads(completed.read_text()) != committed_summary:
                raise IntegrationError("Resume rejected: completed method report changed")
            return committed_summary
        store.archive_result()
        module = load_upstream(output_dir / "upstream" / EXAMPLE)
    else:
        _write_json(output_dir / "experiment.json", {"upstream_commit": UPSTREAM_COMMIT, "config": config,
                                                   "method_policy": policy_identity(config["method_policy"])})
        module = load_upstream(prepare_checkout(source or PROJECT_ROOT / "meta-harness", output_dir / "upstream"))
        shutil.copytree(template, module.AGENTS_DIR / "baseline_codex", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        store.save()
    bindings = Bindings(module, config, settings, evaluate_fn, codex_fn, sdk_types, output_dir, store)
    module.BASELINES = [("codex-baseline", "agents.baseline_codex:AgentHarness")]
    module.BASELINE_AGENT_NAME = "codex-baseline"
    module.MODEL = settings.model
    module.N_EVAL_TASKS = sum(len(item.get("task_ids") or []) for item in _selection_list(config["evolve"]))
    module.harbor_run = bindings.harbor_run
    module.smoke_test = bindings.smoke_test
    module.validate_candidate = bindings.validate_candidate
    module.propose_claude = bindings.propose
    module.render_task_prompt = bindings.render_task_prompt
    module.parse_job_results = bindings.parse_job_results
    module.update_frontier = bindings.update_frontier
    module.update_evolution_summary = bindings.update_history
    module.count_iterations = bindings.count_iterations
    history = module.EVOLVE_DIR / "logs/search/evolution_summary.jsonl"
    previous_rows = [json.loads(line) for line in history.read_text().splitlines() if line.strip()] if history.exists() else []
    completed_rounds = bindings.count_iterations()
    if completed_rounds > iterations:
        raise IntegrationError("Recorded round count exceeds experiment configuration")
    arguments = SimpleNamespace(run_name="search", fresh=False, iterations=iterations - completed_rounds,
                                trials=1, concurrent=1, skip_baseline=False,
                                skip_smoke=config.get("skip_smoke", False), full_eval=False,
                                propose_timeout=settings.timeout_seconds)
    summary = {"status": "failed", "upstream_commit": UPSTREAM_COMMIT,
               "output_dir": str(output_dir), "method": "meta-harness-codex",
               "method_policy": policy_identity(config["method_policy"]),
               "variant": bool(config["method_policy"]), "checkpoint": str(store.path),
               "proposer_attempts": store.data["proposer_attempts"]}
    try:
        module.run_evolve(arguments)
        rows = [json.loads(line) for line in module.EVOLUTION_SUMMARY.read_text().splitlines() if line.strip()] if module.EVOLUTION_SUMMARY.exists() else []
        if set(bindings.settled_rounds) != {str(i) for i in range(1, iterations + 1)}:
            raise IntegrationError("Native search returned before every requested round settled")
        frontier = json.loads(module.FRONTIER_VAL.read_text())
        winner = frontier["_best"]["agent"]
        reference = bindings.candidates[winner]
        if winner not in bindings.measured:
            raise IntegrationError("Native frontier selected an unmeasured candidate")
        test_dir = bindings.evaluate_many(reference, "final-test", _selection_list(config["test"]), 1, 1, "test")
        summary.update(status="completed", rounds=iterations, winner=winner,
                       winner_artifact=str(bindings.artifact(reference)),
                       frontier=frontier, test_artifact_dir=str(test_dir),
                       history=str(module.EVOLUTION_SUMMARY), evaluations=str(output_dir / "evaluations.json"))
        summary["round_outcomes"] = list(bindings.settled_rounds.values())
        summary["acceptance"] = acceptance_report(bindings.state(), iterations)
        summary["acceptance_passed"] = summary["acceptance"]["passed"]
    except BaseException as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        active = store.data.get("active_proposer")
        if active:
            active.update(status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                          error=summary["error"])
            _write_json(store.path, store.data)
            summary["proposer_attempts"] = [*store.data["proposer_attempts"],
                                            {key: value for key, value in active.items() if key != "before"}]
        _write_json(output_dir / "result.json", summary)
        raise
    _write_json(output_dir / "result.json", summary)
    store.data["summary"] = summary
    bindings.checkpoint()
    return summary


def acceptance_report(bindings: dict, iterations: int) -> dict:
    """Stronger functional evidence; ordinary native rejections remain valid outcomes."""
    rounds = bindings.get("settled_rounds", {})
    missing = [i for i in range(1, iterations + 1)
               if not rounds.get(str(i), {}).get("evaluated")]
    return {"passed": not missing, "rounds_without_changed_measured_candidate": missing,
            "note": "Each requested round must evaluate a changed candidate; promotion and score improvement are not required."}


def audit_experiment(output_dir: Path) -> dict:
    data = json.loads((output_dir / "resume.json").read_text())
    report = acceptance_report(data["bindings"], data["identity"]["config"].get("iterations", 2))
    if not report["passed"]:
        raise IntegrationError(f"Integration acceptance failed: {report}")
    return report


def run_experiment(config: dict[str, Any], output_dir: Path, *, source: Path | None = None,
                   template: Path | None = None, settings=None, evaluate_fn=None,
                   codex_fn=None, sdk_types=None, resume: bool = False) -> dict[str, Any]:
    """Run or resume one experiment, with exclusive access to its local state."""
    prepared = normalize_config(config, Path.cwd())
    validate_evolve_scoring(prepared["evolve"], prepared.get("method_policy"))
    output_dir = output_dir.resolve()
    if resume and not (output_dir / "resume.json").exists():
        raise IntegrationError("This run has no durable checkpoint; use a new output directory")
    if not resume and output_dir.exists() and any(path.name != ".meta-run.lock" for path in output_dir.iterdir()):
        raise IntegrationError("Use a fresh output directory, or --resume to continue a recorded experiment")
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".meta-run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntegrationError("Another process is running or resuming this Meta-Harness experiment") from exc
        try:
            return _run_experiment(prepared, output_dir, source=source, template=template,
                                   settings=settings, evaluate_fn=evaluate_fn, codex_fn=codex_fn,
                                   sdk_types=sdk_types, resume=resume)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--upstream-source", type=Path, default=PROJECT_ROOT / "meta-harness")
    parser.add_argument("--template", type=Path, help="Initial Codex wrapper artifact directory")
    parser.add_argument("--resume", action="store_true", help="Continue an unchanged experiment from its durable checkpoint")
    parser.add_argument("--audit", action="store_true", help="Check strict integration acceptance evidence without running models")
    args = parser.parse_args(argv)
    config = normalize_config(json.loads(args.config.read_text()), args.config.resolve().parent)
    try:
        result = audit_experiment(args.output_dir) if args.audit else run_experiment(config, args.output_dir, source=args.upstream_source,
                                template=args.template, resume=args.resume)
    except Exception as exc:
        print(f"Meta-Harness did not complete: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
