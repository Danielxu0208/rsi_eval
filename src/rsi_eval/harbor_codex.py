"""Harbor 0.18 target: one Codex CLI session with an evolvable Python wrapper.

The install and ATIF parser are inherited from Harbor. The run lifecycle follows
Harbor's Apache-2.0 Codex adapter, with private credential write-back before
cleanup. Candidate code never owns the authentication implementation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit

from harbor.agents.installed.codex import Codex
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

from rsi_eval.codex import (
    CodexAuthError, CodexError, CodexQuotaError, async_auth_lock, mark_auth_uncertain, persist_auth_file,
)
from rsi_eval.artifacts import artifact_hash


def _cli_events(result) -> list[dict[str, Any]]:
    events = []
    # Inspect the full output before Harbor truncates it for an exception. Tool
    # output is nested inside command_execution items and is never diagnostic.
    for output in (result.stdout or "", result.stderr or ""):
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                # CLI startup/argument/auth failures can precede JSON mode.
                # Accept only an explicit diagnostic prefix, never arbitrary
                # text from a benchmark command's output.
                if re.match(r"^\s*error:\s", line, flags=re.IGNORECASE):
                    events.append({"type": "error", "message": line.strip()})
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def _cli_diagnostic_error(events: list[dict[str, Any]]) -> CodexError | None:
    messages = []
    for event in events:
        diagnostic = None
        if event.get("type") == "turn.failed":
            diagnostic = event.get("error")
        elif event.get("type") == "error":
            diagnostic = event.get("message") or event.get("error")
        elif event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "error":
                diagnostic = item.get("message")
        if isinstance(diagnostic, dict):
            diagnostic = " ".join(f"{key}: {diagnostic[key]}" for key in ("message", "code", "type")
                                  if isinstance(diagnostic.get(key), (str, int)))
        if isinstance(diagnostic, str):
            messages.append(diagnostic)
    text = "\n".join(messages).lower()
    if re.search(r"usage[ _]limit|insufficient_quota|rate[ _]limit|quota.{0,30}(?:exceed|exhaust|reached)|(?:http|status|code)\D{0,8}429\b", text):
        return CodexQuotaError("Codex subscription quota/rate limit prevented completion; no fallback was used")
    if re.search(r"authentication|unauthori[sz]ed|not logged in|please (?:log|sign) in|refresh[ _]token|invalid[ _](?:(?:access|id)[ _])?token|(?:access |id )?token.{0,30}(?:expired|revoked|invalid)|(?:http|status|code)\D{0,8}401\b", text):
        return CodexAuthError("Codex login failed; renew the dedicated ChatGPT login before resuming")
    return None


def load_candidate(artifact_dir: Path):
    artifact_dir = Path(artifact_dir).resolve()
    source = artifact_dir / "agent.py"
    if not source.is_file():
        raise ValueError("Codex target artifact must contain agent.py")
    # Unique module identity avoids Python's import cache reusing an old candidate.
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    identity = hashlib.sha256(str(artifact_dir).encode()).hexdigest()[:16]
    name = f"_rsi_candidate_{identity}_{digest}"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load Codex candidate")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # Compile the actual source instead of accepting a stale timestamp-based pyc.
    exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
    if not callable(getattr(module, "build_prompt", None)):
        raise ValueError("Candidate agent.py must define build_prompt(instruction, artifact_dir)")
    for hook in ("prepare_environment", "after_run"):
        value = getattr(module, hook, None)
        if value is not None and not inspect.iscoroutinefunction(value):
            raise ValueError(f"Candidate {hook} must be async")
    return module


def mcp_cli_flags(servers) -> str:
    """Explicit task MCP settings survive --ignore-user-config; no host discovery."""
    flags, names = [], set()
    for server in servers or ():
        name = getattr(server, "name", None)
        transport = getattr(server, "transport", None)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in names:
            raise ValueError("MCP config requires unique simple server names")
        names.add(name)
        # These servers belong to the isolated benchmark task, whose actions
        # are the evaluation itself. Interactive MCP prompts cannot be serviced
        # by codex exec; authorize this explicit task server, never host servers.
        settings = {"required": True, "default_tools_approval_mode": "approve",
                    "startup_timeout_sec": 120, "tool_timeout_sec": 300}
        if transport == "streamable-http":
            url = getattr(server, "url", None)
            parsed = urlsplit(url) if isinstance(url, str) else None
            if not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("MCP config requires an HTTP URL without embedded credentials")
            settings["url"] = url
        elif transport == "stdio":
            command, args = getattr(server, "command", None), getattr(server, "args", [])
            if not isinstance(command, str) or not command or not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                raise ValueError("MCP config requires a stdio command and string arguments")
            settings.update(command=command, args=args)
        else:
            raise ValueError("MCP config requires streamable-http or stdio; legacy SSE is unsupported")
        for key, value in settings.items():
            flags.extend(("-c", shlex.quote(f"mcp_servers.{name}.{key}={json.dumps(value)}")))
    return " ".join(flags)


class Agent(Codex):
    """The built-in seed target; arbitrary Harbor agents remain separate targets."""

    def __init__(self, logs_dir: Path, artifact_dir: str | Path,
                 codex_auth_dir: str | Path, codex_version: str,
                 auth_lock_timeout: float = 600, web_search_mode: str = "disabled", **kwargs: Any):
        if not codex_version:
            raise ValueError("codex_version must be pinned explicitly")
        supplied_version = kwargs.pop("version", codex_version)
        if supplied_version != codex_version:
            raise ValueError("version and codex_version disagree")
        if not kwargs.get("model_name"):
            raise ValueError("Codex target model must be explicit")
        self.task_mcp_flags = mcp_cli_flags(kwargs.get("mcp_servers"))
        if web_search_mode not in {"disabled", "cached", "live"}:
            raise ValueError("web_search_mode must be disabled, cached or live")
        self.web_search_mode = web_search_mode
        self.artifact_dir = Path(artifact_dir).resolve()
        self.codex_auth_dir = Path(codex_auth_dir).expanduser().resolve()
        if self.codex_auth_dir == self.artifact_dir or self.artifact_dir in self.codex_auth_dir.parents:
            raise ValueError("Codex auth directory must not be inside the candidate artifact")
        self.auth_lock_timeout = float(auth_lock_timeout)
        self.candidate = load_candidate(self.artifact_dir)
        self.candidate_hash = artifact_hash(self.artifact_dir)
        super().__init__(logs_dir=Path(logs_dir), version=codex_version, **kwargs)

    @staticmethod
    def name() -> str:
        return "rsi-codex"

    def _compute_cost_from_pricing(self, *args, **kwargs):
        # Subscription usage is not billed at the API pricing table rate.
        return None

    def _classify_exec_error(self, command: str, result):
        if "codex exec " not in command:
            return super()._classify_exec_error(command, result)
        events = _cli_events(result)
        diagnostic = _cli_diagnostic_error(events)
        if diagnostic is not None:
            return diagnostic
        if any(event.get("type") in {"turn.failed", "error"} for event in events):
            return CodexError("Codex CLI reported a failed turn; inspect the preserved codex.txt")
        # Do not let the base class interpret words in ordinary tool output as
        # account errors. Keep a real nonzero agent exit visible to the verifier.
        return NonZeroAgentExitCodeError(
            f"Codex CLI exited with status {result.return_code}; inspect the preserved codex.txt"
        )

    @staticmethod
    def _check_cli_completion(result) -> None:
        events = _cli_events(result)
        diagnostic = _cli_diagnostic_error(events)
        if diagnostic is not None:
            raise diagnostic
        if any(event.get("type") in {"turn.failed", "error"} for event in events):
            raise CodexError("Codex CLI reported a failed turn; inspect the preserved codex.txt")
        if not any(event.get("type") == "turn.completed" for event in events):
            raise CodexError("Codex CLI exited without turn.completed; inspect the preserved codex.txt")

    def _convert_events_to_trajectory(self, session_dir):
        trajectory = super()._convert_events_to_trajectory(session_dir)
        if trajectory is not None and trajectory.final_metrics is not None:
            trajectory.final_metrics.total_cost_usd = None
        return trajectory

    async def _hook(self, name: str, environment) -> None:
        hook = getattr(self.candidate, name, None)
        if hook is not None:
            await hook(environment, self.artifact_dir)

    async def _write_back_auth(self, environment) -> None:
        # Use the effective file: Codex's atomic refresh may replace the symlink.
        # Harbor's Docker download uses `compose cp` without -L, so materialize
        # a regular file before transferring it across the environment boundary.
        remote_copy = (self._REMOTE_CODEX_SECRETS_DIR / "auth-return.json").as_posix()
        fd, name = tempfile.mkstemp(prefix=".rsi-auth-", dir=self.codex_auth_dir)
        os.close(fd)
        temporary = Path(name)
        try:
            await self.exec_as_agent(environment, command=(
                f"cp -L {shlex.quote((self._REMOTE_CODEX_HOME / 'auth.json').as_posix())} "
                f"{shlex.quote(remote_copy)} && chmod 600 {shlex.quote(remote_copy)}"
            ))
            await environment.download_file(
                remote_copy, temporary
            )
            persist_auth_file(temporary, self.codex_auth_dir)
        except BaseException:
            mark_auth_uncertain(self.codex_auth_dir)
            raise CodexAuthError(
                "Codex container auth write-back failed; stop and renew the dedicated login"
            ) from None
        finally:
            temporary.unlink(missing_ok=True)

    async def _finish(self, environment, env: dict[str, str]) -> None:
        auth_error = None
        try:
            await self._write_back_auth(environment)
        except BaseException as exc:
            auth_error = exc
        try:
            await self.exec_as_agent(
                environment,
                command=(
                    f"mkdir -p {shlex.quote(EnvironmentPaths.agent_dir.as_posix())}; "
                    'if [ -d "$CODEX_HOME/sessions" ]; then '
                    f'cp -R "$CODEX_HOME/sessions" {shlex.quote(EnvironmentPaths.agent_dir.as_posix())}/sessions; '
                    "fi"
                ), env=env,
            )
        finally:
            # No credentials enter /logs/agent, even after an exception.
            try:
                await self.exec_as_agent(environment, command=(
                    f"rm -rf {shlex.quote(self._REMOTE_CODEX_SECRETS_DIR.as_posix())} "
                    '"$CODEX_HOME"'
                ), env=env)
            except Exception:
                self.logger.warning("Codex temporary authentication cleanup failed")
        if auth_error is not None:
            raise auth_error

    async def run(self, instruction: str, environment, context: AgentContext) -> None:
        # The model receives the candidate's prompt and tools, not merely its name.
        transformed = self.candidate.build_prompt(instruction, self.artifact_dir)
        if not isinstance(transformed, str) or not transformed.strip():
            raise ValueError("Candidate build_prompt must return a non-empty string")
        transformed = self.render_instruction(transformed)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "candidate.json").write_text(json.dumps({
            "artifact_sha256": self.candidate_hash, "codex_version": self.version(),
            "model": self.model_name, "auth_mode": "chatgpt", "cost_usd": None,
            "task_mcp_servers": [server.model_dump() for server in self.mcp_servers or ()],
            "web_search_mode": self.web_search_mode,
        }, indent=2))
        (self.logs_dir / "rendered_prompt.txt").write_text(transformed)
        async with async_auth_lock(self.codex_auth_dir, self.auth_lock_timeout) as auth_file:
            remote_home = self._REMOTE_CODEX_HOME.as_posix()
            remote_secrets = self._REMOTE_CODEX_SECRETS_DIR.as_posix()
            remote_auth = (self._REMOTE_CODEX_SECRETS_DIR / "auth.json").as_posix()
            env = {"CODEX_HOME": remote_home}
            await self.exec_as_agent(environment, command=(
                f"mkdir -p {shlex.quote(remote_home)} {shlex.quote(remote_secrets)} "
                f"{shlex.quote(EnvironmentPaths.agent_dir.as_posix())}; "
                f"chmod 700 {shlex.quote(remote_home)} {shlex.quote(remote_secrets)}"
            ), env=env)
            await environment.upload_file(auth_file, remote_auth)
            # From here on, every failure must still recover the current auth file.
            try:
                owner = getattr(environment, "default_user", None)
                if owner is not None:
                    await self.exec_as_root(environment, command=(
                        f"chown {shlex.quote(str(owner))} {shlex.quote(remote_auth)}"
                    ))
                await self.exec_as_agent(environment, command=(
                    f"chmod 600 {shlex.quote(remote_auth)}; "
                    f'ln -sf {shlex.quote(remote_auth)} "$CODEX_HOME/auth.json"'
                ), env=env)
                await self._hook("prepare_environment", environment)
                for registration in (self._build_register_skills_command(),):
                    if registration:
                        await self.exec_as_agent(environment, command=registration, env=env)
                cli_flags = self.build_cli_flags()
                # Externally isolated benchmark container, matching Harbor's
                # installed-agent execution boundary. This is never used on host.
                command = (
                    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                    "unset OPENAI_API_KEY CODEX_API_KEY CODEX_ACCESS_TOKEN OPENAI_BASE_URL; "
                    "codex exec --dangerously-bypass-approvals-and-sandbox "
                    "--ignore-user-config "
                    "--skip-git-repo-check --json --enable unified_exec "
                    "--disable multi_agent --disable multi_agent_v2 "
                    "--disable apps --disable plugins --disable browser_use "
                    "--disable computer_use --disable in_app_browser "
                    "--disable in_app_local_automation "
                    f"--model {shlex.quote(self.model_name.split('/')[-1])} "
                    "-c 'cli_auth_credentials_store=\"file\"' "
                    "-c 'forced_login_method=\"chatgpt\"' "
                    "-c 'model_provider=\"openai\"' "
                    f"{cli_flags} {self.task_mcp_flags} -c 'web_search=\"{self.web_search_mode}\"' -- {shlex.quote(transformed)} "
                    f"2>&1 </dev/null | tee {shlex.quote((EnvironmentPaths.agent_dir / self._OUTPUT_FILENAME).as_posix())}"
                )
                # If the runner is forcibly killed after a CLI refresh, retain
                # an uncertainty marker instead of silently reusing the old copy.
                mark_auth_uncertain(self.codex_auth_dir)
                execution = await self.exec_as_agent(environment, command=command, env=env)
                self._check_cli_completion(execution)
                await self._hook("after_run", environment)
            finally:
                await self._finish(environment, env)

    def populate_context_post_run(self, context: AgentContext) -> None:
        sessions = self.logs_dir / "sessions"
        session_files = sorted(sessions.rglob("*.jsonl")) if sessions.exists() else []
        if len(session_files) > 1:
            # Harbor 0.18 silently parses only the first file. This seed target
            # promises one session, so a multi-session run must fail explicitly.
            raise RuntimeError("Built-in Codex target produced multiple sessions; use a custom multi-session target")
        super().populate_context_post_run(context)
        context.cost_usd = None
        context.metadata = {**(context.metadata or {}), "auth_mode": "chatgpt",
                            "artifact_sha256": self.candidate_hash,
                            "codex_version": self.version(), "cost_basis": "unavailable_subscription"}
        # Harbor substitutes 0 for absent final metrics; recover unknown values
        # from the actual native token event instead of manufacturing usage.
        totals = None
        for source in session_files:
            for line in source.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                    value = (payload.get("info") or {}).get("total_token_usage")
                    if isinstance(value, dict):
                        totals = value
        for attribute, key in (("n_input_tokens", "input_tokens"),
                               ("n_cache_tokens", "cached_input_tokens"),
                               ("n_output_tokens", "output_tokens")):
            value = totals.get(key) if totals else None
            setattr(context, attribute, value if isinstance(value, int) else None)
