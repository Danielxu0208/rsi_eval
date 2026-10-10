"""Validate the installed core and four managed adapters outside the checkout.

Uses existing, independently audited method dependency environments. This is
separate from audit_method_dependencies.py, which proves fresh installation.
No command here runs inference or a benchmark verifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--environments", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit = args.output.resolve()
    audit.mkdir(parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix="rsi-installed-final-"))
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", LITELLM_LOCAL_MODEL_COST_MAP="True")
    rows = {}
    def command(name, command, *, parse=True):
        proc = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
        (audit / f"{name}.stdout").write_text(proc.stdout)
        (audit / f"{name}.stderr").write_text(proc.stderr)
        rows[name] = {"returncode": proc.returncode, "command": command,
            "stdout_sha256": hashlib.sha256(proc.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(proc.stderr.encode()).hexdigest()}
        (audit / "progress.json").write_text(json.dumps(rows, indent=2))
        if proc.returncode:
            raise RuntimeError(f"Installed check {name} failed; inspect {audit / (name + '.stderr')}")
        return json.loads(proc.stdout) if parse else proc.stdout
    command("venv", [sys.executable, "-m", "venv", str(root / "core")], parse=False)
    python = str(root / "core/bin/python")
    command("install", [python, "-m", "pip", "install", "--no-index", "--no-deps", str(args.wheel.resolve())], parse=False)
    cli = str(root / "core/bin/rsi-eval")
    inventory = command("inventory", [python, "-I", "-c", "import importlib.metadata as m,json,rsi_eval; print(json.dumps({'location':rsi_eval.__file__,'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))"])
    assert set(inventory["packages"]) <= {"rsi-eval", "pip", "setuptools"}
    assert "site-packages" in inventory["location"]
    assert len(command("list", [cli, "list", "--json"])) == 26
    methods = command("methods", [cli, "method", "list"])
    assert set(methods) == {"meta-harness", "rrsi", "ahe", "harnessx"}
    for action in (("method", "test"), ("experiment", "import"), ("runtime", "capture")):
        command("help-" + "-".join(action), [cli, *action, "--help"], parse=False)
    resources = command("resources", [python, "-I", "-c", "from importlib.resources import files; from rsi_eval.resources import codex_template; import json; print(json.dumps({'template':str(codex_template()),'locks':[p.name for p in files('integrations').joinpath('locks/macos-arm64-py312').iterdir()]}))"])
    assert len(resources["locks"]) == 4
    profile = json.loads(args.profile.read_text())["codex"]
    config = {"target": {"artifact_dir": resources["template"], "entrypoint": "oracle"},
              "benchmark": {"name": "terminal-bench", "version": "2.0", "task_ids": ["extract-elf"]},
              "output_dir": str(root / "preview")}
    config_path = root / "preview.json"
    config_path.write_text(json.dumps(config))
    command("preview", [cli, "preview", "--config", str(config_path)])
    environments = json.loads(args.environments.read_text())
    sources = json.loads(args.sources.read_text())
    for method in methods:
        experiment = root / method
        command(f"prepare-{method}", [cli, "method", "prepare", "--method", method,
                "--profile", str(args.profile.resolve()), "--experiment", str(experiment),
                "--source", sources[method], "--environment", environments[method], "--no-install"])
        command(f"check-{method}", [cli, "method", "check", "--experiment", str(experiment)], parse=False)
        bundle = root / f"{method}-bundle"
        command(f"export-{method}", [cli, "experiment", "export", "--experiment", str(experiment), "--output", str(bundle)])
        moved = root / f"{method}-moved"
        import_command = [cli, "experiment", "import", "--bundle", str(bundle),
                "--experiment", str(moved), "--environment", environments[method],
                "--auth-dir", profile["auth_dir"]]
        if Path(profile.get("executable", "codex")).is_absolute():
            import_command += ["--codex", profile["executable"]]
        command(f"import-{method}", import_command)
        experiment.rename(root / f"{method}-original-archived")
        command(f"check-moved-{method}", [cli, "method", "check", "--experiment", str(moved)], parse=False)
    with zipfile.ZipFile(args.wheel) as wheel:
        assert not any(p.endswith("/auth.json") or "/.runtime/" in p for p in wheel.namelist())
    result = {"status": "passed", "root": str(root), "wheel": str(args.wheel.resolve()),
              "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(), "inventory": inventory,
              "commands": rows, "model_calls": 0, "judge_calls": 0,
              "method_dependencies": "reused independently audited fresh environments"}
    (audit / "validation.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "commands"}, indent=2))


if __name__ == "__main__":
    main()
