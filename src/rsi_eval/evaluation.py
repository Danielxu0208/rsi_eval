"""Resolve a run, invoke the pinned Harbor CLI, and preserve its evidence."""
from __future__ import annotations

import json
import math
import os
import platform
import re
import signal
import subprocess
import time
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .artifacts import artifact_hash, snapshot
from .benchmarks import resolve
from .types import BenchmarkSelection, EvaluationResult, RunOptions, TargetSpec, TrialResult

HARBOR_VERSION = "0.18.0"
CODEX_AGENT = "rsi_eval.harbor_codex:Agent"
DOCKER_ENVIRONMENT = "rsi_eval.harbor_docker:Environment"


def _json_write(path: Path, value: Any) -> None:
    payload = json.dumps(value, indent=2, default=str, allow_nan=False) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(payload)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _read_native_json(path: Path) -> Any:
    def invalid(token: str) -> None:
        raise ValueError(f"nonfinite number in native result: {token}")

    def finite_float(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            invalid(token)
        return value

    from .relocation import relocate
    return relocate(json.loads(path.read_text(), parse_constant=invalid, parse_float=finite_float), path)


def _validate(target: TargetSpec, options: RunOptions) -> None:
    if not target.entrypoint:
        raise ValueError("target entrypoint is required")
    if any(type(value) is not int or value < 1 for value in (options.repetitions, options.concurrency)):
        raise ValueError("repetitions and concurrency must be positive integers")
    if options.timeout is not None and options.timeout <= 0:
        raise ValueError("timeout must be positive")
    if options.runtime_snapshot is not None and (not isinstance(options.runtime_snapshot, str)
                                                or options.environment != "docker"):
        raise ValueError("runtime_snapshot requires a path string and the Docker environment")
    if type(options.retain_runtime_images) is not bool or (options.retain_runtime_images and options.environment != "docker"):
        raise ValueError("retain_runtime_images requires a boolean and the Docker environment")
    if not isinstance(options.benchmark_runtime, dict):
        raise ValueError("benchmark_runtime must be an object")
    try:
        json.dumps(options.benchmark_runtime, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("benchmark_runtime must contain finite JSON values") from error
    if target.entrypoint == CODEX_AGENT:
        if not target.model:
            raise ValueError("Codex target requires an explicit model")
        if options.concurrency != 1:
            raise ValueError("subscription Codex v0.1 requires concurrency=1")
        for key in ("codex_auth_dir", "codex_version"):
            if not target.config.get(key):
                raise ValueError(f"Codex target requires config.{key}")


def _validate_oracle(target: TargetSpec, spec: dict[str, Any]) -> None:
    if target.entrypoint != "oracle":
        return
    oracle = spec.get("oracle", {})
    missing = set(spec["task_ids"]) & set(oracle.get("unsupported_task_ids", []))
    if "supported_task_ids" in oracle:
        missing |= set(spec["task_ids"]) - set(oracle["supported_task_ids"])
    if missing:
        raise ValueError(f"no declared oracle/reference solution for tasks: {sorted(missing)}")


def preview(*, target: TargetSpec, benchmark: BenchmarkSelection,
            options: RunOptions | None = None, output_dir: Path | str = "runs/preview") -> dict[str, Any]:
    """Resolve exact tasks and candidate identity without model or network calls."""
    options = options or RunOptions()
    _validate(target, options)
    spec = resolve(benchmark)
    if options.benchmark_runtime and not spec.get("materializer"):
        raise ValueError("benchmark_runtime applies to source-backed benchmark adapters")
    _validate_oracle(target, spec)
    frozen = None
    if options.runtime_snapshot:
        from .runtime_snapshot import load_snapshot, runtime_settings_match
        _, frozen = load_snapshot(options.runtime_snapshot, spec=spec)
        if not runtime_settings_match(frozen["benchmark_runtime"], options.benchmark_runtime):
            raise ValueError("benchmark_runtime differs from the frozen runtime configuration")
    source = Path(target.artifact_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if output == source or source in output.parents:
        raise ValueError("output_dir must be outside the candidate artifact directory")
    return {"schema_version": 1, "benchmark": spec,
            "runtime_snapshot_sha256": frozen["sha256"] if frozen else None,
            "target": {"artifact_dir": str(source), "entrypoint": target.entrypoint,
                       "model": target.model, "config": target.config,
                       "sha256": artifact_hash(source)},
            "options": asdict(options), "output_dir": str(output),
            "backend": {"name": "harbor", "version": HARBOR_VERSION,
                        "docker_context_guard": "rsi-context-v2" if options.environment == "docker" else None},
            "runtime": {"host_platform": platform.system(), "host_machine": platform.machine(),
                        "python_version": platform.python_version(),
                        "docker_default_platform": os.environ.get("DOCKER_DEFAULT_PLATFORM"),
                        "docker_context": os.environ.get("DOCKER_CONTEXT")},
            "expected_trials": len(spec["tasks"]) * options.repetitions}


def _probe(command: list[str], timeout: float = 20) -> dict[str, Any]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return {"ok": result.returncode == 0,
                "detail": (result.stdout + result.stderr).strip()[-2000:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "detail": str(error)}


def check(*, target: TargetSpec, benchmark: BenchmarkSelection,
          options: RunOptions | None = None, codex_executable: str | None = None) -> dict[str, Any]:
    """Check tools and auth shape without model calls or containers.

    codex_executable selects only the host-side version probe. It is never
    included in TargetSpec, snapshots or the container agent's command.
    """
    options = options or RunOptions()
    _validate(target, options)
    spec = resolve(benchmark)
    _validate_oracle(target, spec)
    checks: dict[str, Any] = {"candidate": {"ok": True, "sha256": artifact_hash(target.artifact_dir)}}
    if options.runtime_snapshot:
        from .runtime_snapshot import load_snapshot, preflight as frozen_preflight, runtime_settings_match
        try:
            _, frozen = load_snapshot(options.runtime_snapshot, spec=spec, check_images=True)
            checks["runtime_snapshot"] = {"ok": runtime_settings_match(frozen["benchmark_runtime"], options.benchmark_runtime),
                                           "sha256": frozen["sha256"]}
            checks.update({f"benchmark.{name}": row for name, row in frozen_preflight(spec, options.benchmark_runtime).items()})
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            checks["runtime_snapshot"] = {"ok": False, "detail": str(error)}
    elif spec.get("materializer"):
        from .materializers import preflight
        try:
            checks.update({f"benchmark.{name}": row for name, row in preflight(
                spec, options.benchmark_runtime).items()})
        except Exception as error:
            checks["benchmark"] = {"ok": False, "detail": str(error)}
    elif options.benchmark_runtime:
        checks["benchmark"] = {"ok": False, "detail": "benchmark_runtime applies to source-backed benchmark adapters"}
    harbor = _probe([options.harbor_executable, "--version"])
    harbor["ok"] = harbor["ok"] and bool(re.search(r"(?<![\d.])0\.18\.0(?![\d.])", harbor["detail"]))
    checks["harbor"] = harbor
    if options.environment == "docker":
        checks["docker"] = _probe(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"])
        checks["compose"] = _probe(["docker", "compose", "version", "--short"])
    if target.entrypoint == CODEX_AGENT:
        from .codex import CodexSettings, codex_version, validate_auth
        settings = CodexSettings(model=target.model, auth_dir=Path(target.config["codex_auth_dir"]),
                                 expected_version=target.config["codex_version"],
                                 executable=codex_executable or "codex")
        try:
            validate_auth(settings.auth_dir)
            checks["codex"] = {"ok": True, "version": codex_version(settings)}
        except Exception as error:
            checks["codex"] = {"ok": False, "detail": str(error)}
    return {"ok": all(row["ok"] for row in checks.values()), "checks": checks}


def _job_config(resolved: dict[str, Any], snapshot_dir: Path) -> dict[str, Any]:
    target, options = resolved["target"], resolved["options"]
    entry = target["entrypoint"]
    kwargs = dict(target["config"])
    # Custom agents opt into the same immutable artifact contract by accepting
    # an artifact_dir keyword. Never leave it pointing at the mutable source
    # workspace while reporting the hash of a different SDK snapshot.
    if entry == CODEX_AGENT or "artifact_dir" in kwargs:
        kwargs["artifact_dir"] = str(snapshot_dir)
    agent = {"import_path" if ":" in entry else "name": entry,
             "model_name": target["model"], "kwargs": kwargs}
    # Harbor 0.18 initializes explicit-task metrics under "adhoc". Supplying a
    # dataset source without a DatasetConfig creates an empty metric group and
    # crashes after verification. Keep provenance in resolved.json; the native
    # explicit-task Mean aggregation and each task's verifier remain unchanged.
    tasks = [{key: value for key, value in task.items() if key not in {"id", "source", "sha256", "native_name"}}
             for task in resolved.get("execution_tasks", resolved["benchmark"]["tasks"])]
    environment = {"type": options["environment"]}
    if options["environment"] == "docker":
        environment["import_path"] = DOCKER_ENVIRONMENT
        if options.get("retain_runtime_images"):
            environment.setdefault("kwargs", {})["retain_runtime_images"] = True
        if options.get("runtime_snapshot"):
            environment["import_path"] = "rsi_eval.runtime_docker:FrozenDockerEnvironment"
            environment.setdefault("kwargs", {})["runtime_snapshot"] = options["runtime_snapshot"]
    return {"job_name": "eval", "jobs_dir": str(Path(resolved["output_dir"]) / "native"),
            "n_attempts": options["repetitions"], "n_concurrent_trials": options["concurrency"],
            "retry": {"max_retries": 0}, "environment": environment,
            "agents": [agent], "tasks": tasks}


def _status(error: Any, reward: float | None, finished: Any) -> tuple[str, str | None]:
    if error:
        message = (error.get("exception_type", "") + ": " + error.get("exception_message", "")) if isinstance(error, dict) else str(error)
        lower = message.lower()
        kind = str(error.get("exception_type", "")).lower() if isinstance(error, dict) else ""
        # Native typed exceptions outrank prose: Harbor's generic agent error
        # includes the whole command/task prompt, which may discuss HTTP 401 or
        # quotas without representing a model-service failure.
        if kind in {"codexquotaerror", "apiratelimiterror", "apiusagelimiterror"}:
            return "quota_exceeded", message
        if "auth" in kind:
            return "auth_error", message
        if kind in {"agenttimeouterror", "verifiertimeouterror"}:
            return "task_timeout", message
        if kind == "nonzeroagentexitcodeerror":
            return "agent_error", message
        if any(s in lower for s in ("quota", "usage limit", "rate limit", "ratelimit", "429")):
            return "quota_exceeded", message
        # Harbor includes the executed command in errors. Normal flags such as
        # cli_auth_credentials_store and forced_login_method are not failures.
        if any(s in lower for s in (
            "not logged in", "please log in", "login expired", "authentication failed",
            "invalid authentication", "unauthorized", "401", "refresh token",
        )):
            return "auth_error", message
        if any(s in lower for s in ("agenttimeout", "verifiertimeout")):
            return "task_timeout", message
        if "nonzeroagentexitcodeerror" in lower:
            return "agent_error", message
        if any(s in lower for s in ("docker", "environment", "image", "connection", "setup", "oserror")):
            return "infra_error", message
        return "incomplete", message
    if reward is None or not finished:
        return "incomplete", "missing reward or trial completion marker"
    return "completed", None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return float(value)
        except OverflowError:
            pass
    return None


def _tokens(contexts: list[dict[str, Any]], key: str) -> int | None:
    values = [c.get(key) for c in contexts]
    if not values or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
        return None
    return sum(values)


def _native_task_index(resolved: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Index pinned identities once, including large complete record collections."""
    index = {}
    for task in resolved.get("execution_tasks", resolved["benchmark"]["tasks"]):
        if task.get("name"):
            org, name = task["name"].split("/", 1)
            pinned = {"org": org, "name": name, "ref": task["ref"]}
            native_name = task["name"]
        elif "git_url" in task:
            pinned = {key: task[key] for key in ("git_url", "git_commit_id", "path")}
            native_name = Path(task["path"]).name
        else:
            pinned = {"path": task["path"]}
            native_name = task.get("native_name", Path(task["path"]).name)
        key = (native_name, json.dumps(pinned, sort_keys=True, allow_nan=False))
        if key in index:
            raise ValueError("selected task IDs have ambiguous native identities")
        index[key] = task["id"]
    return index


def _native_task(data: dict[str, Any], resolved: dict[str, Any],
                 index: dict[tuple[str, str], str] | None = None) -> str:
    """Match immutable task identity, not only Harbor's abbreviated directory name."""
    identity = data.get("task_id")
    if not isinstance(identity, dict):
        raise ValueError("missing native task_id")
    key = (data.get("task_name"), json.dumps(identity, sort_keys=True, allow_nan=False))
    lookup = _native_task_index(resolved) if index is None else index
    try:
        return lookup[key]
    except KeyError:
        raise ValueError("native task identity does not match a selected immutable task") from None


def _validate_native_agent(data: dict[str, Any], resolved: dict[str, Any]) -> None:
    config = data.get("config")
    agent = config.get("agent") if isinstance(config, dict) else None
    if not isinstance(agent, dict):
        raise ValueError("missing native trial config.agent")
    target = resolved["target"]
    key = "import_path" if ":" in target["entrypoint"] else "name"
    if agent.get(key) != target["entrypoint"]:
        raise ValueError("native agent entrypoint does not match the submitted target")
    if agent.get("model_name") != target["model"]:
        raise ValueError("native model does not match the submitted target")
    if target["entrypoint"] == CODEX_AGENT or "artifact_dir" in target.get("config", {}):
        expected = str(Path(resolved["output_dir"]) / "candidate")
        kwargs = agent.get("kwargs")
        if not isinstance(kwargs, dict) or kwargs.get("artifact_dir") != expected:
            raise ValueError("native agent artifact_dir does not match the candidate snapshot")


def collect_result(resolved: dict[str, Any], *, returncode: int | None,
                   error: str | None = None, elapsed: float | None = None,
                   write_report: bool = True) -> EvaluationResult:
    output = Path(resolved["output_dir"])
    native = output / "native" / "eval"
    expected = resolved["benchmark"]["task_ids"]
    repetitions = resolved["options"]["repetitions"]
    by_task: dict[str, list[TrialResult]] = {t: [] for t in expected}
    parse_errors = []
    paths = sorted(native.glob("*/result.json"))
    try:
        task_index = _native_task_index(resolved) if paths else {}
    except (ValueError, KeyError, TypeError) as exc:
        parse_errors.append(f"native task catalog: {exc}")
        paths = []
    for path in paths:
        try:
            data = _read_native_json(path)
            if not isinstance(data, dict):
                raise ValueError("native trial result must be an object")
            task = _native_task(data, resolved, task_index)
            _validate_native_agent(data, resolved)
            metrics = (data.get("verifier_result") or {}).get("rewards") or {}
            if not isinstance(metrics, dict):
                raise ValueError("native rewards must be an object")
            reward = _number(metrics.get("reward"))
            status, message = _status(data.get("exception_info"), reward, data.get("finished_at"))
            if reward is None and resolved["benchmark"].get("materializer"):
                # API judges can fail before Harbor receives a reward. Only
                # benchmark-owned post-agent reports may refine that failure;
                # arbitrary candidate files and agent logs are never scanned.
                from .materializers import verifier_failure
                diagnosis = verifier_failure(resolved["benchmark"], path.parent)
                if diagnosis:
                    status, message = diagnosis["status"], diagnosis["error"]
            from .metrics import metric_error
            invalid_metrics = metric_error(metrics, resolved["benchmark"], task)
            if invalid_metrics and status in {"completed", "task_timeout", "agent_error"}:
                status, message, reward = "incomplete", invalid_metrics, None
                parse_errors.append(f"{path.parent.name}: {message}")
            if status in {"task_timeout", "agent_error"}:
                verifier = data.get("verifier") or {}
                if not (data.get("finished_at") and isinstance(verifier, dict)
                        and verifier.get("finished_at") and reward is not None):
                    status = "incomplete"
                    message = (message or "") + "; no completed native verification with a reward"
            contexts = ([data["agent_result"]] if data.get("agent_result") is not None else
                        [s["agent_result"] for s in data.get("step_results") or [] if s.get("agent_result") is not None])
            if any(not isinstance(context, dict) for context in contexts):
                raise ValueError("native agent results must be objects")
            trajectory = path.parent / "agent" / "trajectory.json"
            by_task[task].append(TrialResult(task_id=task, repetition=len(by_task[task]),
                status=status, reward=reward, metrics=metrics,
                input_tokens=_tokens(contexts, "n_input_tokens"),
                output_tokens=_tokens(contexts, "n_output_tokens"),
                cached_input_tokens=_tokens(contexts, "n_cache_tokens"),
                artifact_dir=str(path.parent), trajectory_path=str(trajectory) if trajectory.is_file() else None,
                error=message))
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            parse_errors.append(f"{path.name} in {path.parent.name}: {exc}")
    trials = []
    for task in expected:
        rows = by_task[task]
        if len(rows) > repetitions:
            parse_errors.append(f"unexpected extra trials for {task}: {len(rows)} > {repetitions}")
        while len(rows) < repetitions:
            rows.append(TrialResult(task, len(rows), "incomplete", error="trial result was not produced"))
        trials.extend(rows)
    native_summary = {}
    summary_path = native / "result.json"
    if summary_path.exists():
        try:
            native_summary = _read_native_json(summary_path)
            if not isinstance(native_summary, dict):
                raise ValueError("native job result must be an object")
            if not native_summary.get("finished_at"):
                raise ValueError("missing native job completion marker")
            total = native_summary.get("n_total_trials")
            if not isinstance(total, int) or isinstance(total, bool) or total != resolved["expected_trials"]:
                raise ValueError("native job trial count does not match the selected run")
        except (ValueError, OSError) as exc:
            parse_errors.append(f"native job result: {exc}")
            if not isinstance(native_summary, dict):
                native_summary = {}
    else:
        parse_errors.append("missing native job summary")
    issues = ([error] if error else []) + parse_errors
    if returncode not in (0, None):
        issues.append(f"Harbor exited with status {returncode}")
    # Harbor still runs the verifier after an agent timeout/nonzero exit. Those
    # measured outcomes belong in the benchmark, including a genuine score 0.
    # Authentication, quota and infrastructure failures are never policy scores.
    complete = sum(t.status in {"completed", "task_timeout", "agent_error"} for t in trials)
    if complete < resolved["expected_trials"] and not issues:
        first = next(t for t in trials if t.status not in {"completed", "task_timeout", "agent_error"})
        issues.append(f"{resolved['expected_trials'] - complete} trial(s) lack completed measurements; "
                      f"first failure {first.task_id}: {first.status}: {first.error or 'missing native result'}")
    status = "completed" if complete == resolved["expected_trials"] and not issues else ("partial" if complete else "failed")
    usage = {"elapsed_seconds": elapsed, "cost_usd": None,
             "cost_basis": "subscription_unknown" if resolved["target"]["entrypoint"] == CODEX_AGENT else "not_aggregated"}
    for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
        values = [getattr(t, key) for t in trials]
        usage[key] = sum(values) if values and all(v is not None for v in values) else None
        usage[f"observed_{key}"] = sum(v for v in values if v is not None)
    result = EvaluationResult(status, trials, native_summary, usage, str(native), resolved,
                              "; ".join(issues) or None, str(output / "result.json"))
    aggregation = resolved["benchmark"].get("aggregation")
    if aggregation:
        from .metrics import benchmark_metrics
        result.metrics = {**native_summary, "benchmark_metrics": benchmark_metrics(
            trials, resolved["benchmark"], repetitions, valid_run=status == "completed")}
    if write_report:
        _json_write(output / "result.json", result.to_dict())
    return result


def evaluate(*, target: TargetSpec, benchmark: BenchmarkSelection,
             options: RunOptions | None = None, output_dir: Path | str,
             resume: bool = False) -> EvaluationResult:
    """Run a selection, or explicitly resume its unchanged recorded experiment."""
    if resume:
        from .resume import resume_evaluation
        return resume_evaluation(output_dir, target=target, benchmark=benchmark, options=options or RunOptions())
    options = options or RunOptions()
    resolved = preview(target=target, benchmark=benchmark, options=options, output_dir=output_dir)
    output = Path(resolved["output_dir"])
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty; choose a new run directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    from .resume import evaluation_lock
    with evaluation_lock(output):
        return _evaluate_new(target, options, resolved)


def _evaluate_new(target: TargetSpec, options: RunOptions, resolved: dict[str, Any]) -> EvaluationResult:
    output = Path(resolved["output_dir"])
    from .resume import implementation_identity
    resolved["execution_identity"] = implementation_identity()
    copied_hash = snapshot(Path(target.artifact_dir), output / "candidate")
    if copied_hash != resolved["target"]["sha256"]:
        raise RuntimeError("candidate changed while being snapshotted; retry with a stable artifact")
    resolved["target"]["snapshot_dir"] = str(output / "candidate")
    _json_write(output / "resolved.json", resolved)
    version = _probe([options.harbor_executable, "--version"])
    if not version["ok"] or not re.search(r"(?<![\d.])0\.18\.0(?![\d.])", version["detail"]):
        return _setup_failure(resolved, f"requires Harbor {HARBOR_VERSION}: {version['detail']}")
    if options.runtime_snapshot:
        from .runtime_snapshot import load_snapshot, prepare_snapshot, preflight as frozen_preflight
        try:
            _, frozen = load_snapshot(options.runtime_snapshot, spec=resolved["benchmark"])
            if frozen["sha256"] != resolved["runtime_snapshot_sha256"]:
                raise ValueError("Frozen runtime changed after preview")
            resolved["benchmark_checks"] = frozen_preflight(resolved["benchmark"], options.benchmark_runtime)
            failures = [f"{name}: {row.get('detail', 'requirement not met')}"
                        for name, row in resolved["benchmark_checks"].items() if not row["ok"]]
            if failures:
                raise RuntimeError("; ".join(failures))
            resolved["execution_tasks"] = prepare_snapshot(options.runtime_snapshot, resolved["benchmark"], output / "tasks")
        except Exception as exc:
            return _setup_failure(resolved, f"frozen runtime preparation failed: {exc}")
        _json_write(output / "resolved.json", resolved)
    elif resolved["benchmark"].get("materializer"):
        from .materializers import preflight, prepare
        try:
            resolved["benchmark_checks"] = preflight(resolved["benchmark"], options.benchmark_runtime)
            _json_write(output / "resolved.json", resolved)
            failures = [f"{name}: {row.get('detail', 'requirement not met')}"
                        for name, row in resolved["benchmark_checks"].items() if not row["ok"]]
            if failures:
                raise RuntimeError("; ".join(failures))
            runtime_args = {"runtime_options": options.benchmark_runtime} if options.benchmark_runtime else {}
            resolved["execution_tasks"] = prepare(resolved["benchmark"], output / "tasks", **runtime_args)
        except Exception as exc:
            return _setup_failure(resolved, f"benchmark preparation failed: {exc}")
        _json_write(output / "resolved.json", resolved)
    config_path = output / "harbor.json"
    _json_write(config_path, _job_config(resolved, output / "candidate"))
    command = [options.harbor_executable, "run", "--config", str(config_path)]
    _json_write(output / "command.json", command)
    env = dict(os.environ)
    if resolved["benchmark"].get("materializer"):
        from .materializers import runtime_environment
        try:
            env.update(runtime_environment(resolved["benchmark"], options.benchmark_runtime))
        except Exception as exc:
            return _setup_failure(resolved, f"benchmark runtime environment failed: {exc}")
    import_paths = [str(Path(__file__).resolve().parents[1])]
    # The fixed target loads only agent.py explicitly. A candidate's json.py,
    # harbor/, or rsi_eval/ must not replace the evaluator's own dependencies.
    if target.entrypoint != CODEX_AGENT:
        import_paths.append(str(output / "candidate"))
        if env.get("PYTHONPATH"):
            import_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(import_paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    started = time.monotonic()
    error = None
    returncode = None
    with (output / "harbor.log").open("w") as log:
        process = None
        try:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            returncode = process.wait(timeout=options.timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            error = "evaluation interrupted" if isinstance(exc, KeyboardInterrupt) else "evaluation wall-time limit exceeded"
            if process is not None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    returncode = process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    returncode = process.wait()
        except OSError as exc:
            error = str(exc)
    try:
        modified = artifact_hash(output / "candidate") != copied_hash
    except (OSError, ValueError):
        modified = True
    if modified:
        error = (error + "; " if error else "") + "candidate snapshot was modified during evaluation"
    for task in resolved.get("execution_tasks", []):
        try:
            unchanged = artifact_hash(Path(task["path"])) == task["sha256"]
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
            error = (error + "; " if error else "") + f"prepared task was modified: {task['id']}"
    return collect_result(resolved, returncode=returncode, error=error, elapsed=time.monotonic() - started)


def _setup_failure(resolved: dict[str, Any], message: str) -> EvaluationResult:
    result = collect_result(resolved, returncode=None, error=message)
    for trial in result.trials:
        trial.status, trial.error = "infra_error", message
    _json_write(Path(result.report_path), result.to_dict())
    return result
