"""Public MetaAgent/TB2 promotion loop using the common evaluation SDK."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate
from rsi_eval.artifacts import artifact_hash
from rsi_eval.codex import CodexAuthError, CodexQuotaError, CodexSettings
from rsi_eval.method_policy import normalize_policy, policy_identity, score_trial, select_candidate
from integrations.runtime_options import normalize_run_options, normalize_selection, effective_options

from . import AGENT_ENTRYPOINT, UPSTREAM_COMMIT
from .bundle import freeze_config, load_config
from .prepare import check_checkout
from .provider import CodexProvider, accounting


def load_experiment(path: Path) -> dict:
    path = Path(path).resolve()
    data = json.loads(path.read_text())
    def absolute(value):
        value = Path(value).expanduser()
        return str((path.parent / value).resolve() if not value.is_absolute() else value.resolve())
    for key in ("checkout", "output_dir"):
        data[key] = absolute(data[key])
    if data.get("seed"):
        data["seed"] = absolute(data["seed"])
    data["codex"]["auth_dir"] = absolute(data["codex"]["auth_dir"])
    if "/" in data["codex"].get("executable", "codex"):
        data["codex"]["executable"] = absolute(data["codex"]["executable"])
    if not data["codex"].get("model") or not data["codex"].get("expected_version"):
        raise ValueError("codex.model and codex.expected_version are required")
    data["evolve"] = normalize_selection(data["evolve"], path.parent)
    data["test"] = [normalize_selection(x, path.parent) for x in data.get("test", [])]
    data["run_options"] = normalize_run_options(data.get("run_options", {}), path.parent)
    if data["run_options"].get("concurrency", 1) != 1:
        raise ValueError("Codex subscription integration requires concurrency=1")
    if type(data.get("rounds", 2)) is not int or data.get("rounds", 2) < 1:
        raise ValueError("rounds must be a positive integer")
    data.setdefault("rounds", 2)
    data.setdefault("method", {})
    if data.get("method_policy"):
        data["method_policy"] = normalize_policy(data["method_policy"], path.parent,
            candidate_roots=(Path(data["output_dir"]) / "candidates",))
    return data


def runtime_environment(config: dict) -> None:
    checkout = Path(config["checkout"])
    check_checkout(checkout)
    root = Path(__file__).resolve().parents[2]
    paths = [str(root), str(root / "src"), str(checkout)]
    sys.path[:0] = paths
    # Inherit this explicit configuration in Harbor and native self-check children.
    os.environ["PYTHONPATH"] = os.pathsep.join(paths + [os.environ.get("PYTHONPATH", "")])
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")


def target_for(config, candidate):
    codex = config["codex"]
    return TargetSpec(artifact_dir=Path(candidate), entrypoint=AGENT_ENTRYPOINT,
        model=codex["model"], config={
            "artifact_dir": str(candidate), "harnessx_checkout": config["checkout"],
            "codex_auth_dir": codex["auth_dir"], "codex_version": codex["expected_version"],
            "codex_executable": codex.get("executable", "codex"),
            "reasoning_effort": codex.get("reasoning_effort", "medium"),
            "model_timeout": codex.get("timeout_seconds", 600),
            "expected_candidate_hash": artifact_hash(Path(candidate)),
            "max_steps": config.get("target", {}).get("max_steps", 100),
        })


def selection_value(selection):
    value = {k: v for k, v in selection.items() if k not in {"benchmark_runtime", "runtime_snapshot"}}
    if value.get("task_ids") is not None:
        value["task_ids"] = tuple(value["task_ids"])
    if value.get("manifest"):
        value["manifest"] = Path(value["manifest"])
    return BenchmarkSelection(**value)


def preflight(config):
    from rsi_eval import check
    from rsi_eval.codex import validate_auth, codex_version
    from harnessx import HarnessConfig
    from benchmarks.terminal_bench_2.harness import make_tb2_harness_config
    checks = {"source": {"ok": True, **check_checkout(Path(config["checkout"]))}}
    try:
        settings = CodexSettings(**config["codex"])
        validate_auth(settings.auth_dir)
        checks["codex"] = {"ok": True, "version": codex_version(settings), "model": settings.model}
    except Exception as exc:
        checks["codex"] = {"ok": False, "detail": str(exc)}
    with tempfile.TemporaryDirectory(prefix="harnessx-check-") as temporary:
        root = Path(temporary)
        seed = HarnessConfig.from_yaml_file(config["seed"]) if config.get("seed") else make_tb2_harness_config()
        seed.to_yaml_file(root / "seed.yaml")
        candidate = root / "candidate"
        freeze_config(Path(config["seed"]) if config.get("seed") else root / "seed.yaml", candidate)
        for index, selection in enumerate([config["evolve"], *config.get("test", [])]):
            try:
                checks[f"selection.{index}"] = check(target=target_for(config, candidate),
                    benchmark=selection_value(selection),
                    options=RunOptions(**effective_options(config["run_options"], selection)))
            except Exception as exc:
                checks[f"selection.{index}"] = {"ok": False, "detail": str(exc)}
    return {"ok": all(item["ok"] for item in checks.values()), "model_calls": 0, "checks": checks}


def evaluate_candidate(config, candidate, selection, output_dir):
    result = evaluate(target=target_for(config, candidate), benchmark=selection_value(selection),
        options=RunOptions(**effective_options(config["run_options"], selection)), output_dir=output_dir)
    statuses = {result.status, *(trial.status for trial in result.trials)}
    if statuses & {"auth_error", "authentication_error"}:
        raise CodexAuthError(f"Target authentication failed; inspect {result.report_path}")
    if statuses & {"quota_exceeded", "quota_error", "quota_exhausted"}:
        raise CodexQuotaError(f"Target subscription quota prevented completion; inspect {result.report_path}")
    if result.status != "completed" or not result.trials or any(t.reward is None for t in result.trials):
        raise RuntimeError(f"Benchmark evaluation incomplete: {result.status}; {result.report_path}")
    return result


def feedback(result, destination):
    destination.mkdir(parents=True)
    (destination / "evaluation.json").write_text(json.dumps(result.to_dict(), indent=2) + "\n")
    for trial in result.trials:
        trial_dir = destination / trial.task_id / str(trial.repetition)
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "result.json").write_text(json.dumps(asdict(trial), indent=2) + "\n")
        # Copy only agent evidence. Verifier files stay in the raw evaluation.
        agent_dir = Path(trial.artifact_dir) / "agent"
        if agent_dir.is_dir():
            shutil.copytree(agent_dir, trial_dir / "agent")
            markdown = agent_dir / "trajectory.md"
            if markdown.is_file():
                (trial_dir / "trajectory.md").write_text(
                    f"---\ntask_id: {trial.task_id}\nreward: {trial.reward}\nstatus: {trial.status}\n---\n\n"
                    + markdown.read_text())
    return destination


def write_result(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def choose_evaluated(config, incumbent, candidate):
    """Run an explicit policy only after the native candidate gates succeeded."""
    policy = config.get("method_policy", {})
    rows = []
    for candidate_id, value in (("incumbent", incumbent), ("candidate", candidate)):
        measured = json.loads(Path(value["report_path"]).read_text())
        rows.append({"id": candidate_id, **value, "native_gates_passed": True,
                     "evaluation": measured,
                     "mapped_scores": [score_trial(policy, trial, config["evolve"])
                                       for trial in measured["trials"]]})
    selected = select_candidate(policy, rows, "candidate", context={
        "method": "harnessx", "benchmark": config["evolve"],
        "native_policy": "promote_after_native_gates"})
    return selected == "candidate", {"policy": policy_identity(policy),
        "selected": selected or "incumbent", "eligible": [
            {"id": row["id"], "candidate_hash": row["candidate_hash"],
             "mapped_scores": row["mapped_scores"], "report_path": row["report_path"]}
            for row in rows]}


def history_reads(output: Path, memo: Path) -> list[dict]:
    """Audit v2: approved Read, or Bash file-read plus matching returned text.

    This is execution evidence, not a claim about the model's internal reasoning.
    Bash reads require both a referenced history file and distinctive contents;
    printing a path alone cannot satisfy the audit.
    """
    paths = {str((output / "_meta_scratch" / "CONTEXT.md").resolve()), str(memo.resolve())}
    signatures = {}
    for name in paths:
        try:
            lines = [line.strip() for line in Path(name).read_text().splitlines() if line.strip()]
        except OSError:
            continue
        headings = [line for line in lines if line.startswith(("# Journal Context for R", "## Round "))]
        details = [line for line in lines if not line.startswith(("#", "<!--", "-->"))
                   and len(line) >= 24 and sum(c.isalnum() for c in line) >= 10]
        signatures[name] = (headings, details)
    requests, approvals, results = {}, {}, {}
    for path in sorted((output / "meta_workspace" / "sessions").rglob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") in {"raw_assistant", "assistant"}:
                for call in row.get("message", {}).get("tool_calls", []):
                    name = call.get("name")
                    requested = call.get("input", {}).get("file_path")
                    if name == "Read" and requested and str(Path(requested).resolve()) in paths:
                        requests[call["id"]] = {"path": requested, "tool_call_id": call["id"], "trace": str(path),
                                               "evidence": "approved_read", "audit_version": 2}
                    elif name == "Bash":
                        command = call.get("input", {}).get("command", "")
                        reader = re.search(r"(?:^|[\s;|&(])(?:cat|sed|head|tail|grep|rg|awk|perl|python(?:3(?:\.\d+)?)?|node|ruby|dd|less|more)(?:\s|$)", command)
                        referenced = [p for p in signatures if p in command or Path(p).name in command]
                        if reader and referenced:
                            requests[call["id"]] = {"paths": referenced, "tool_call_id": call["id"],
                                "trace": str(path), "evidence": "approved_bash_content_match", "audit_version": 2}
            elif row.get("event_type") == "tool_call":
                approvals[row["tool_call_id"]] = row.get("approved")
            elif row.get("type") in {"raw_tool", "tool"}:
                message = row.get("message", {})
                content = message.get("content")
                if not content and row.get("meta", {}).get("content_ref"):
                    reference = Path(row["meta"]["content_ref"])
                    reference = reference if reference.is_absolute() else path.parent / reference
                    try:
                        content = reference.read_text()
                    except OSError:
                        pass
                if isinstance(content, str) and content.strip() and not content.lstrip().startswith("Error:"):
                    results[message.get("tool_call_id")] = content
    evidence = []
    for key, row in requests.items():
        if approvals.get(key) is not True or key not in results:
            continue
        if row["evidence"] == "approved_read":
            evidence.append(row)
            continue
        content = results[key]
        for name in row["paths"]:
            headings, details = signatures[name]
            if any(line in content for line in headings) and any(line in content for line in details):
                evidence.append({k: v for k, v in {**row, "path": name}.items() if k != "paths"})
    return evidence


async def run(config, *, resume=False):
    runtime_environment(config)
    from harnessx import HarnessConfig, ModelConfig
    from harnessx.meta_harness.agent import MetaAgent
    from harnessx.meta_harness.journal import fill_gating, latest_entry
    from benchmarks.terminal_bench_2.harness import make_tb2_harness_config

    root = Path(config["output_dir"])
    auth = Path(config["codex"]["auth_dir"])
    if root == auth or root in auth.parents:
        raise ValueError("Codex auth must be outside experiment output")
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["HARNESSX_HOME"] = str(root / "harnessx-home")
    state_path = root / "result.json"
    if state_path.exists():
        if not resume:
            raise FileExistsError("Experiment exists; use --resume or another output directory")
        state = json.loads(state_path.read_text())
        if state["config_sha256"] != fingerprint:
            raise ValueError("Cannot resume with a changed experiment configuration")
    else:
        state = {"method": "HarnessX public TB2 evolution; Codex adaptation",
                 "upstream_commit": UPSTREAM_COMMIT, "config_sha256": fingerprint,
                 "status": "running", "model": config["codex"]["model"],
                 "cli_version": config["codex"]["expected_version"],
                 "selection_policy": "promote after native validity/novelty/evidence/smoke gates",
                 "method_policy": policy_identity(config.get("method_policy")),
                 "rounds": [], "test": [], "cost_usd": None}
        write_result(state_path, state)
    if state.get("status") == "completed":
        return state
    state["status"] = "running"
    state.pop("error", None)
    write_result(state_path, state)
    settings = CodexSettings(**config["codex"])
    provider = CodexProvider(settings, root / "method-calls")
    model = ModelConfig(main=provider)
    method = config.get("method", {})
    meta = MetaAgent(inner_model=model, memo_path=root / "learnings.md", max_cost_usd=None,
        max_steps=method.get("max_steps", 40), wall_clock_s=method.get("timeout_seconds", 1800),
        extra_harness_kws={"loop_detection": False},
        step_deadline_reminder_step=method.get("reminder_step", 30))
    try:
        if not state.get("baseline"):
            seed_yaml = root / "seed.yaml"
            if config.get("seed"):
                seed = HarnessConfig.from_yaml_file(config["seed"])
            else:
                seed = make_tb2_harness_config(timeout_seconds=config.get("target", {}).get("task_timeout", 600))
            seed.to_yaml_file(seed_yaml)
            bundle = root / "candidates" / ("baseline-" + uuid.uuid4().hex[:8])
            freeze_config(Path(config["seed"]) if config.get("seed") else seed_yaml, bundle)
            baseline = await asyncio.to_thread(evaluate_candidate, config, bundle, config["evolve"],
                                               root / "evaluations" / bundle.name)
            fdir = feedback(baseline, root / "feedback" / bundle.name)
            state["baseline"] = {"candidate": str(bundle), "candidate_hash": artifact_hash(bundle),
                                 "report_path": baseline.report_path, "feedback": str(fdir)}
            state["current_candidate"] = str(bundle)
            state["current_feedback"] = str(fdir)
            state["current_report"] = baseline.report_path
            write_result(state_path, state)

        for index in range(len(state["rounds"]) + 1, config["rounds"] + 1):
            current = Path(state["current_candidate"])
            attempt = f"R{index}-{uuid.uuid4().hex[:8]}"
            out = root / "evolution" / attempt
            # The native validator loads file paths itself, so materialize a
            # current-config view of the portable immutable candidate bundle.
            current_yaml = root / f"current-{attempt}.yaml"
            load_config(current, checkout=Path(config["checkout"])).to_yaml_file(current_yaml)
            output = await meta.evolve(current_config=current_yaml,
                trajectories_dir=Path(state["current_feedback"]), output_dir=out,
                replay_model=model, replay_max_cost_usd=None,
                replay_timeout_s=method.get("smoke_timeout_seconds", 300))
            if provider.failure:
                raise provider.failure
            bundle = root / "candidates" / attempt
            freeze_config(output, bundle, allowed_roots=[current])
            candidate_result = await asyncio.to_thread(evaluate_candidate, config, bundle, config["evolve"],
                                                        root / "evaluations" / attempt)
            fdir = feedback(candidate_result, root / "feedback" / attempt)
            prior_report = state.get("current_report") or next(
                (r["report_path"] for r in reversed(state["rounds"])
                 if r["candidate"] == str(current)), state["baseline"]["report_path"])
            promoted, policy_record = choose_evaluated(config,
                {"candidate": str(current), "candidate_hash": artifact_hash(current), "report_path": prior_report},
                {"candidate": str(bundle), "candidate_hash": artifact_hash(bundle), "report_path": candidate_result.report_path})
            entry = latest_entry(root / "learnings.md")
            if entry is not None:
                fill_gating(root / "learnings.md", entry.round, "accepted" if promoted else "rejected", {}, extra_frontmatter={
                    "evaluation_report": candidate_result.report_path,
                    "native_gates_passed": True, "explicit_method_policy": policy_record,
                    "per_task_rewards": {t.task_id: t.reward for t in candidate_result.trials}})
            state["rounds"].append({"round": index, "candidate": str(bundle),
                "candidate_hash": artifact_hash(bundle), "previous_candidate_hash": artifact_hash(current),
                "report_path": candidate_result.report_path, "native_output": str(out),
                "feedback": str(fdir), "decision": ("promoted_by_explicit_policy" if config.get("method_policy", {}).get("selection") else "promoted_by_native_gates") if promoted else "retained_by_explicit_policy",
                "method_policy": policy_record,
                "history_context": str(out / "_meta_scratch" / "CONTEXT.md"),
                "structural_changes": json.loads((out / "_meta_scratch" / "changeset.json").read_text()),
                "history_context_exists": (out / "_meta_scratch" / "CONTEXT.md").is_file()})
            state["rounds"][-1]["history_reads"] = history_reads(out, root / "learnings.md")
            if promoted:
                state["current_candidate"], state["current_feedback"] = str(bundle), str(fdir)
                state["current_report"] = candidate_result.report_path
            state["method_usage"] = provider.accounting()
            write_result(state_path, state)

        for index, selection in enumerate(config.get("test", [])):
            if index < len(state["test"]):
                continue
            result = await asyncio.to_thread(evaluate_candidate, config, Path(state["current_candidate"]),
                selection, root / "test" / f"{index}-{uuid.uuid4().hex[:8]}")
            state["test"].append({"selection": selection, "report_path": result.report_path})
            write_result(state_path, state)
        state["status"] = "completed"
        state["functional_evidence"] = {
            "evaluated_rounds": len(state["rounds"]),
            "structurally_changed_rounds": sum(bool(r["structural_changes"]) for r in state["rounds"]),
            "later_rounds_with_journal_context": sum(r["history_context_exists"] for r in state["rounds"][1:]),
            "later_rounds_with_verified_history_reads": sum(bool(r.get("history_reads")) for r in state["rounds"][1:]),
        }
    except BaseException as exc:
        terminal = provider.failure if isinstance(provider.failure, (CodexAuthError, CodexQuotaError)) else exc
        state["status"] = ("auth_error" if isinstance(terminal, CodexAuthError) else
                           "quota_exceeded" if isinstance(terminal, CodexQuotaError) else
                           "interrupted" if isinstance(terminal, (KeyboardInterrupt, asyncio.CancelledError)) else "failed")
        state["error"] = {"type": type(terminal).__name__, "message": str(terminal)}
        if terminal is not exc:
            raise terminal from exc
        raise
    finally:
        state["method_usage"] = provider.accounting()
        # All attempts, including resumed and failed calls, retain their own raw
        # result.json; this index avoids losing usage across process restarts.
        state["method_call_records"] = [str(p) for p in sorted((root / "method-calls").glob("*/result.json"))]
        if state["method_call_records"]:
            state["method_usage"] = accounting([json.loads(Path(p).read_text()) for p in state["method_call_records"]])
        write_result(state_path, state)
    return state
