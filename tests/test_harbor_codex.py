from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from harbor.models.agent.context import AgentContext
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from rsi_eval.codex import CodexAuthError, CodexError, CodexQuotaError, validate_auth
from rsi_eval.harbor_codex import Agent, load_candidate, mcp_cli_flags
from harbor.models.task.config import MCPServerConfig


def auth(token):
    return {"auth_mode": "chatgpt", "tokens": {"access_token": token, "refresh_token": "r-" + token}}


class FakeEnvironment:
    default_user = "agent"

    def __init__(self, fail_run=False, fail_download=False, events=None, output_prefix=""):
        self.calls = []
        self.fail_run = fail_run
        self.fail_download = fail_download
        self.events = [{"type": "turn.completed"}] if events is None else events
        self.output_prefix = output_prefix

    async def exec(self, command, **kwargs):
        self.calls.append(("exec", command, kwargs))
        code = 1 if self.fail_run and "codex exec " in command else 0
        stdout = (self.output_prefix + "\n".join(map(json.dumps, self.events))) if "codex exec " in command else ""
        return SimpleNamespace(return_code=code, stdout=stdout, stderr="")

    async def upload_file(self, source, target):
        self.calls.append(("upload", str(source), target))

    async def download_file(self, source, target):
        self.calls.append(("download", source, str(target)))
        if self.fail_download:
            raise OSError("transport error")
        Path(target).write_text(json.dumps(auth("fresh")))


class HarborCodexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.auth_dir = self.root / "private"
        self.auth_dir.mkdir()
        (self.auth_dir / "auth.json").write_text(json.dumps(auth("old")))
        self.artifact = self.root / "artifact"
        self.artifact.mkdir()
        (self.artifact / "agent.py").write_text(
            "def build_prompt(instruction, artifact_dir):\n"
            "    return 'CANDIDATE STRATEGY: ' + instruction\n"
            "async def prepare_environment(environment, artifact_dir):\n"
            "    await environment.exec(command='candidate-before')\n"
            "async def after_run(environment, artifact_dir):\n"
            "    await environment.exec(command='candidate-after')\n"
        )
        self.logs = self.root / "logs"
        self.agent = Agent(logs_dir=self.logs, artifact_dir=self.artifact,
                           codex_auth_dir=self.auth_dir, codex_version="1.2.3",
                           model_name="openai/test-model")

    def tearDown(self):
        self.temporary.cleanup()

    async def test_task_mcp_is_explicit_required_and_recorded(self):
        server = MCPServerConfig(name="world", transport="streamable-http", url="http://world:8000/mcp")
        agent = Agent(logs_dir=self.logs, artifact_dir=self.artifact,
                      codex_auth_dir=self.auth_dir, codex_version="1.2.3",
                      model_name="test", mcp_servers=[server], web_search_mode="live")
        env = FakeEnvironment()
        await agent.run("Use world tools", env, AgentContext())
        command = next(row[1] for row in env.calls if row[0] == "exec" and "codex exec " in row[1])
        self.assertIn("--ignore-user-config", command)
        self.assertIn('mcp_servers.world.url="http://world:8000/mcp"', command)
        self.assertIn('mcp_servers.world.required=true', command)
        self.assertIn('mcp_servers.world.default_tools_approval_mode="approve"', command)
        self.assertIn('web_search="live"', command)
        self.assertEqual(json.loads((self.logs / "candidate.json").read_text())["task_mcp_servers"][0]["name"], "world")

    def test_mcp_config_rejects_silent_transport_loss_and_credential_urls(self):
        for server in [
            MCPServerConfig(name="world", transport="sse", url="http://world:8000/sse"),
            MCPServerConfig(name="unsafe.name", transport="stdio", command="python3"),
            MCPServerConfig(name="world", transport="streamable-http", url="http://user:secret@world/mcp"),
            MCPServerConfig(name="world", transport="streamable-http", url="http://world/mcp?key=secret"),
        ]:
            with self.subTest(server=server.name), self.assertRaisesRegex(ValueError, "MCP config"):
                mcp_cli_flags([server])
        server = MCPServerConfig(name="stdio", transport="stdio", command="python3", args=["a b.py", "'quoted'"])
        import shlex
        flags = shlex.split(mcp_cli_flags([server]))
        self.assertIn('mcp_servers.stdio.args=["a b.py", "\'quoted\'"]', flags)
        with self.assertRaisesRegex(ValueError, "unique"):
            mcp_cli_flags([server, server])

    async def test_candidate_is_executed_and_auth_saved_before_cleanup(self):
        env = FakeEnvironment()
        await self.agent.run("task", env, AgentContext())
        commands = [row[1] for row in env.calls if row[0] == "exec"]
        self.assertIn("candidate-before", commands)
        self.assertIn("candidate-after", commands)
        command = next(c for c in commands if "codex exec " in c)
        self.assertIn("CANDIDATE STRATEGY: task", command)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn("--disable multi_agent", command)
        self.assertIn("--ignore-user-config", command)
        self.assertNotIn("--ignore-rules", command)
        self.assertIn("--disable plugins", command)
        self.assertIn('web_search="disabled"', command)
        download = next(i for i, row in enumerate(env.calls) if row[0] == "download")
        cleanup = next(i for i, row in enumerate(env.calls) if "rm -rf /tmp/codex-secrets" in row[1])
        self.assertLess(download, cleanup)
        self.assertEqual(env.calls[download][1], "/tmp/codex-secrets/auth-return.json")
        self.assertTrue(any("cp -L /tmp/codex-home/auth.json" in command for command in commands))
        self.assertEqual(json.loads((self.auth_dir / "auth.json").read_text())["tokens"]["access_token"], "fresh")
        self.assertNotIn("refresh_token", (self.logs / "candidate.json").read_text())

    async def test_failed_codex_still_roundtrips_credentials(self):
        env = FakeEnvironment(fail_run=True)
        with self.assertRaises(RuntimeError):
            await self.agent.run("task", env, AgentContext())
        self.assertEqual(json.loads((self.auth_dir / "auth.json").read_text())["tokens"]["access_token"], "fresh")
        self.assertTrue(any("rm -rf /tmp/codex-secrets" in row[1] for row in env.calls))

    async def test_late_subscription_errors_use_full_structured_output_and_roundtrip_auth(self):
        tool = {"type": "item.completed", "item": {
            "type": "command_execution", "aggregated_output": "ordinary tool output " * 1000,
        }}
        for message, exception in [
            ("You've hit your usage limit. Please try again later.", CodexQuotaError),
            ("Failed to refresh token: authentication failed", CodexAuthError),
        ]:
            for nonzero in (False, True):
                with self.subTest(message=message, nonzero=nonzero):
                    env = FakeEnvironment(fail_run=nonzero, events=[tool, {
                        "type": "turn.failed", "error": {"message": message},
                    }])
                    with self.assertRaises(exception):
                        await self.agent.run("task", env, AgentContext())
                    self.assertEqual(json.loads((self.auth_dir / "auth.json").read_text())["tokens"]["access_token"], "fresh")
                    self.assertEqual(validate_auth(self.auth_dir), self.auth_dir / "auth.json")
                    self.assertTrue(any("rm -rf /tmp/codex-secrets" in row[1] for row in env.calls))

    async def test_tool_text_and_startup_warning_are_not_account_failures(self):
        events = [
            {"type": "item.completed", "item": {"type": "command_execution",
             "aggregated_output": "You've hit your usage limit. HTTP 401 authentication failed. HTTP 429"}},
            {"type": "item.completed", "item": {"type": "error",
             "message": "Under-development features enabled: skip_host_skill_discovery"}},
            {"type": "turn.completed"},
        ]
        await self.agent.run("task about HTTP 401 and quota", FakeEnvironment(events=events), AgentContext())
        with self.assertRaises(NonZeroAgentExitCodeError):
            await self.agent.run("task", FakeEnvironment(events=events, fail_run=True), AgentContext())

    async def test_zero_exit_without_completed_turn_is_not_success(self):
        env = FakeEnvironment(events=[{"type": "thread.started", "thread_id": "fixture"}])
        with self.assertRaisesRegex(CodexError, "without turn.completed"):
            await self.agent.run("task", env, AgentContext())
        self.assertEqual(json.loads((self.auth_dir / "auth.json").read_text())["tokens"]["access_token"], "fresh")

    async def test_pre_json_cli_auth_failure_is_classified(self):
        env = FakeEnvironment(fail_run=True, events=[],
                              output_prefix="Error: authentication failed while refreshing token\n")
        with self.assertRaises(CodexAuthError):
            await self.agent.run("task", env, AgentContext())

    async def test_failed_writeback_blocks_next_trial_and_still_cleans_up(self):
        env = FakeEnvironment(fail_download=True)
        with self.assertRaises(CodexAuthError):
            await self.agent.run("task", env, AgentContext())
        with self.assertRaises(CodexAuthError):
            validate_auth(self.auth_dir)
        self.assertTrue(any("rm -rf /tmp/codex-secrets" in row[1] for row in env.calls))

    def test_native_tokens_trace_and_subscription_cost(self):
        session = self.logs / "sessions" / "2026" / "10" / "10"
        session.mkdir(parents=True)
        events = [
            {"type": "session_meta", "payload": {"id": "test-session"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Done"}]}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 80, "output_tokens": 9, "cached_input_tokens": 12},
                "last_token_usage": {"input_tokens": 80, "output_tokens": 9, "cached_input_tokens": 12},
            }}},
        ]
        (session / "session.jsonl").write_text("\n".join(map(json.dumps, events)))
        context = AgentContext()
        self.agent.populate_context_post_run(context)
        self.assertEqual((context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens), (80, 9, 12))
        self.assertIsNone(context.cost_usd)
        trajectory = json.loads((self.logs / "trajectory.json").read_text())
        self.assertIsNone(trajectory["final_metrics"].get("total_cost_usd"))
        self.assertEqual(context.metadata["artifact_sha256"], self.agent.candidate_hash)

    def test_missing_tokens_stay_unknown(self):
        context = AgentContext()
        self.agent.populate_context_post_run(context)
        self.assertIsNone(context.n_input_tokens)
        self.assertIsNone(context.n_output_tokens)

    def test_multiple_sessions_are_not_silently_miscounted(self):
        sessions = self.logs / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "one.jsonl").write_text("{}\n")
        (sessions / "two.jsonl").write_text("{}\n")
        with self.assertRaisesRegex(RuntimeError, "multiple sessions"):
            self.agent.populate_context_post_run(AgentContext())

    def test_changing_candidate_reloads_actual_source(self):
        (self.artifact / "agent.py").write_text("def build_prompt(instruction, artifact_dir):\n    return 'changed'\n")
        self.assertEqual(load_candidate(self.artifact).build_prompt("x", self.artifact), "changed")

    def test_ignored_mcp_configuration_is_rejected_explicitly(self):
        with self.assertRaisesRegex(ValueError, "MCP config"):
            Agent(logs_dir=self.logs, artifact_dir=self.artifact,
                  codex_auth_dir=self.auth_dir, codex_version="1.2.3",
                  model_name="test", mcp_servers=[object()])


if __name__ == "__main__":
    unittest.main()
