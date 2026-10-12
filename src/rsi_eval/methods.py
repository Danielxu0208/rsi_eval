"""Thin setup and launch commands for optional methods, without an optimizer API."""
from __future__ import annotations

import argparse
from copy import deepcopy
from importlib.resources import files
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .artifacts import artifact_hash, snapshot
from .evaluation import _json_write
from .resources import codex_template, integration_resource
from .resume import evaluation_lock

METHODS = {
    "meta-harness": {"module": "meta_harness", "url": "https://github.com/stanford-iris-lab/meta-harness.git",
                     "commit": "8123ccabe2b19fa1123090b2e5f1bacc20963ce8", "example": "example.json"},
    "rrsi": {"module": "rrsi", "url": "https://github.com/google-research/rrsi.git",
             "commit": "be50316e1db05914068a973f322770ef08ed7ba1", "example": "example.json"},
    "ahe": {"module": "ahe", "url": "https://github.com/china-qijizhifeng/agentic-harness-engineering.git",
            "commit": "8b2a55d97590363fe50c3cc6b5e833b020a4bb4c", "example": "example.json"},
    "harnessx": {"module": "harnessx", "url": "https://github.com/Darwin-Agent/HarnessX.git",
                 "commit": "915520584c92cc35ed24aef1af17eec1d60c0b01", "example": "example.json"},
}


def _absolute(value: str, base: Path) -> str:
    path = Path(os.path.expandvars(value)).expanduser()
    return str((path if path.is_absolute() else base / path).resolve())


def _python(environment: Path) -> Path:
    # Do not resolve the final symlink: its venv identity is significant.
    return environment / "bin/python"


def _has_pip(python: Path) -> bool:
    result = subprocess.run([str(python), "-m", "pip", "--version"],
                            capture_output=True, text=True)
    return result.returncode == 0


def _invoke(command: list[str], *, cwd: Path, log: Path, env: dict | None = None) -> None:
    with log.open("a") as stream:
        stream.write(json.dumps({"command": command}) + "\n")
        stream.flush()
        result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"Command failed with exit {result.returncode}; see {log}")


def _runtime_copy(destination: Path) -> str:
    if destination.exists():
        raise FileExistsError(f"Runtime snapshot already exists: {destination}")
    destination.mkdir()
    roots = {"rsi_eval": Path(__file__).parent,
             "integrations": Path(str(files("integrations"))),
             "rsi_eval_templates": codex_template().parent}
    for name, source in roots.items():
        target = destination / name
        snapshot(source, target)
    return artifact_hash(destination)


def _environment(record: dict) -> dict:
    root = Path(record["experiment"])
    return {**os.environ, "PYTHONPATH": str(root / "runtime"),
            "PATH": str(Path(record["environment"]) / "bin") + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}


def environment_identity(environment: Path) -> dict:
    code = ("import importlib.metadata as m,json,platform; "
            "print(json.dumps({'python':platform.python_version(),'packages':"
            "{d.metadata['Name'].lower():d.version for d in m.distributions()}}))")
    result = subprocess.run([str(_python(environment)), "-I", "-c", code], capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    if tuple(map(int, value["python"].split(".")[:2])) < (3, 12):
        raise ValueError("Method execution requires Python 3.12 or newer")
    if value["packages"].get("harbor") != "0.18.0":
        raise ValueError("Method environment requires Harbor 0.18.0")
    return value


def configure(*, model: str, auth_dir: Path, output: Path, executable: str = "codex",
              expected_version: str | None = None, reasoning_effort: str = "medium") -> dict:
    """Save only non-secret shared settings. Probe CLI version without inference."""
    from .codex import CodexSettings, codex_version
    output = output.expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"Profile already exists: {output}")
    if "/" in executable:
        executable = _absolute(executable, Path.cwd())
    settings = CodexSettings(model=model, auth_dir=auth_dir, executable=executable,
                             expected_version=expected_version, reasoning_effort=reasoning_effort)
    version = codex_version(settings)
    profile = {"codex": {"model": model, "auth_dir": str(settings.auth_dir),
                         "executable": executable, "expected_version": version,
                         "reasoning_effort": reasoning_effort}}
    output.parent.mkdir(parents=True, exist_ok=True)
    _json_write(output, profile)
    return {"profile": str(output), "model": model, "codex_version": version, "model_calls": 0}


