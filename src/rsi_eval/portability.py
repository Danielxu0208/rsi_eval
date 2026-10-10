"""Explicit, integrity-checked relocation of newly prepared managed experiments.

Bundles are directories, not executable installers. Environments and credentials
are supplied separately on import. Original control records remain in receipts;
candidate code, the frozen runtime and raw evaluation evidence keep their bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

from .artifacts import artifact_hash
from .evaluation import _json_write
from .relocation import map_text, map_value
from .resume import evaluation_lock

FORMAT = "rsi-eval-managed-bundle-v1"
# Lock files carry no lock ownership across copies. Preserve their bytes because
# native method checkpoints may include them in the protected evidence tree.
SKIP = {"__pycache__"}
CREDENTIAL_NAMES = {"auth.json", ".env", ".codex-auth"}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> dict:
    rows = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            rows[str(relative)] = {"link": os.readlink(path)}
        elif path.is_file():
            rows[str(relative)] = {"sha256": _sha(path), "executable": path.stat().st_mode & 0o111}
    return rows


def _platform() -> dict:
    return {"system": platform.system(), "machine": platform.machine()}


def _external_inputs(config: dict, root: Path, environment: Path) -> dict:
    """Record explicit local inputs; credentials and executables are supplied separately."""
    excluded = [root, environment, Path(config["codex"]["auth_dir"]).resolve()]
    inputs = {}
    def visit(value, key=None):
        if isinstance(value, dict):
            for name, item in value.items():
                visit(item, name)
        elif isinstance(value, list):
            for item in value:
                visit(item, key)
        elif isinstance(value, str) and key not in {"executable", "harbor_executable", "auth_dir", "codex_auth_dir"} and Path(value).is_absolute():
            path = Path(value).absolute()
            if any(path.is_relative_to(base) for base in excluded):
                return
            if not path.exists():
                if key in {"manifest", "runtime_snapshot", "assets_dir", "runtime_config", "source_dir", "dataset_python", "judge_env_file"}:
                    raise ValueError(f"Required external input is missing: {path}")
                return
            inputs[str(path)] = {"kind": "directory" if path.is_dir() else "file",
                                  "sha256": artifact_hash(path) if path.is_dir() else _sha(path)}
    visit(config)
    return inputs


def _harnessx_fingerprint(root: Path, record: dict) -> str:
    """Normalize with the experiment's exact optional implementation/environment."""
    from .methods import _environment, _python, environment_identity
    if artifact_hash(root / "runtime") != record["runtime_sha256"]:
        raise ValueError("Frozen runtime changed")
    if environment_identity(Path(record["environment"])) != record["environment_identity"]:
        raise ValueError("Method environment changed before fingerprint validation")
    code = ("import hashlib,json,sys; from pathlib import Path; "
            "from integrations.harnessx.launcher import load_experiment; "
            "print(hashlib.sha256(json.dumps(load_experiment(Path(sys.argv[1])),sort_keys=True).encode()).hexdigest())")
    result = subprocess.run([str(_python(Path(record["environment"]))), "-c", code,
                             str(root / "configuration.json")], cwd=root,
                            env=_environment(record), capture_output=True, text=True)
    value = result.stdout.strip()
    if result.returncode or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"Frozen HarnessX configuration validation failed: {result.stderr[-2000:]}")
    return value


