"""Opt-in real Docker regression, no model calls; removes its own resources."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import uuid

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from rsi_eval.harbor_docker import DockerEnvironment


async def validate(output: Path, base_image: str):
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for index, marker in enumerate(("alpha-bytes", "bravo-bytes")):
        root = output / f"case-{index}"
        source = root / "task/environment"
        worker = source / "worker"
        worker.mkdir(parents=True)
        extra = root / "declared-extra"
        extra.mkdir()
        (worker / ".hidden").write_text(marker)
        (extra / "more").write_text(marker)
        for path in (worker / ".hidden", extra / "more"):
            os.utime(path, ns=(0, 0))
        (source / "runtime").mkdir()
        (source / "runtime/value").write_text("bound-original-runtime")
        (source / "runtime.env").write_text("FROM_FILE=loaded-env-file\n")
        external = root / "build.Dockerfile"
        external.write_text(f"FROM {base_image}\nCOPY .hidden /identity\nCOPY --from=assets more /additional\n")
        project = "rsi-compose-check-" + uuid.uuid4().hex[:10]
        image = project + ":validation"
        (source / "docker-compose.yaml").write_text(json.dumps({"version": "3.8", "services": {"main": {
            "image": image, "build": {"context": "./worker", "dockerfile": "../../../build.Dockerfile",
                                      "additional_contexts": {"assets": "../../declared-extra"}},
            "env_file": ["./runtime.env"], "environment": {"MERGED": "base", "LITERAL": "$$unchanged"},
            "volumes": ["./runtime:/runtime:ro"], "command": ["sleep", "infinity"]}}}))
        overlay = root / "overlay.json"
        overlay.write_text(json.dumps({"services": {"main": {"environment": {"MERGED": "overlay"}}}}))
        environment = DockerEnvironment(source, "compose-regression", project, TrialPaths(root / "trial"),
                                        EnvironmentConfig(), extra_docker_compose=[str(overlay)])
        try:
            await environment._run_docker_compose_command(["build"], timeout_sec=600)
            await environment._run_docker_compose_command(["up", "--detach", "--wait"], timeout_sec=120)
            probe = await environment._run_docker_compose_command(["exec", "-T", "main", "python", "-c",
                "import json,os,pathlib;print(json.dumps({'identity':pathlib.Path('/identity').read_text(),"
                "'additional':pathlib.Path('/additional').read_text(),'runtime':pathlib.Path('/runtime/value').read_text(),"
                "'env_file':os.environ['FROM_FILE'],'merged':os.environ['MERGED'],'literal':os.environ['LITERAL']}))"], timeout_sec=30)
            actual = json.loads(probe.stdout)
            assert actual == {"identity": marker, "additional": marker, "runtime": "bound-original-runtime",
                              "env_file": "loaded-env-file", "merged": "overlay", "literal": "$unchanged"}, actual
            rows.append({"marker": marker, "actual": actual, "provenance": environment.context_provenance})
        finally:
            await environment._run_docker_compose_command(["down", "--volumes", "--remove-orphans"], check=False, timeout_sec=60)
            subprocess.run(["docker", "image", "rm", image], capture_output=True, check=False)
    context_paths = [next(r["isolated_dir"] for r in row["provenance"]["contexts"] if r["role"] == "context") for row in rows]
    assert Path(context_paths[0]).name != Path(context_paths[1]).name
    report = {"status": "passed", "model_calls": 0, "cases": rows,
              "checks": ["same-size/same-mtime hidden bytes", "external Dockerfile", "additional context",
                         "extra Compose override", "original runtime bind", "env_file", "literal dollar", "cleanup"]}
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "output": str(output), "cases": len(rows)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-image", required=True)
    args = parser.parse_args()
    asyncio.run(validate(args.output.resolve(), args.base_image))