def _config(method: str, config_file: Path | None, profile_file: Path | None, root: Path,
            environment: Path) -> dict:
    from integrations.runtime_options import normalize_run_options, normalize_selection
    from .method_policy import normalize_policy
    spec = METHODS[method]
    source = (config_file or integration_resource(spec["module"], spec["example"])).resolve()
    value = json.loads(source.read_text())
    profile = json.loads(profile_file.read_text()) if profile_file else {}
    # An explicitly supplied shared profile takes precedence over example defaults.
    value["codex"] = {**value.get("codex", {}), **profile.get("codex", {})}
    codex = value["codex"]
    if not all(codex.get(key) for key in ("model", "auth_dir")):
        raise ValueError("Set model and dedicated auth_dir in a profile or method configuration")
    auth_base = profile_file.resolve().parent if profile_file and "auth_dir" in profile.get("codex", {}) else source.parent
    codex["auth_dir"] = _absolute(codex["auth_dir"], auth_base)
    if Path(codex["auth_dir"]).is_relative_to(root):
        raise ValueError("The dedicated authentication directory must be outside the experiment")
    if "/" in codex.get("executable", "codex"):
        binary_base = profile_file.resolve().parent if profile_file and "executable" in profile.get("codex", {}) else source.parent
        codex["executable"] = _absolute(codex["executable"], binary_base)
    if not codex.get("expected_version"):
        from .codex import CodexSettings, codex_version
        codex["expected_version"] = codex_version(CodexSettings(**codex))
    for role in ("evolve", "test", "smoke"):
        if role in value:
            row = value[role]
            value[role] = ([normalize_selection(s, source.parent) for s in row]
                           if isinstance(row, list) else normalize_selection(row, source.parent))
    value["run_options"] = normalize_run_options(value.get("run_options", {}), source.parent)
    value["run_options"]["harbor_executable"] = str(environment / "bin/harbor")
    if value.get("method_policy"):
        declared_seed = Path(_absolute(value["seed"], source.parent)) if config_file and value.get("seed") else codex_template()
        value["method_policy"] = normalize_policy(value["method_policy"], source.parent,
                                                  candidate_roots=[declared_seed, root / "seed", root / "run/candidates"])
    if method != "harnessx":
        seed = Path(_absolute(value["seed"], source.parent)) if config_file and value.get("seed") else codex_template()
        value["seed"] = str(seed)
    elif value.get("seed"):
        value["seed"] = _absolute(value["seed"], source.parent)
    if method == "harnessx":
        value["checkout"] = str(root / "checkout")
        value["output_dir"] = str(root / "run")
    return value