def _validate_record(root: Path, record: dict) -> None:
    from .methods import _verify_upstream
    if record.get("schema_version") != 2 or record.get("status") != "prepared":
        raise ValueError("Export requires a completed preparation in the current managed format; legacy experiments are not migrated")
    if record["experiment"] != str(root):
        raise ValueError("Experiment moved without a verified import")
    if artifact_hash(root / "runtime") != record["runtime_sha256"]:
        raise ValueError("Frozen runtime changed")
    if not (root / "runtime/rsi_eval/relocation.py").is_file():
        raise ValueError("This frozen runtime predates portable managed experiments")
    if json.loads((root / "configuration.json").read_text()) != record["configuration"]:
        raise ValueError("Prepared configuration changed")
    if record.get("seed_sha256") and artifact_hash(root / "seed") != record["seed_sha256"]:
        raise ValueError("Prepared seed changed")
    _verify_upstream(root / "upstream-source", record["identity"]["upstream_commit"])
    if record["method"] == "meta-harness" and (root / "run/resume.json").is_file():
        from integrations.meta_harness.state import tree
        data = json.loads((root / "run/resume.json").read_text())
        current = tree(root / "run/upstream")
        for path, expected in data.get("protected", {}).items():
            if current.get(path) != expected:
                raise ValueError(f"Meta-Harness recorded evidence changed: {path}")
        if data.get("summary") is not None and json.loads((root / "run/result.json").read_text()) != data["summary"]:
            raise ValueError("Meta-Harness completed report changed")
    if record["method"] == "harnessx" and (root / "run/result.json").is_file():
        expected = _harnessx_fingerprint(root, record)
        if json.loads((root / "run/result.json").read_text())["config_sha256"] != expected:
            raise ValueError("HarnessX experiment fingerprint changed")


def export_experiment(experiment: Path, output: Path) -> dict:
    root, output = experiment.expanduser().resolve(), output.expanduser().resolve()
    if output.exists() or output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Bundle destination must be new and outside the experiment")
    with evaluation_lock(root):
        record = json.loads((root / "method.json").read_text())
        _validate_record(root, record)
        environment = Path(record["environment"]).resolve()
        external_inputs = _external_inputs(record["configuration"], root, environment)
        def ignore(directory, names):
            ignored = []
            for name in names:
                path = Path(directory) / name
                if name in CREDENTIAL_NAMES:
                    raise ValueError(f"Credentials cannot be exported: {path}")
                if name in SKIP or path.resolve() == environment:
                    ignored.append(name)
            return ignored
        output.mkdir(parents=True)
        payload = output / "experiment"
        shutil.copytree(root, payload, symlinks=True, ignore=ignore)
        rows = _files(payload)
        for relative, row in rows.items():
            if "link" in row:
                source = root / relative
                if not source.resolve().is_relative_to(root):
                    raise ValueError(f"External symlink cannot be exported: {relative}")
        manifest = {"format": FORMAT, "source": str(root), "platform": _platform(),
                    "method": record["method"], "environment_identity": record["environment_identity"],
                    "files": rows, "external_inputs": external_inputs,
                    "credentials_included": False, "environment_included": False}
        _json_write(output / "bundle.json", manifest)
        return {"bundle": str(output), "method": record["method"], "files": len(rows),
                "model_calls": 0, "environment_included": False, "credentials_included": False}


def _immutable(path: Path, root: Path, evaluations: set[Path]) -> bool:
    relative = path.relative_to(root)
    if relative.parts[0] in {"runtime", "seed", "upstream-source", "import-receipts"}:
        return True
    if ".git" in relative.parts or any(p in {"candidate", "candidates", "agents", "workspace", "selected_candidate", "harness"}
                                        for p in relative.parts):
        return True
    if relative.parts[0] == "checkout":
        return True  # Keep tracked integration commits clean; readers apply relocation.
    return any(path.is_relative_to(directory) for directory in evaluations)


def _rewrite_controls(root: Path, mappings: dict, receipt: Path) -> None:
    evaluations = {path.parent for path in root.rglob("resolved.json")
                   if "runtime" not in path.relative_to(root).parts and path.parent.joinpath("candidate").is_dir()}
    changes = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or _immutable(path, root, evaluations):
            continue
        if path.suffix not in {".json", ".jsonl", ".yaml", ".yml"} or path.name == ".rsi-relocation.json":
            continue
        original = path.read_text()
        if path.suffix == ".json":
            try:
                value = json.loads(original)
            except json.JSONDecodeError:
                continue  # Preserve incomplete attempt bytes for native recovery.
            changed = map_value(value, mappings)
            updated = json.dumps(changed, indent=2) + "\n" if changed != value else original
        else:
            updated = map_text(original, mappings)
        if updated != original:
            relative = path.relative_to(root)
            archived = receipt / "original-controls" / relative
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, archived)
            path.write_text(updated)
            changes.append({"path": str(relative), "original_sha256": _sha(archived), "rebased_sha256": _sha(path)})
    _json_write(receipt / "control-changes.json", changes)


