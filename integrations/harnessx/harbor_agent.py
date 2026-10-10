"""Native HarnessX loop with all sandbox operations delegated to Harbor."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from harbor.agents.base import BaseAgent
from rsi_eval.artifacts import artifact_hash
from rsi_eval.codex import CodexSettings

from . import UPSTREAM_COMMIT
from .bundle import load_config
from .provider import CodexProvider
from .task_mcp import TaskMCPBridge, server_specs


def sandbox_provider(environment, cwd):
    from harnessx.sandbox.base import Sandbox, SandboxProvider

    class HarborSandbox(Sandbox):
        @property
        def workspace_path(self):
            return cwd

        async def exec(self, command, cwd=None, timeout=30.0):
            result = await environment.exec(command=command, cwd=cwd or self.workspace_path,
                                            timeout_sec=timeout)
            text = result.stdout or ""
            if result.stderr:
                text += "\nSTDERR: " + result.stderr
            if result.return_code:
                text += f"\n[exit status: {result.return_code}]"
            return text

    class HarborProvider(SandboxProvider):
        async def acquire(self, hint_id=None, workspace=None):
            return HarborSandbox()

        async def release(self, sandbox):
            pass

    return HarborProvider()


class Agent(BaseAgent):
    def __init__(self, logs_dir, artifact_dir, harnessx_checkout, codex_auth_dir,
                 codex_version, codex_executable="codex", max_steps=100,
                 reasoning_effort="medium", model_timeout=600, expected_candidate_hash=None, **kwargs):
        super().__init__(logs_dir=Path(logs_dir), **kwargs)
        if not self.model_name:
            raise ValueError("HarnessX Codex model must be explicit")
        self.artifact_dir = Path(artifact_dir).resolve()
        self.candidate_hash = artifact_hash(self.artifact_dir)
        if expected_candidate_hash and self.candidate_hash != expected_candidate_hash:
            raise ValueError("Candidate snapshot does not match the SDK target hash")
        self.checkout = Path(harnessx_checkout).resolve()
        from .prepare import check_checkout
        check_checkout(self.checkout)
        sys.path.insert(0, str(self.checkout))
        self.settings = CodexSettings(model=self.model_name, auth_dir=Path(codex_auth_dir),
                                      executable=codex_executable, expected_version=codex_version,
                                      reasoning_effort=reasoning_effort, timeout_seconds=model_timeout)
        if self.settings.auth_dir == self.artifact_dir or self.artifact_dir in self.settings.auth_dir.parents:
            raise ValueError("Codex auth must remain outside candidate artifacts")
        self.max_steps = int(max_steps)
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.provider = CodexProvider(self.settings, self.logs_dir / "codex")

    @staticmethod
    def name():
        return "harnessx-codex"

    def version(self):
        return UPSTREAM_COMMIT

    async def setup(self, environment):
        self.verify_candidate()
        server_specs(self.mcp_servers)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        result = await environment.exec(command="pwd", timeout_sec=30)
        if result.return_code or not result.stdout.strip().startswith("/"):
            raise RuntimeError("Cannot determine Harbor task working directory")
        self.cwd = result.stdout.strip()

    def verify_candidate(self):
        if artifact_hash(self.artifact_dir) != self.candidate_hash:
            raise RuntimeError("Candidate snapshot changed during execution")

    def populate_context_post_run(self, context):
        usage = self.provider.accounting()
        context.n_input_tokens = usage["input_tokens"]
        context.n_output_tokens = usage["output_tokens"]
        context.n_cache_tokens = usage["cached_input_tokens"]
        context.cost_usd = None
        context.metadata = {**(context.metadata or {}), "harnessx_usage": usage,
                            "candidate_hash": self.candidate_hash,
                            "candidate_unchanged": artifact_hash(self.artifact_dir) == self.candidate_hash,
                            "harnessx_commit": UPSTREAM_COMMIT}
        (self.logs_dir / "usage.json").write_text(json.dumps(usage, indent=2) + "\n")

    async def run(self, instruction, environment, context):
        from harnessx import BaseTask, ModelConfig
        from harnessx.tracing.journal import HarnessJournal
        os.environ["HARNESSX_HOME"] = str(self.logs_dir / "harnessx-home")
        self.verify_candidate()
        config = load_config(self.artifact_dir, checkout=self.checkout)
        config = config.copy(sandbox_provider=sandbox_provider(environment, self.cwd),
                             tracer=HarnessJournal(base_dir=str(self.logs_dir / "native"), silent=True),
                             workspace=None, init_workspace=False)
        bridge = TaskMCPBridge(environment, self.mcp_servers, self.logs_dir / "task-mcp")
        try:
            task = BaseTask(
                description=instruction, max_steps=self.max_steps, max_cost_usd=None,
            )
            harness = ModelConfig(main=self.provider).agentic(config)
            # Mount task tools into the native runtime, outside evolvable YAML.
            # The pinned runtime retains all processors, gates and tool tracing.
            await bridge.start(harness._rt.tool_registry)
            result = await harness.run(task)
            (self.logs_dir / "trajectory.md").write_text(result.trajectory.to_markdown(
                task=task, result=result, config=config))
            if self.provider.failure:
                raise self.provider.failure
            (self.logs_dir / "harnessx-result.json").write_text(json.dumps({
                "exit_reason": result.exit_reason, "final_output": result.final_output,
                "candidate_hash": self.candidate_hash, "cost_usd": None,
                "upstream_cost_estimate_is_billing": False,
            }, indent=2) + "\n")
            if result.exit_reason == "error":
                raise RuntimeError("Native HarnessX run loop failed; inspect native trajectory")
        finally:
            try:
                await bridge.close()
            finally:
                self.populate_context_post_run(context)
                self.verify_candidate()