def _git_output(directory: Path, *arguments: str) -> str | None:
    result = subprocess.run(["git", "-C", str(directory), *arguments],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _verify_upstream(directory: Path, commit: str) -> None:
    if directory.is_symlink() or _git_output(directory, "rev-parse", "HEAD") != commit:
        raise ValueError("Prepared upstream source pin changed")
    if _git_output(directory, "status", "--porcelain", "--untracked-files=no") != "":
        raise ValueError("Prepared upstream source has tracked changes")


def _directory_stage(record: dict, marker: Path, name: str, status: str) -> None:
    record.setdefault("managed_directories", {})[name] = status
    _json_write(marker, record)


def _archive_incomplete(root: Path, name: str, record: dict, marker: Path) -> None:
    """Retain partial setup bytes only when this preparation recorded ownership."""
    path = root / name
    if record.get("managed_directories", {}).get(name) != "creating":
        raise ValueError(f"Cannot recreate unrecorded or completed managed directory: {path}")
    if path.is_symlink():
        raise ValueError(f"Refusing to recover a managed-directory symlink: {path}")
    archive_root = root / "prepare-attempts"
    archive_root.mkdir(exist_ok=True)
    count = len(list(archive_root.iterdir())) + 1
    destination = archive_root / f"{count:04d}-{name}"
    shutil.move(str(path), str(destination))
    record.setdefault("preparation_recoveries", []).append({"directory": name,
        "archive": str(destination.relative_to(root)), "reason": "incomplete managed preparation"})
    _json_write(marker, record)


def _prepare_upstream(root: Path, source: str, commit: str, record: dict, marker: Path, log: Path) -> Path:
    directory = root / "upstream-source"
    state = record.get("managed_directories", {}).get("upstream-source")
    if directory.exists():
        if state == "complete":
            _verify_upstream(directory, commit)
            return directory
        if directory.is_symlink():
            raise ValueError("Upstream source cannot be a symlink")
        head = _git_output(directory, "rev-parse", "HEAD")
        pin = _git_output(directory, "rev-parse", f"{commit}^{{commit}}")
        clean = _git_output(directory, "status", "--porcelain", "--untracked-files=no")
        if head is None or pin != commit or clean != "":
            _archive_incomplete(root, "upstream-source", record, marker)
        elif state != "creating":
            raise ValueError("Existing upstream clone was not recorded by this preparation")
    if not directory.exists():
        _directory_stage(record, marker, "upstream-source", "creating")
        _invoke(["git", "clone", "--no-hardlinks", source, str(directory)], cwd=root, log=log)
    # A complete clone can survive an interruption before this checkout. Always
    # verify and detach the pin before declaring that preparation complete.
    _invoke(["git", "-C", str(directory), "checkout", "--detach", commit], cwd=root, log=log)
    _verify_upstream(directory, commit)
    _directory_stage(record, marker, "upstream-source", "complete")
    return directory


def prepare_method(*, method: str, experiment: Path, config_file: Path | None = None,
                   profile_file: Path | None = None, source: str | None = None,
                   environment: Path | None = None, install: bool = True,
                   dependency_lock: Path | None = None) -> dict:
    if method not in METHODS:
        raise ValueError(f"Unknown method: {method}")
    root = experiment.expanduser().resolve()
    if root.exists() and any(p.name != ".evaluation.lock" for p in root.iterdir()) and not (root / "method.json").is_file():
        raise FileExistsError("Use an empty experiment directory or resume its recorded preparation")
    root.mkdir(parents=True, exist_ok=True)
    env_dir = environment.expanduser().absolute() if environment else root / "environment"
    from .dependency_locks import create_lock, load_lock, validate_platform, verify_environment
    package_lock = load_lock(dependency_lock, method=method) if dependency_lock else None
    with evaluation_lock(root):
        marker = root / "method.json"
        config = _config(method, config_file, profile_file, root, env_dir)
        seed_source = Path(config["seed"]) if method != "harnessx" else None
        seed_identity = {"path": str(seed_source), "sha256": artifact_hash(seed_source)} if seed_source else None
        if seed_source:
            config["seed"] = str(root / "seed")
        spec = METHODS[method]
        source = source or spec["url"]
        if Path(source).expanduser().exists():
            source = str(Path(source).expanduser().resolve())
        identity = {"method": method, "config": config, "environment": str(env_dir),
                    "source": source, "upstream_commit": spec["commit"], "seed_source": seed_identity}
        if package_lock:
            identity["dependency_lock_sha256"] = package_lock["sha256"]
        if marker.exists():
            record = json.loads(marker.read_text())
            if record["identity"] != identity:
                raise ValueError("Preparation settings changed; use a new experiment directory")
            if record.get("runtime_sha256") and artifact_hash(root / "runtime") != record["runtime_sha256"]:
                raise ValueError("Prepared runtime snapshot changed")
            if record["status"] == "prepared":
                if json.loads((root / "configuration.json").read_text()) != record["configuration"]:
                    raise ValueError("Prepared configuration changed")
                if record.get("seed_sha256") and artifact_hash(root / "seed") != record["seed_sha256"]:
                    raise ValueError("Prepared seed changed")
                _verify_upstream(root / "upstream-source", spec["commit"])
                return record
        else:
            record = {"schema_version": 2, "method": method, "experiment": str(root),
                      "environment": str(env_dir), "identity": identity,
                      "status": "preparing", "model_calls": 0, "managed_directories": {},
                      "environment_owned": environment is None}
            _json_write(marker, record)
        log = root / "prepare.log"
        python = _python(env_dir)
        try:
            if not record.get("runtime_sha256"):
                if (root / "runtime").exists():
                    _archive_incomplete(root, "runtime", record, marker)
                _directory_stage(record, marker, "runtime", "creating")
                record["runtime_sha256"] = _runtime_copy(root / "runtime")
                _directory_stage(record, marker, "runtime", "complete")
            if package_lock:
                adapter_requirements = root / "runtime/integrations" / spec["module"] / "requirements.txt"
                requested_hashes = {str(adapter_requirements.relative_to(root / "runtime")): hashlib.sha256(adapter_requirements.read_bytes()).hexdigest()} if adapter_requirements.exists() else {}
                if package_lock.get("requirements_sha256") != requested_hashes:
                    raise ValueError("Dependency lock was resolved for different adapter requirements")
            _json_write(root / "configuration.json", config)
            if seed_source:
                destination = root / "seed"
                if destination.exists() and artifact_hash(destination) != seed_identity["sha256"]:
                    _archive_incomplete(root, "seed", record, marker)
                if not destination.exists():
                    _directory_stage(record, marker, "seed", "creating")
                    snapshot(seed_source, destination)
                record["seed_sha256"] = artifact_hash(destination)
                _directory_stage(record, marker, "seed", "complete")
            if not python.exists():
                if not install:
                    raise ValueError("--no-install requires an existing method environment")
                if record.get("environment_owned"):
                    _directory_stage(record, marker, "environment", "creating")
                _invoke([sys.executable, "-m", "venv", str(env_dir)], cwd=root, log=log)
            if install:
                if not _has_pip(python):
                    if not record.get("environment_owned"):
                        raise ValueError(f"Selected environment has no working pip; run {python} -m ensurepip --upgrade, or choose a new environment")
                    # A killed stdlib venv invocation may create Python before
                    # ensurepip finishes. Repair only our own managed venv.
                    _directory_stage(record, marker, "environment", "creating")
                    _invoke([sys.executable, "-m", "venv", str(env_dir)], cwd=root, log=log)
                    if not _has_pip(python):
                        raise ValueError("Managed environment still has no working pip after venv recovery")
                if record.get("environment_owned"):
                    _directory_stage(record, marker, "environment", "complete")
                command = [str(python), "-m", "pip", "install", "--report", str(root / "dependency-install-report.json")]
                if package_lock:
                    validate_platform(package_lock, python)
                    _json_write(root / "dependency-lock.json", package_lock)
                    requirements = root / "dependency-lock.txt"
                    requirements.write_text("\n".join(package_lock["requirements"]) + "\n")
                    command += ["-r", str(requirements)]
                else:
                    command += ["harbor==0.18.0"]
                    requirements = root / "runtime/integrations" / spec["module"] / "requirements.txt"
                    if requirements.exists():
                        command += ["-r", str(requirements)]
                _invoke(command, cwd=root, log=log)
                _invoke([str(python), "-m", "pip", "check"], cwd=root, log=log)
            if package_lock:
                verify_environment(package_lock, python)
            env = _environment(record)
            upstream = _prepare_upstream(root, source, spec["commit"], record, marker, log)
            checkout = root / "checkout"
            manifests = {"rrsi": ".rsi-eval-integration.json", "ahe": "prepared.json", "harnessx": ".rsi-eval-harnessx.json"}
            if method in manifests:
                manifest = checkout / manifests[method]
                if checkout.exists() and not manifest.is_file():
                    _archive_incomplete(root, "checkout", record, marker)
                if not checkout.exists():
                    _directory_stage(record, marker, "checkout", "creating")
                if method == "rrsi":
                    _invoke([str(python), "-m", "integrations.rrsi", "prepare", "--source", str(upstream),
                             "--checkout", str(checkout), "--config", str(root / "configuration.json"),
                             "--seed", str(root / "seed")], cwd=root, log=log, env=env)
                elif method == "ahe":
                    if manifest.is_file():
                        code = "from pathlib import Path; import sys; from integrations.ahe.launcher import verify_prepared; verify_prepared(Path(sys.argv[1]))"
                        _invoke([str(python), "-c", code, str(checkout)], cwd=root, log=log, env=env)
                    else:
                        _invoke([str(python), "-m", "integrations.ahe", "prepare", "--source", str(upstream),
                                 "--checkout", str(checkout)], cwd=root, log=log, env=env)
                else:
                    _invoke([str(python), "-m", "integrations.harnessx", "prepare", "--source", str(upstream),
                             "--checkout", str(checkout)], cwd=root, log=log, env=env)
                _directory_stage(record, marker, "checkout", "complete")
            if method == "harnessx" and config.get("seed"):
                destination = root / "seed"
                if destination.exists():
                    if record.get("seed_sha256"):
                        if artifact_hash(destination) != record["seed_sha256"]:
                            raise ValueError("Prepared custom seed changed")
                    else:
                        _archive_incomplete(root, "seed", record, marker)
                if not destination.exists():
                    _directory_stage(record, marker, "seed", "creating")
                    code = ("from pathlib import Path; import sys; from integrations.harnessx.bundle import freeze_config; "
                            "freeze_config(Path(sys.argv[1]),Path(sys.argv[2]))")
                    _invoke([str(python), "-c", code, config["seed"], str(destination)], cwd=root, log=log, env=env)
                    record["seed_sha256"] = artifact_hash(destination)
                    _directory_stage(record, marker, "seed", "complete")
                config = deepcopy(config)
                config["seed"] = str(destination / "config.yaml")
                _json_write(root / "configuration.json", config)
            if package_lock:
                # Also retain an explicitly supplied lock with --no-install.
                _json_write(root / "dependency-lock.json", package_lock)
                record["dependency_lock_sha256"] = package_lock["sha256"]
            elif _has_pip(python):
                requirements = root / "runtime/integrations" / spec["module"] / "requirements.txt"
                hashes = {str(requirements.relative_to(root / "runtime")): hashlib.sha256(requirements.read_bytes()).hexdigest()} if requirements.exists() else {}
                recorded_lock = create_lock(python, method=method, destination=root / "dependency-lock.json", requirements=hashes)
                record["dependency_lock_sha256"] = recorded_lock["sha256"]
            else:
                record["dependency_lock_status"] = "unavailable: supplied environment has no working pip; package inventory is recorded separately"
            record.update(status="prepared", configuration_sha256=hashlib.sha256((root / "configuration.json").read_bytes()).hexdigest(),
                          configuration=config, environment_identity=environment_identity(env_dir))
            record.pop("error", None)
        except BaseException as exc:
            record.update(status="preparation_failed", error=f"{type(exc).__name__}: {exc}")
            _json_write(marker, record)
            raise
        _json_write(marker, record)
        return record


def method_command(record: dict, action: str, *, selection_file: Path | None = None,
                   output: Path | None = None, test_resume: bool = False) -> list[str]:
    root = Path(record["experiment"])
    python = str(_python(Path(record["environment"])))
    config = str(root / "configuration.json")
    method = record["method"]
    resume = action == "resume"
    if action == "test":
        if output is None:
            raise ValueError("Independent method test requires --output")
        return [python, "-m", "rsi_eval.method_test", "--experiment", str(root),
                "--output", str(output.expanduser().resolve()),
                *(["--selection", str(selection_file.expanduser().resolve())] if selection_file else []),
                *(["--resume"] if test_resume else [])]
    if method == "meta-harness":
        if action == "check":
            return [python, "-m", "rsi_eval.method_check", str(root)]
        return [python, "-m", "integrations.meta_harness.launcher", "--config", config,
                "--output-dir", str(root / "run"), "--upstream-source", str(root / "upstream-source"),
                "--template", str(root / "seed"), *(["--resume"] if resume else [])]
    if method == "rrsi":
        if action == "check":
            return [python, "-m", "rsi_eval.method_check", str(root)]
        return [python, "-m", "integrations.rrsi", "resume" if resume else "run",
                "--checkout", str(root / "checkout"), "--runs", str(root / "run")]
    if method == "ahe":
        return [python, "-m", "integrations.ahe", "check" if action == "check" else "run",
                "--config", config, "--checkout", str(root / "checkout"),
                *([] if action == "check" else ["--output", str(root / "run")]),
                *(["--resume"] if resume else [])]
    return [python, "-m", "integrations.harnessx", "check" if action == "check" else "run", config,
            *(["--resume"] if resume else [])]


def launch_method(experiment: Path, action: str, *, selection_file: Path | None = None,
                  output: Path | None = None, test_resume: bool = False) -> int:
    if action not in {"check", "run", "resume", "test"}:
        raise ValueError("Unknown method action")
    root = experiment.expanduser().resolve()
    with evaluation_lock(root):
        record = json.loads((root / "method.json").read_text())
        if record.get("status") != "prepared":
            raise ValueError("Method preparation has not completed")
        if record["experiment"] != str(root) or record["method"] not in METHODS:
            raise ValueError("Method experiment identity mismatch")
        from .relocation import manifest_for
        manifest_for(root)  # An imported experiment must retain its completed receipt.
        if json.loads((root / "configuration.json").read_text()) != record["configuration"]:
            raise ValueError("Experiment configuration changed after preparation")
        if artifact_hash(root / "runtime") != record["runtime_sha256"]:
            raise ValueError("Experiment runtime snapshot changed after preparation")
        if record.get("seed_sha256") and artifact_hash(root / "seed") != record["seed_sha256"]:
            raise ValueError("Prepared seed changed; prepare a new experiment")
        _verify_upstream(root / "upstream-source", record["identity"]["upstream_commit"])
        if environment_identity(Path(record["environment"])) != record["environment_identity"]:
            raise ValueError("Method environment changed after preparation; prepare a new experiment")
        result = subprocess.run(method_command(record, action, selection_file=selection_file,
                                              output=output, test_resume=test_resume),
                                cwd=root, env=_environment(record))
        return result.returncode


def add_commands(commands: Any) -> None:
    p = commands.add_parser("configure", help="Save shared non-secret Codex settings; no model call")
    p.add_argument("--model", required=True)
    p.add_argument("--auth-dir", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--codex", default="codex")
    p.add_argument("--codex-version")
    p.add_argument("--reasoning-effort", default="medium")
    p = commands.add_parser("method", help="Prepare and invoke an optional native method")
    methods = p.add_subparsers(dest="method_action", required=True)
    methods.add_parser("list")
    p = methods.add_parser("prepare")
    p.add_argument("--method", choices=list(METHODS), required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--profile", type=Path)
    p.add_argument("--experiment", type=Path, required=True)
    p.add_argument("--source", help="Existing upstream clone or its URL")
    p.add_argument("--environment", type=Path, help="Reuse a dedicated method venv")
    p.add_argument("--no-install", action="store_true", help="Use existing dependencies without installing packages")
    p.add_argument("--dependency-lock", type=Path, help="Replay an exact package-version lock for this host platform")
    for action in ("check", "run", "resume"):
        p = methods.add_parser(action)
        p.add_argument("--experiment", type=Path, required=True)
    p = methods.add_parser("test", help="Evaluate the measured selected candidate using the frozen runtime")
    p.add_argument("--experiment", type=Path, required=True)
    p.add_argument("--selection", type=Path, help="JSON file containing a benchmark selection or list; defaults to configured test")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--resume", action="store_true", help="Resume this independent test with unchanged inputs")


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "configure":
        result = configure(model=args.model, auth_dir=args.auth_dir, output=args.output,
                           executable=args.codex, expected_version=args.codex_version,
                           reasoning_effort=args.reasoning_effort)
    elif args.method_action == "list":
        result = METHODS
    elif args.method_action == "prepare":
        result = prepare_method(method=args.method, experiment=args.experiment, config_file=args.config,
                                profile_file=args.profile, source=args.source, environment=args.environment,
                                install=not args.no_install, dependency_lock=args.dependency_lock)
    elif args.method_action == "test":
        return launch_method(args.experiment, "test", selection_file=args.selection,
                             output=args.output, test_resume=args.resume)
    else:
        return launch_method(args.experiment, args.method_action)
    print(json.dumps(result, indent=2))
    return 0
