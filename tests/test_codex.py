from __future__ import annotations

import json
import base64
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rsi_eval.codex import (
    CodexAuthError, CodexError, CodexQuotaError, CodexSettings, RoleToolUseError, _cli_env,
    _command, auth_lock, mark_auth_uncertain, persist_auth_file, run_codex,
    validate_auth,
)


def managed_auth(token="old"):
    return {"auth_mode": "chatgpt", "tokens": {
        "access_token": token, "refresh_token": "refresh-" + token,
    }}


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.auth = self.root / "private"
        self.auth.mkdir()
        (self.auth / "auth.json").write_text(json.dumps(managed_auth()))
        self.workspace = self.root / "candidate"
        self.workspace.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def executable(self, events, *, trailer="", exit_code=0):
        path = self.root / "fake-codex"
        path.write_text(
            f"#!{sys.executable}\nimport sys,json,os,time\n"
            "if '--version' in sys.argv:\n print('codex-cli 1.2.3'); sys.exit(0)\n"
            "sys.stdin.read()\n"
            f"events={events!r}\n"
            "for event in events: print(json.dumps(event),flush=True)\n"
            + trailer + f"\nsys.exit({exit_code})\n"
        )
        path.chmod(0o700)
        return str(path)

    def settings(self, executable):
        return CodexSettings("test-model", self.auth, executable, "1.2.3", timeout_seconds=3)

    @staticmethod
    def successful_events(usage=None):
        return [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"action":"done"}'}},
            {"type": "turn.completed", "usage": usage or {"input_tokens": 40, "cached_input_tokens": 10, "output_tokens": 7}},
        ]

    def test_real_subprocess_contract_and_private_login(self):
        exe = self.executable(self.successful_events())
        result = run_codex("return an action", settings=self.settings(exe),
                           cwd=self.workspace, json_only=True, log_dir=self.root / "logs")
        self.assertEqual(json.loads(result.text), {"action": "done"})
        self.assertEqual(result.usage["input_tokens"], 40)
        self.assertEqual(result.usage["cached_input_tokens"], 10)
        self.assertEqual(result.cli_version, "1.2.3")
        self.assertEqual(result.session_id, "thread-1")
        self.assertEqual(len((self.root / "logs" / "events.jsonl").read_text().splitlines()), 3)
        record = json.loads((self.root / "logs" / "result.json").read_text())
        self.assertEqual(record["codex_cli_calls"], 1)
        self.assertEqual(result.codex_cli_calls, 1)
        self.assertGreater(record["elapsed_seconds"], 0)
        self.assertEqual(record["elapsed_seconds"], result.elapsed_seconds)

    def test_role_disables_tools_and_rejects_events_before_more_work(self):
        sentinel = self.root / "should-not-exist"
        exe = self.executable([
            {"type": "item.started", "item": {"type": "command_execution", "command": "cat secret"}}
        ], trailer=f"time.sleep(1)\nopen({str(sentinel)!r},'w').write('bad')")
        with self.assertRaises(RoleToolUseError):
            run_codex("role", settings=self.settings(exe), cwd=self.workspace)
        self.assertFalse(sentinel.exists())
        cmd = _command(self.settings(exe), "role", self.workspace, None)
        self.assertIn("shell_tool", cmd)
        self.assertIn("plugins", cmd)
        self.assertIn("read-only", cmd)

    def test_role_does_not_inherit_project_directory(self):
        exe = self.executable([], trailer=(
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message',"
            "'text':os.getcwd()}}),flush=True)\n"
            "print(json.dumps({'type':'turn.completed'}),flush=True)"
        ))
        result = run_codex("role", settings=self.settings(exe), cwd=self.workspace)
        self.assertNotEqual(Path(result.text), self.workspace)
        self.assertIn("rsi-codex-role-", result.text)
        self.assertIsNone(result.usage["input_tokens"])

    def test_images_reach_cli_in_order_and_retain_exact_bytes(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        original = self.workspace / "first, image.png"
        original.write_bytes(png)
        other = self.workspace / "second.png"
        other.write_bytes(png + b"second-input")
        exe = self.executable([], trailer=(
            "import pathlib,hashlib\n"
            "images=[pathlib.Path(sys.argv[i+1]) for i,x in enumerate(sys.argv) if x=='--image']\n"
            "assert sys.argv[-2:]==['--','-']\n"
            "assert all(p.parent==pathlib.Path.cwd() and ',' not in str(p) for p in images)\n"
            "reply=[hashlib.sha256(p.read_bytes()).hexdigest() for p in images]\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':5,'output_tokens':2}}))\n"
        ))
        logs = self.root / "image-logs"
        result = run_codex("inspect both", settings=self.settings(exe), cwd=self.workspace,
                           images=[Path(original.name), other], log_dir=logs, json_only=True)
        expected = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, other)]
        self.assertEqual(json.loads(result.text), expected)
        records = json.loads((logs / "images.json").read_text())
        self.assertEqual([r["sha256"] for r in records], expected)
        self.assertEqual([r["index"] for r in records], [1, 2])
        self.assertEqual([r["mime_type"] for r in records], ["image/png", "image/png"])
        for source, record in zip((original, other), records):
            self.assertEqual((logs / record["path"]).read_bytes(), source.read_bytes())
        self.assertEqual(json.loads((logs / "result.json").read_text())["images"], records)

    def test_invalid_images_fail_before_model_execution(self):
        exe = self.executable(self.successful_events())
        plain = self.workspace / "not-image.png"
        plain.write_text("this is not an image")
        link = self.workspace / "link.png"
        link.symlink_to(plain)
        for index, path in enumerate((plain, link, self.workspace / "missing.png", self.auth / "auth.json")):
            with self.subTest(path=path.name):
                logs = self.root / f"invalid-image-{index}"
                with self.assertRaises(ValueError):
                    run_codex("inspect", settings=self.settings(exe), cwd=self.workspace,
                              images=[path], log_dir=logs)
                record = json.loads((logs / "result.json").read_text())
                self.assertEqual(record["codex_cli_calls"], 0)
                self.assertFalse((logs / "events.jsonl").exists())

    def test_cli_diagnostic_error_item_is_not_a_tool_call(self):
        warning = {"type": "item.completed", "item": {
            "type": "error", "message": "Under-development features enabled: skip_host_skill_discovery",
        }}
        exe = self.executable([warning, *self.successful_events()])
        result = run_codex("role", settings=self.settings(exe), cwd=self.workspace)
        self.assertEqual(json.loads(result.text), {"action": "done"})
        self.assertEqual(result.events[0], warning)

    def test_edit_keeps_candidate_cwd_and_failures_do_not_fallback(self):
        exe = self.executable(self.successful_events(), exit_code=9)
        with self.assertRaises(CodexError):
            run_codex("edit", settings=self.settings(exe), cwd=self.workspace, mode="edit")
        cmd = _command(self.settings(exe), "edit", self.workspace, None)
        self.assertIn("workspace-write", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        disabled = {cmd[index + 1] for index, value in enumerate(cmd) if value == "--disable"}
        self.assertTrue({"apps", "plugins", "hooks"} <= disabled)
        self.assertNotIn("shell_tool", disabled)
        self.assertNotIn("unified_exec", disabled)
        self.assertIn("skip_host_skill_discovery", cmd)
        self.assertNotIn("project_doc_max_bytes=0", cmd)

    def test_api_credentials_and_provider_overrides_are_not_inherited(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "CODEX_API_KEY": "secret",
                                     "OPENAI_BASE_URL": "https://invalid", "CODEX_ACCESS_TOKEN": "secret"}):
            env = _cli_env(self.settings("codex"))
        self.assertEqual(env["CODEX_HOME"], str(self.auth))
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertNotIn("CODEX_ACCESS_TOKEN", env)
        self.assertNotIn("OPENAI_BASE_URL", env)

    def test_auth_roundtrip_and_relogin_recovery(self):
        mark_auth_uncertain(self.auth)
        with self.assertRaises(CodexAuthError):
            validate_auth(self.auth)
        downloaded = self.auth / ".new-auth"
        downloaded.write_text(json.dumps(managed_auth("refreshed")))
        persist_auth_file(downloaded, self.auth)
        self.assertEqual(validate_auth(self.auth), self.auth / "auth.json")
        self.assertEqual((self.auth / "auth.json").stat().st_mode & 0o777, 0o600)

    def test_auth_writeback_never_publishes_a_container_symlink(self):
        downloaded = self.auth / ".returned-auth"
        downloaded.symlink_to(self.auth / "auth.json")
        with self.assertRaisesRegex(CodexAuthError, "regular file"):
            persist_auth_file(downloaded, self.auth)

    def test_auth_lock_serializes_cli_and_trial_users(self):
        ready = threading.Event()
        acquired = threading.Event()

        def contender():
            ready.set()
            with auth_lock(self.auth, 2):
                acquired.set()

        with auth_lock(self.auth):
            thread = threading.Thread(target=contender)
            thread.start()
            ready.wait(1)
            time.sleep(0.1)
            self.assertFalse(acquired.is_set())
        thread.join(2)
        self.assertTrue(acquired.is_set())

    def test_version_pin_and_json_response_are_checked(self):
        exe = self.executable(self.successful_events())
        with self.assertRaisesRegex(CodexError, "version mismatch"):
            run_codex("x", settings=CodexSettings("test", self.auth, exe, "0.0"), cwd=self.workspace)
        events = self.successful_events()
        events[1]["item"]["text"] = "not json"
        exe = self.executable(events)
        with self.assertRaisesRegex(CodexError, "not valid JSON"):
            run_codex("x", settings=self.settings(exe), cwd=self.workspace, json_only=True)

    def test_quota_failure_has_diagnostic_artifacts_and_no_fallback(self):
        exe = self.executable([{"type": "turn.failed", "error": {"message": "Usage limit reached"},
                                "usage": {"input_tokens": 20, "output_tokens": 3}}], exit_code=1)
        logs = self.root / "failure-logs"
        with self.assertRaises(CodexQuotaError):
            run_codex("x", settings=self.settings(exe), cwd=self.workspace, log_dir=logs)
        record = json.loads((logs / "result.json").read_text())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["codex_cli_calls"], 1)
        self.assertGreater(record["elapsed_seconds"], 0)
        self.assertEqual(record["usage"]["input_tokens"], 20)
        self.assertEqual(record["usage"]["output_tokens"], 3)
        self.assertIsNone(record["usage"]["cached_input_tokens"])
        self.assertIn("Usage limit", (logs / "events.jsonl").read_text())

    def test_preflight_failure_records_zero_exec_calls_without_reusing_old_usage(self):
        exe = self.executable(self.successful_events())
        logs = self.root / "preflight"
        logs.mkdir()
        (logs / "events.jsonl").write_text("\n".join(map(json.dumps, self.successful_events())))
        with self.assertRaisesRegex(CodexError, "version mismatch"):
            run_codex("x", settings=CodexSettings("test", self.auth, exe, "0.0"),
                      cwd=self.workspace, mode="edit", log_dir=logs)
        record = json.loads((logs / "result.json").read_text())
        self.assertEqual(record["codex_cli_calls"], 0)
        self.assertGreater(record["elapsed_seconds"], 0)
        self.assertIsNone(record["usage"]["input_tokens"])
        self.assertIsNone(record["session_id"])

    def test_tool_rejection_records_failed_cli_attempt(self):
        exe = self.executable([{"type": "item.started", "item": {"type": "command_execution"}}])
        logs = self.root / "tool-rejection"
        with self.assertRaises(RoleToolUseError):
            run_codex("x", settings=self.settings(exe), cwd=self.workspace, log_dir=logs)
        record = json.loads((logs / "result.json").read_text())
        self.assertEqual(record["codex_cli_calls"], 1)
        self.assertEqual(record["error_type"], "RoleToolUseError")


if __name__ == "__main__":
    unittest.main()
