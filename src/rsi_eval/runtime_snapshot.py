"""Explicit task and built-image snapshots. No service or model is invoked here.

The image IDs are recorded while a *new* trial is running. Capture never guesses
which image a historical mutable tag used. Replaying skips materialization and
image builds, preserving downloaded assets and transitive build dependencies.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

from .artifacts import artifact_hash

FORMAT = "rsi-eval-runtime-snapshot-v1"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def tree_receipts(root: Path) -> dict:
    rows = {".": {"directory": True, "mode": root.stat().st_mode & 0o777}}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Runtime snapshot cannot contain a symlink: {path}")
        if path.is_file():
            if path.name in {"auth.json", ".env"}:
                raise ValueError(f"Move credential files outside the task before capture: {path}")
            rows[path.relative_to(root).as_posix()] = {
                "sha256": file_hash(path), "mode": path.stat().st_mode & 0o777}
        elif path.is_dir():
            rows[path.relative_to(root).as_posix()] = {"directory": True, "mode": path.stat().st_mode & 0o777}
        else:
            raise ValueError(f"Unsupported runtime snapshot entry: {path}")
    return rows


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Docker {' '.join(args[:2])} failed: {result.stderr.strip()[-2000:]}")
    return result.stdout


def inspect_image(reference: str) -> dict:
    # Never request image/container environment, which can contain credentials.
    row = json.loads(_docker("image", "inspect", "--format",
        '{"id":{{json .Id}},"os":{{json .Os}},"architecture":{{json .Architecture}},"repo_digests":{{json .RepoDigests}}}', reference))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", row.get("id", "")):
        raise ValueError("Docker returned an invalid image content ID")
    return row


def task_evidence(source: Path) -> dict:
    """Record complete task bytes while the environment is being observed."""
    source = source.resolve()
    receipts = tree_receipts(source)
    content_hash = artifact_hash(source)
    if tree_receipts(source) != receipts:
        raise ValueError("Task changed while recording its runtime identity")
    return {"path": str(source), "sha256": content_hash,
            "tree_sha256": digest(receipts)}


async def record_images(environment) -> None:
    """Called after startup, before the agent can change the running container."""
    result = await environment._run_docker_compose_command(["ps", "--all", "--quiet"])
    ids = result.stdout.split()
    if not ids:
        raise ValueError("No running Compose service image was recorded")
    services = {}
    for container in ids:
        row = json.loads(await asyncio.to_thread(_docker, "inspect", "--format",
            '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"image":{{json .Image}}}', container))
        if not row.get("service"):
            raise ValueError("Compose container has no service identity")
        image = await asyncio.to_thread(inspect_image, row["image"])
        if row["service"] in services and services[row["service"]] != image:
            raise ValueError("Replicas of a service used different images")
        services[row["service"]] = image
        if environment._retain_runtime_images:
            # An explicit run option retains this newly observed image past
            # Harbor's normal `down --rmi local`, enabling later docker save.
            await asyncio.to_thread(_docker, "image", "tag", image["id"],
                                    "rsi-eval-runtime:" + image["id"].removeprefix("sha256:"))
    external = []
    # Runtime bind mounts outside the task or trial are not part of this
    # snapshot. Name them without reading their contents or environment values.
    document = await environment._normalized_compose_config()
    source = Path(environment.context_provenance["source_dir"]).resolve().parent
    trial_root = environment._guard_trial_dir.resolve()
    for name, service in document.get("services", {}).items():
        for mount in service.get("volumes", []):
            if isinstance(mount, dict) and mount.get("type") == "bind":
                origin = Path(mount["source"]).resolve()
                if not origin.is_relative_to(source) and not origin.is_relative_to(trial_root):
                    external.append({"service": name, "kind": "bind", "source": str(origin)})
        for key in ("env_file",):
            for entry in service.get(key, []):
                origin = Path(entry["path"] if isinstance(entry, dict) else entry).resolve()
                if not origin.is_relative_to(source):
                    external.append({"service": name, "kind": key, "source": str(origin)})
    for name, volume in document.get("volumes", {}).items():
        if volume and volume.get("external"):
            external.append({"kind": "external-volume", "source": name})
    try:
        task = await asyncio.to_thread(task_evidence, source)
    except (OSError, ValueError) as error:
        # Unsupported snapshot inputs must not change the ordinary benchmark
        # lifecycle. Capture will fail explicitly if this evidence is needed.
        task = {"path": str(source), "error": str(error)}
    path = environment._guard_trial_dir / "build-context/runtime-images.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": FORMAT, "services": services,
                               "task": task, "external_inputs": external}, indent=2) + "\n")


def _manifest(path: Path) -> Path:
    return path / "runtime-snapshot.json" if path.is_dir() else path


def load_snapshot(path: Path | str, *, spec: dict | None = None, check_images: bool = False,
                  check_archive: bool = False) -> tuple[Path, dict]:
    path = _manifest(Path(path).expanduser().resolve())
    value = json.loads(path.read_text())
    payload = {k: v for k, v in value.items() if k != "sha256"}
    if value.get("format") != FORMAT or value.get("sha256") != digest(payload):
        raise ValueError("Runtime snapshot manifest identity changed")
    root = path.parent
    if check_archive and value.get("image_archive"):
        record = value["image_archive"]
        archive = root / "images.tar"
        if record.get("file") != archive.name or archive.is_symlink() or file_hash(archive) != record.get("sha256"):
            raise ValueError("Saved image archive checksum mismatch")
    tasks = value.get("tasks", [])
    if not tasks or len({t["id"] for t in tasks}) != len(tasks):
        raise ValueError("Runtime snapshot task identities are missing or duplicated")
    for row in tasks:
        task = root / row["directory"]
        if not task.resolve().is_relative_to(root) or task.is_symlink():
            raise ValueError("Runtime snapshot task path escapes its root")
        if tree_receipts(task) != row["files"]:
            raise ValueError(f"Frozen task or asset bytes changed: {row['id']}")
        if not row["services"] or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", image["id"])
                                      for image in row["services"].values()):
            raise ValueError("Runtime snapshot requires content-addressed service images")
        if check_images:
            for image in row["services"].values():
                current = inspect_image(image["id"])
                if any(current[k] != image[k] for k in ("id", "os", "architecture")):
                    raise ValueError("Frozen image identity or platform changed")
    if spec is not None:
        prior = value["benchmark"]
        subset_keys = {"tasks", "task_ids", "selection"}
        if ({k: v for k, v in spec.items() if k not in subset_keys}
                != {k: v for k, v in prior.items() if k not in subset_keys}):
            raise ValueError("Runtime snapshot belongs to another benchmark version")
        native = {t["id"]: t for t in prior["tasks"]}
        available = {t["id"] for t in tasks}
        if any(t["id"] not in available or native.get(t["id"]) != t for t in spec["tasks"]):
            raise ValueError("Selected tasks do not match frozen benchmark task identities")
    return root, value


def preflight(spec: dict, options: dict) -> dict:
    """Keep execution prerequisites, replacing only frozen source requirements."""
    if not spec.get("materializer"):
        return {}
    from .materializers import preflight as native_preflight
    checks = native_preflight(spec, options)
    for key in checks:
        source_only = (spec.get("materializer") == "frontier_eng" and key.endswith(":assets_directory"))
        source_only |= (spec.get("materializer") == "harvey_lab" and key in {"source_directory", "git"})
        if source_only:
            checks[key] = {"ok": True, "detail": "Source files are preserved and verified in the explicit runtime snapshot"}
    return checks


def runtime_settings_match(recorded: dict, requested: dict) -> bool:
    # Frozen task bytes replace these source locations. A judge credential may
    # be rebound without changing the judge model, rubric or scoring settings.
    location_only = {"assets_dir", "source_dir", "dataset_python", "judge_env_file"}
    def settings(value):
        result = {k: v for k, v in value.items() if k not in location_only}
        if "task_overrides" in result:
            result["task_overrides"] = {k: settings(v) for k, v in result["task_overrides"].items()}
        return result
    return settings(recorded) == settings(requested)


def capture(run: Path, output: Path, *, save_images: bool = True) -> dict:
    """Freeze already prepared task bytes and observed service image IDs."""
    from .resume import _read_run, evaluation_lock
    from .evaluation import _read_native_json
    run, output = run.resolve(), output.expanduser().resolve()
    if output.exists() or output.is_relative_to(run):
        raise ValueError("Choose a new snapshot directory outside the evaluation")
    with evaluation_lock(run):
        resolved, results = _read_run(run)
        tasks = resolved.get("execution_tasks")
        native_tasks = not tasks
        expected = {t["id"] for t in resolved["benchmark"]["tasks"]}
        observed_tasks = {}
        service_sets = {}
        for trial in results.trials:
            if not trial.artifact_dir:
                continue
            record = Path(trial.artifact_dir) / "build-context/runtime-images.json"
            if not record.is_file():
                continue
            value = _read_native_json(record)
            if value.get("format") != FORMAT:
                raise ValueError("Unsupported trial image evidence")
            if value.get("external_inputs"):
                raise ValueError("Task uses external bind mounts, env files or volumes that cannot be captured: "
                                 + json.dumps(value["external_inputs"]))
            recorded_task = value.get("task")
            if native_tasks and not recorded_task:
                raise ValueError("Missing contemporaneous task evidence; historical task contents are not reconstructed")
            if recorded_task:
                if (not isinstance(recorded_task, dict) or recorded_task.get("error")
                        or not isinstance(recorded_task.get("path"), str)
                        or not Path(recorded_task["path"]).is_absolute()
                        or any(not isinstance(recorded_task.get(key), str)
                               or not re.fullmatch(r"[0-9a-f]{64}", recorded_task[key])
                               for key in ("sha256", "tree_sha256"))):
                    raise ValueError(f"Invalid contemporaneous task evidence: {trial.task_id}")
                if task_evidence(Path(recorded_task["path"])) != recorded_task:
                    raise ValueError(f"Observed task changed after evaluation: {trial.task_id}")
                prior = observed_tasks.get(trial.task_id)
                if prior and any(prior[key] != recorded_task[key] for key in ("sha256", "tree_sha256")):
                    raise ValueError("Repeated trials used different task contents; capture a consistent single attempt")
                observed_tasks[trial.task_id] = recorded_task
            def identities(services):
                return {name: {key: image[key] for key in ("id", "os", "architecture")}
                        for name, image in services.items()}
            if trial.task_id in service_sets and identities(service_sets[trial.task_id]) != identities(value["services"]):
                raise ValueError("Repeated trials used different images; capture a consistent single attempt")
            service_sets[trial.task_id] = value["services"]
        if set(service_sets) - expected:
            raise ValueError("Runtime image evidence refers to an unselected task")
        if native_tasks:
            missing = sorted(expected - observed_tasks.keys())
            if missing:
                raise ValueError("Missing contemporaneous task evidence: " + ", ".join(missing))
            tasks = [{"id": t["id"], "path": observed_tasks[t["id"]]["path"],
                      "sha256": observed_tasks[t["id"]]["sha256"]}
                     for t in resolved["benchmark"]["tasks"]]
        missing = [t["id"] for t in tasks if t["id"] not in service_sets]
        if missing:
            raise ValueError("Missing contemporaneous image evidence; historical tags are not reconstructed: " + ", ".join(missing))
        output.mkdir(parents=True)
        rows, images = [], {}
        for number, task in enumerate(tasks):
            destination = output / "tasks" / f"{number:06d}"
            source = Path(task["path"])
            receipts = tree_receipts(source)
            recorded_task = observed_tasks.get(task["id"])
            if recorded_task and (digest(receipts) != recorded_task["tree_sha256"]
                                  or artifact_hash(source) != recorded_task["sha256"]):
                raise ValueError("Observed task changed before runtime capture")
            shutil.copytree(source, destination)
            if (tree_receipts(destination) != receipts or tree_receipts(source) != receipts
                    or artifact_hash(destination) != task["sha256"]):
                raise ValueError("Prepared task changed during runtime capture")
            services = service_sets[task["id"]]
            for image in services.values():
                actual = inspect_image(image["id"])
                if any(actual[k] != image[k] for k in ("id", "os", "architecture")):
                    raise ValueError("Observed image is unavailable or has changed")
                images[image["id"]] = image
            rows.append({"id": task["id"], "directory": str(destination.relative_to(output)),
                         "original_path": str(source), "files": receipts, "services": services})
        image_archive = None
        if save_images:
            archive = output / "images.tar"
            _docker("image", "save", "--output", str(archive), *sorted(images))
            image_archive = {"file": archive.name, "sha256": file_hash(archive)}
        value = {"format": FORMAT, "source_run": str(run), "benchmark": resolved["benchmark"],
                 "benchmark_runtime": resolved["options"].get("benchmark_runtime", {}),
                 "tasks": rows, "image_archive": image_archive,
                 "scope": "prepared-task-files-and-all-running-compose-service-images",
                 "model_calls": 0, "judge_calls": 0}
        value["sha256"] = digest(value)
        (output / "runtime-snapshot.json").write_text(json.dumps(value, indent=2) + "\n")
    return {"snapshot": str(output), "sha256": value["sha256"], "tasks": len(rows),
            "images": len(images), "image_archive": image_archive is not None,
            "model_calls": 0, "judge_calls": 0}


def restore_images(path: Path) -> dict:
    root, value = load_snapshot(path)
    record = value.get("image_archive")
    if not record or record["file"] != "images.tar":
        raise ValueError("Snapshot has no saved image archive; keep its original Docker image store")
    archive = root / record["file"]
    if archive.is_symlink() or file_hash(archive) != record["sha256"]:
        raise ValueError("Saved image archive checksum mismatch")
    _docker("image", "load", "--input", str(archive))
    load_snapshot(path, check_images=True)
    return {"snapshot": str(root), "images_restored": True, "model_calls": 0}


def prepare_snapshot(path: str, spec: dict, output: Path) -> list[dict]:
    root, value = load_snapshot(path, spec=spec, check_images=True)
    if output.exists():
        raise ValueError("Frozen task output must be new")
    output.mkdir(parents=True)
    tasks = {t["id"]: t for t in value["tasks"]}
    rows = []
    for number, task in enumerate(spec["tasks"]):
        frozen = tasks[task["id"]]
        destination = output / f"{number:06d}"
        shutil.copytree(root / frozen["directory"], destination)
        if tree_receipts(destination) != frozen["files"]:
            raise ValueError("Frozen task copy changed")
        config = tomllib.loads((destination / "task.toml").read_text())
        declared = config.get("task")
        native_name = declared.get("name") if isinstance(declared, dict) else destination.name
        if not isinstance(native_name, str) or not native_name:
            raise ValueError("Frozen task declares an invalid native task name")
        rows.append({"id": task["id"], "path": str(destination), "sha256": artifact_hash(destination),
                     "native_name": native_name})
    return rows


def add_commands(commands) -> None:
    parser = commands.add_parser("runtime", help="Capture and restore exact built images and task assets")
    actions = parser.add_subparsers(dest="runtime_action", required=True)
    p = actions.add_parser("capture")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--no-save-images", action="store_true", help="Keep image IDs only; requires this Docker image store")
    for name in ("check", "restore"):
        p = actions.add_parser(name)
        p.add_argument("--snapshot", type=Path, required=True)


def dispatch(args) -> int:
    if args.runtime_action == "capture":
        result = capture(args.run, args.output, save_images=not args.no_save_images)
    elif args.runtime_action == "restore":
        result = restore_images(args.snapshot)
    else:
        root, value = load_snapshot(args.snapshot, check_images=True, check_archive=True)
        result = {"snapshot": str(root), "sha256": value["sha256"], "ok": True}
    print(json.dumps(result, indent=2))
    return 0