def _repair_git(root: Path, mappings: dict) -> None:
    # Repair only Git's worktree administration. Committed source and objects stay byte-identical.
    for path in root.rglob(".git"):
        if path.is_file():
            text = path.read_text()
            rebased = map_text(text, mappings)
            if text != rebased:
                path.write_text(rebased)
        elif path.is_dir() and (path / "worktrees").is_dir():
            for gitdir in (path / "worktrees").glob("*/gitdir"):
                gitdir.write_text(map_text(gitdir.read_text(), mappings))
            result = subprocess.run(["git", "-C", str(path.parent), "worktree", "repair"], capture_output=True, text=True)
            if result.returncode:
                raise ValueError(f"Cannot repair relocated Git worktrees: {result.stderr}")


def import_experiment(bundle: Path, experiment: Path, environment: Path, auth_dir: Path,
                      *, codex_executable: str | None = None, path_map: dict | None = None) -> dict:
    from .methods import environment_identity
    bundle, root = bundle.expanduser().resolve(), experiment.expanduser().resolve()
    environment, auth_dir = environment.expanduser().absolute(), auth_dir.expanduser().resolve()
    if root.exists() or root.is_relative_to(bundle) or bundle.is_relative_to(root):
        raise ValueError("Import destination must be new and outside the bundle")
    if auth_dir.is_relative_to(root) or environment.is_relative_to(root):
        raise ValueError("Supply credentials and the reusable environment outside the imported experiment")
    manifest = json.loads((bundle / "bundle.json").read_text())
    payload = bundle / "experiment"
    if manifest.get("format") != FORMAT or manifest.get("platform") != _platform():
        raise ValueError("Bundle format or platform differs; cross-platform environment migration is unsupported")
    if _files(payload) != manifest["files"]:
        raise ValueError("Bundle integrity check failed")
    if any((payload / name).is_symlink() for name in ("method.json", "configuration.json")):
        raise ValueError("Bundle control files must be regular files")
    if environment_identity(environment) != manifest["environment_identity"]:
        raise ValueError("Import requires the same Python and exact installed dependency versions")
    original = json.loads((payload / "method.json").read_text())
    source = manifest["source"]
    mappings = {source: str(root), original["environment"]: str(environment),
                original["configuration"]["codex"]["auth_dir"]: str(auth_dir), **(path_map or {})}
    if source in (path_map or {}) and path_map[source] != str(root):
        raise ValueError("A custom path mapping cannot override the experiment destination")
    for old in path_map or {}:
        if old not in manifest.get("external_inputs", {}):
            raise ValueError("Custom path mappings may only relocate recorded external inputs")
    if codex_executable:
        old_executable = original["configuration"]["codex"].get("executable", "codex")
        if old_executable == "codex":
            raise ValueError("A PATH-resolved Codex executable must remain PATH-resolved; attach it through the environment PATH")
        mappings[old_executable] = str(Path(codex_executable).expanduser().resolve())
    for old, new in mappings.items():
        if not Path(old).is_absolute() or not Path(new).is_absolute():
            raise ValueError("Relocation mappings must contain absolute paths")
    for old, identity in manifest.get("external_inputs", {}).items():
        path = Path(map_text(old, mappings))
        current = artifact_hash(path) if identity["kind"] == "directory" else _sha(path)
        if current != identity["sha256"]:
            raise ValueError(f"Relocated external input changed: {path}")
    previous = payload / ".rsi-relocation.json"
    if previous.exists():
        for old, current in json.loads(previous.read_text())["mappings"].items():
            mappings.setdefault(old, map_text(current, mappings))
    shutil.copytree(payload, root, symlinks=True)
    receipt = root / "import-receipts" / f"import-{len(list((root / 'import-receipts').glob('import-*'))) + 1:04d}"
    receipt.mkdir(parents=True)
    _json_write(receipt / "bundle.json", manifest)
    _json_write(root / ".rsi-relocation.json", {"format": "rsi-eval-relocation-v1", "destination": str(root),
                "mappings": mappings, "receipt": str(receipt.relative_to(root))})
    # Any interruption before the final write leaves a clearly unlaunchable import.
    pending = map_value(original, mappings)
    pending["status"] = "importing"
    _json_write(root / "method.json", pending)
    try:
        for relative, row in manifest["files"].items():
            if "link" not in row:
                continue
            link = root / relative
            old_link = Path(row["link"])
            old_target = old_link if old_link.is_absolute() else Path(source) / relative / ".." / old_link
            target = Path(map_text(str(old_target.resolve()), mappings))
            if not target.is_relative_to(root):
                raise ValueError(f"Relocated symlink escapes the experiment: {relative}")
            if old_link.is_absolute():
                link.unlink()
                link.symlink_to(target, target_is_directory=target.is_dir())
        _rewrite_controls(root, mappings, receipt)
        _repair_git(root, mappings)
        record = map_value(original, mappings)
        record["environment_owned"] = False
        record["environment_identity"] = manifest["environment_identity"]
        config = json.loads((root / "configuration.json").read_text())
        record["configuration"] = config
        record["configuration_sha256"] = _sha(root / "configuration.json")
        if record["method"] == "meta-harness" and (root / "run/resume.json").exists():
            checkpoint = root / "run/resume.json"
            data = json.loads(checkpoint.read_text())
            # Preserve candidate/raw-evidence hashes. Only control records whose
            # original bytes were verified and archived may acquire new hashes.
            for change in json.loads((receipt / "control-changes.json").read_text()):
                path = Path(change["path"])
                if not path.is_relative_to("run/upstream"):
                    continue
                relative = str(path.relative_to("run/upstream"))
                if relative not in data["protected"]:
                    continue
                if data["protected"][relative] != change["original_sha256"]:
                    raise ValueError(f"Recorded Meta-Harness control changed before import: {relative}")
                data["protected"][relative] = change["rebased_sha256"]
            _json_write(checkpoint, data)
        if record["method"] == "harnessx" and (root / "run/result.json").exists():
            state_path = root / "run/result.json"
            state = json.loads(state_path.read_text())
            state["config_sha256"] = _harnessx_fingerprint(root, record)
            _json_write(state_path, state)
        if artifact_hash(root / "runtime") != original["runtime_sha256"]:
            raise ValueError("Import modified the frozen runtime")
        _validate_record(root, record)
        _json_write(receipt / "result.json", {"status": "completed", "source": source, "destination": str(root),
                    "mappings": mappings, "model_calls": 0, "runtime_sha256": record["runtime_sha256"]})
        _json_write(root / "method.json", record)
        return {"status": "imported", "experiment": str(root), "method": record["method"],
                "receipt": str(receipt), "model_calls": 0}
    except BaseException as exc:
        _json_write(receipt / "result.json", {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        raise


def add_portability_commands(commands) -> None:
    parser = commands.add_parser("experiment", help="Export/import newly prepared managed experiments")
    actions = parser.add_subparsers(dest="experiment_action", required=True)
    export = actions.add_parser("export")
    export.add_argument("--experiment", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    importing = actions.add_parser("import")
    importing.add_argument("--bundle", type=Path, required=True)
    importing.add_argument("--experiment", type=Path, required=True)
    importing.add_argument("--environment", type=Path, required=True)
    importing.add_argument("--auth-dir", type=Path, required=True)
    importing.add_argument("--codex")
    importing.add_argument("--path-map", type=Path, help="JSON mapping for unchanged external inputs at new absolute paths")


def dispatch_portability(args) -> int:
    if args.experiment_action == "export":
        result = export_experiment(args.experiment, args.output)
    else:
        result = import_experiment(args.bundle, args.experiment, args.environment, args.auth_dir,
                                   codex_executable=args.codex,
                                   path_map=json.loads(args.path_map.read_text()) if args.path_map else None)
    print(json.dumps(result, indent=2))
    return 0
