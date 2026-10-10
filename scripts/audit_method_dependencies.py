#!/usr/bin/env python3
"""Create fresh method environments, prepare/check them, and archive version locks.

No optimization, target model, or judge requests are made. Package download
caches may be reused; Python environments and native integration checkouts may not.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import traceback

from rsi_eval.dependency_locks import create_lock, load_lock, verify_environment
from rsi_eval.methods import METHODS, _environment, method_command, prepare_method


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True, help="JSON mapping method names to pinned local source clones")
    parser.add_argument("--lock-output", type=Path, required=True)
    parser.add_argument("--dependency-locks", type=Path, help="Replay existing locks into new environments")
    args = parser.parse_args()
    root = args.output.resolve()
    if root.exists():
        parser.error("Audit output must not exist: each audit creates genuinely fresh environments")
    root.mkdir(parents=True)
    sources = json.loads(args.sources.read_text())
    locks = args.lock_output.resolve()

    def one(method):
        experiment = root / method
        evidence = {"method": method, "fresh_environment": not experiment.exists(),
                    "download_cache_reuse_allowed": True, "model_calls": 0,
                    "requested": ["harbor==0.18.0", f"integrations/{METHODS[method]['module']}/requirements.txt if present"]}
        try:
            record = prepare_method(method=method, experiment=experiment,
                                    profile_file=args.profile.resolve(), source=sources[method],
                                    dependency_lock=args.dependency_locks / f"{method}.json" if args.dependency_locks else None)
            python = experiment / "environment/bin/python"
            requirements = experiment / "runtime/integrations" / METHODS[method]["module"] / "requirements.txt"
            hashes = {str(requirements.relative_to(experiment / "runtime")): hashlib.sha256(requirements.read_bytes()).hexdigest()} if requirements.exists() else {}
            lock = create_lock(python, method=method, destination=locks / f"{method}.json", requirements=hashes)
            evidence.update(prepare_status=record["status"], dependency_lock=str(locks / f"{method}.json"),
                            dependency_lock_sha256=lock["sha256"], environment=lock["environment"],
                            install_report_sha256=hashlib.sha256((experiment / "dependency-install-report.json").read_bytes()).hexdigest())
            if args.dependency_locks:
                original = load_lock(args.dependency_locks / f"{method}.json", method=method)
                verify_environment(original, python)
                evidence["replayed_lock_sha256"] = original["sha256"]
            # Exercise native imports in addition to the public prerequisite
            # command. Meta defers export until run; export an audit copy here.
            probes = {
                "meta-harness": "from integrations.meta_harness.launcher import prepare_checkout,load_upstream; from pathlib import Path; p=prepare_checkout(Path('upstream-source'),Path('native-import-checkout')); m=load_upstream(p); assert callable(m.run_evolve)",
                "rrsi": "import sys; sys.path.insert(0,'checkout'); import rrsi.loop; from rrsi.domain import load_domain; assert load_domain('coding_codex') is not None",
                "ahe": "from integrations.ahe.launcher import load_upstream; from pathlib import Path; m=load_upstream(Path('checkout')); assert callable(m.run_single_experiment)",
                "harnessx": "from integrations.harnessx.launcher import load_experiment,runtime_environment; from pathlib import Path; c=load_experiment(Path('configuration.json')); runtime_environment(c); from recipe.tb2_evolver.run import main; assert callable(main)",
            }
            imported = subprocess.run([str(python), "-c", probes[method]], cwd=experiment,
                                      env=_environment(record), capture_output=True, text=True)
            (experiment / "native-import.stdout").write_text(imported.stdout)
            (experiment / "native-import.stderr").write_text(imported.stderr)
            evidence["native_import_returncode"] = imported.returncode
            command = method_command(record, "check")
            completed = subprocess.run(command, cwd=experiment, env=_environment(record), capture_output=True, text=True)
            (experiment / "native-check.stdout").write_text(completed.stdout)
            (experiment / "native-check.stderr").write_text(completed.stderr)
            evidence.update(check_command=command, check_returncode=completed.returncode,
                            status="passed" if completed.returncode == imported.returncode == 0 else "check_failed")
        except Exception as error:
            evidence.update(status="failed", error=f"{type(error).__name__}: {error}")
            (root / f"{method}-error.txt").write_text(traceback.format_exc())
        (root / f"{method}-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"{method}: {evidence['status']}", flush=True)
        return evidence

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(one, METHODS))
    (root / "summary.json").write_text(json.dumps({"model_calls": 0, "methods": records}, indent=2) + "\n")
    return int(any(record["status"] != "passed" for record in records))


if __name__ == "__main__":
    sys.exit(main())
