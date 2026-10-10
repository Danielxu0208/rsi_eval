"""Exercise the actual pinned Harbor config/factory at our public boundary."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from harbor.agents.factory import AgentFactory
from harbor.models.job.config import JobConfig

from rsi_eval.artifacts import artifact_hash, snapshot
from rsi_eval.evaluation import _job_config, preview
from rsi_eval.harbor_codex import Agent
from rsi_eval.types import BenchmarkSelection, RunOptions, TargetSpec


class RuntimeContractTests(unittest.TestCase):
    def test_sdk_snapshot_builds_real_harbor_config_and_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            artifact = root / "candidate"
            seed = Path(__file__).parents[1] / "templates" / "codex_agent"
            shutil.copytree(seed, artifact)
            (artifact / "prompt.md").write_text("CUSTOM STRATEGY\n{instruction}")
            private = root / "private"
            private.mkdir()
            (private / "auth.json").write_text(json.dumps({
                "auth_mode": "chatgpt", "tokens": {"access_token": "test", "refresh_token": "test"},
            }))
            target = TargetSpec(artifact, "rsi_eval.harbor_codex:Agent", {
                "codex_auth_dir": str(private), "codex_version": "0.162.0-alpha.2",
                "reasoning_effort": "medium",
            }, model="gpt-6-astra")
            resolved = preview(target=target,
                               benchmark=BenchmarkSelection("terminal-bench", "2.0", ("extract-elf",)),
                               options=RunOptions(), output_dir=root / "output")
            copied = root / "snapshot"
            snapshot(artifact, copied)
            config = JobConfig.model_validate(_job_config(resolved, copied))
            runtime = AgentFactory.create_agent_from_config(config.agents[0], root / "logs")
            self.assertIsInstance(runtime, Agent)
            self.assertEqual(runtime.artifact_dir, copied)
            self.assertEqual(runtime.version(), "0.162.0-alpha.2")
            self.assertEqual(runtime.model_name, "gpt-6-astra")
            self.assertEqual(runtime.candidate_hash, resolved["target"]["sha256"])
            self.assertEqual(runtime.candidate.build_prompt("native task", copied),
                             "CUSTOM STRATEGY\nnative task")
            (artifact / "prompt.md").write_text("edited after snapshot")
            self.assertNotEqual(artifact_hash(artifact), runtime.candidate_hash)
            self.assertEqual(runtime.candidate.build_prompt("native task", copied),
                             "CUSTOM STRATEGY\nnative task")


if __name__ == "__main__":
    unittest.main()
