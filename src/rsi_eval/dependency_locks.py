"""Explicit, platform-scoped version locks for optional method environments.

These locks freeze resolved package versions, not operating-system libraries or
wheel bytes. The pip installation report separately records downloaded artifacts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def inspect_environment(python: Path) -> dict:
    code = """import importlib.metadata as m,json,platform,re
print(json.dumps({'python':platform.python_version(), 'implementation':platform.python_implementation(),
 'system':platform.system(), 'machine':platform.machine(),
 'packages':{re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in m.distributions()}}))
"""
    # Exclude caller PYTHONPATH and the working directory: a source checkout's
    # egg-info is not an installed dependency of this isolated environment.
    result = subprocess.run([str(python), "-I", "-c", code], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def create_lock(python: Path, *, method: str, destination: Path, requirements: dict[str, str] | None = None) -> dict:
    """Record a real environment after a successful install and pip check."""
    subprocess.run([str(python), "-m", "pip", "check"], check=True, capture_output=True, text=True)
    environment = inspect_environment(python)
    if environment["packages"].get("harbor") != "0.18.0":
        raise ValueError("A method dependency lock requires Harbor 0.18.0")
    payload = {"schema_version": 1, "method": method,
               "scope": "host-python-package-versions", "environment": environment,
               "requirements_sha256": requirements or {},
               "requirements": [f"{name}=={version}" for name, version in sorted(environment["packages"].items())]}
    value = {**payload, "sha256": _digest(payload)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2) + "\n")
    return value


def load_lock(path: Path, *, method: str) -> dict:
    value = json.loads(Path(path).read_text())
    payload = {k: v for k, v in value.items() if k != "sha256"}
    if value.get("schema_version") != 1 or value.get("sha256") != _digest(payload):
        raise ValueError("Dependency lock content hash or schema mismatch")
    if value.get("method") != method or value.get("scope") != "host-python-package-versions":
        raise ValueError("Dependency lock belongs to another method or scope")
    requirements = value.get("requirements", [])
    pattern = r"[a-z0-9][a-z0-9-]*==[A-Za-z0-9][A-Za-z0-9.!+_-]*"
    if not requirements or any(not isinstance(r, str) or re.fullmatch(pattern, r) is None for r in requirements):
        raise ValueError("Dependency lock must contain exact package versions, without URLs or pip options")
    if requirements != [f"{n}=={v}" for n, v in sorted(value["environment"]["packages"].items())]:
        raise ValueError("Dependency lock requirements do not match its environment")
    if value["environment"]["packages"].get("harbor") != "0.18.0":
        raise ValueError("Dependency lock must pin Harbor 0.18.0")
    return value


def validate_platform(lock: dict, python: Path) -> None:
    current = inspect_environment(python)
    expected = lock["environment"]
    if any(current[k] != expected[k] for k in ("system", "machine", "implementation")) or current["python"].split(".")[:2] != expected["python"].split(".")[:2]:
        raise ValueError("Dependency lock platform or Python minor version differs; resolve and record a lock on this host")


def verify_environment(lock: dict, python: Path) -> None:
    validate_platform(lock, python)
    current = inspect_environment(python)["packages"]
    if current != lock["environment"]["packages"]:
        raise ValueError("Installed method packages differ from the selected dependency lock")
