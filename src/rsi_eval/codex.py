"""Normal Codex CLI calls using a dedicated, serialized ChatGPT login.

This module never exchanges tokens or invokes a model HTTP API. The CLI owns
authentication and refresh. Its private home must be outside experiment artifacts.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator


class CodexError(RuntimeError):
    """A CLI call failed; callers must not substitute another backend."""


class CodexAuthError(CodexError):
    pass


class CodexQuotaError(CodexError):
    pass


class RoleToolUseError(CodexError):
    pass


@dataclass(frozen=True)
class CodexSettings:
    model: str
    auth_dir: Path
    executable: str = "codex"
    expected_version: str | None = None
    reasoning_effort: str = "medium"
    timeout_seconds: float = 600

    def __post_init__(self) -> None:
        object.__setattr__(self, "auth_dir", Path(self.auth_dir).expanduser().resolve())
        if not self.model.strip():
            raise ValueError("Codex model must be explicit")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass
class CodexResult:
    text: str
    events: list[dict[str, Any]]
    usage: dict[str, int | None]
    cli_version: str
    returncode: int = 0
    session_id: str | None = None
    stderr: str = ""
    elapsed_seconds: float | None = None
    codex_cli_calls: int = 1


def _auth_fingerprint(auth_dir: Path) -> str:
    return hashlib.sha256((auth_dir / "auth.json").read_bytes()).hexdigest()


def validate_auth(auth_dir: Path) -> Path:
    """Check metadata only; never return credentials or include them in errors."""
    auth_dir = Path(auth_dir).expanduser().resolve()
    auth_file = auth_dir / "auth.json"
    try:
        data = json.loads(auth_file.read_text())
    except (OSError, ValueError):
        raise CodexAuthError(
            "Dedicated Codex auth.json is missing or invalid; use normal Codex "
            "login with file-backed credential storage for this auth directory"
        ) from None
    if not isinstance(data, dict) or data.get("auth_mode") != "chatgpt" or not isinstance(data.get("tokens"), dict):
        raise CodexAuthError("The dedicated Codex login must use ChatGPT managed auth")
    if not data["tokens"].get("access_token") or not data["tokens"].get("refresh_token"):
        raise CodexAuthError("The dedicated Codex login has no managed token bundle")
    marker = auth_dir / ".rsi-eval-auth-uncertain.json"
    if marker.exists():
        try:
            previous = json.loads(marker.read_text()).get("fingerprint")
        except (OSError, ValueError):
            previous = None
        if previous == _auth_fingerprint(auth_dir) or previous is None:
            raise CodexAuthError(
                "Container authentication write-back failed; log in again in the "
                "dedicated auth directory before resuming"
            )
        marker.unlink()  # A fresh CLI login has replaced the stale state.
    return auth_file


def mark_auth_uncertain(auth_dir: Path) -> None:
    auth_dir = Path(auth_dir)
    marker = auth_dir / ".rsi-eval-auth-uncertain.json"
    try:
        fingerprint = _auth_fingerprint(auth_dir)
    except OSError:
        fingerprint = None
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"fingerprint": fingerprint}, stream)


def persist_auth_file(downloaded: Path, auth_dir: Path) -> None:
    """Atomically publish the CLI-produced auth file while the auth lock is held."""
    if Path(downloaded).is_symlink():
        raise CodexAuthError("Container auth write-back must be a regular file")
    try:
        data = json.loads(Path(downloaded).read_text())
        if data.get("auth_mode") != "chatgpt" or not all(
            (data.get("tokens") or {}).get(k) for k in ("access_token", "refresh_token")
        ):
            raise ValueError("not managed auth")
    except (OSError, ValueError, TypeError, AttributeError):
        raise CodexAuthError("Container returned an invalid managed auth file") from None
    os.chmod(downloaded, 0o600)
    os.replace(downloaded, Path(auth_dir) / "auth.json")
    (Path(auth_dir) / ".rsi-eval-auth-uncertain.json").unlink(missing_ok=True)


class _AuthLock:
    def __init__(self, auth_dir: Path):
        self.auth_dir = Path(auth_dir)
        self.fd: int | None = None

    def acquire_once(self) -> bool:
        if self.fd is None:
            if not self.auth_dir.is_dir():
                raise CodexAuthError("Dedicated Codex auth directory does not exist")
            self.fd = os.open(self.auth_dir / ".rsi-eval-auth.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def close(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


@contextlib.contextmanager
def auth_lock(auth_dir: Path, timeout_seconds: float = 600) -> Iterator[Path]:
    lock = _AuthLock(auth_dir)
    deadline = time.monotonic() + timeout_seconds
    try:
        while not lock.acquire_once():
            if time.monotonic() >= deadline:
                raise CodexAuthError("Timed out waiting for the dedicated Codex auth lock")
            time.sleep(0.05)
        yield validate_auth(auth_dir)
    finally:
        lock.close()


@contextlib.asynccontextmanager
async def async_auth_lock(auth_dir: Path, timeout_seconds: float = 600):
    lock = _AuthLock(auth_dir)
    deadline = time.monotonic() + timeout_seconds
    try:
        while not lock.acquire_once():
            if time.monotonic() >= deadline:
                raise CodexAuthError("Timed out waiting for the dedicated Codex auth lock")
            await asyncio.sleep(0.05)
        yield validate_auth(auth_dir)
    finally:
        lock.close()


def _cli_env(settings: CodexSettings) -> dict[str, str]:
    # Do not inherit API keys, provider overrides, parent-session configuration,
    # or external-token/WIF credentials. Normal CLI login is the only auth path.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP",
               "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env["CODEX_HOME"] = str(settings.auth_dir)
    return env


def codex_version(settings: CodexSettings) -> str:
    try:
        proc = subprocess.run([settings.executable, "--version"], text=True,
                              capture_output=True, timeout=15, env=_cli_env(settings))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexError(f"Cannot query Codex CLI version: {type(exc).__name__}") from None
    if proc.returncode:
        raise CodexError("Codex --version failed")
    version = proc.stdout.strip().removeprefix("codex-cli ")
    if not version or "\n" in version:
        raise CodexError("Unexpected Codex --version output")
    if settings.expected_version and version != settings.expected_version:
        raise CodexError(f"Codex CLI version mismatch: expected {settings.expected_version}, got {version}")
    return version


_ROLE_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "view_image",
    "image_generation", "browser_use", "computer_use", "in_app_browser", "code_mode",
    "code_mode_host", "hooks", "skill_search", "tool_suggest", "sleep_tool", "goals",
)

_AMBIENT_DISABLED_FEATURES = ("apps", "plugins", "hooks")


def _command(settings: CodexSettings, mode: str, cwd: Path, schema_path: Path | None,
             images: list[Path] | None = None) -> list[str]:
    cmd = [settings.executable, "exec", "--json", "--color", "never",
           "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
           "--model", settings.model, "--cd", str(cwd)]
    # --ignore-rules disables execpolicy files, not AGENTS.md or host skills.
    # Both proposer modes exclude personal integrations; edit mode retains the
    # managed method checkout's project instructions and normal coding tools.
    cmd += ["--enable", "skip_host_skill_discovery"]
    for feature in _AMBIENT_DISABLED_FEATURES:
        cmd += ["--disable", feature]
    overrides: dict[str, Any] = {
        "model_provider": "openai", "forced_login_method": "chatgpt",
        "cli_auth_credentials_store": "file", "approval_policy": "never",
        "model_reasoning_effort": settings.reasoning_effort, "web_search": "disabled",
    }
    if mode == "role":
        cmd += ["--sandbox", "read-only"]
        overrides["project_doc_max_bytes"] = 0
        for feature in _ROLE_DISABLED_FEATURES:
            if feature not in _AMBIENT_DISABLED_FEATURES:
                cmd += ["--disable", feature]
    elif mode == "edit":
        cmd += ["--sandbox", "workspace-write"]
    else:
        raise ValueError("Codex mode must be 'role' or 'edit'")
    for key, value in overrides.items():
        cmd += ["-c", f"{key}={json.dumps(value)}"]
    if schema_path:
        cmd += ["--output-schema", str(schema_path)]
    for path in images or []:
        cmd += ["--image", str(path)]
    if images:
        cmd.append("--")  # End variadic image arguments before the stdin prompt.
    cmd.append("-")
    return cmd


def _image_inputs(images: list[Path] | None, *, cwd: Path, scratch: Path,
                  auth_dir: Path, log_dir: Path | None) -> tuple[list[Path], list[dict]]:
    """Copy explicitly supplied raster inputs; no URL fetching or tool access.

    Private, generated names avoid the CLI's comma-separated path semantics.
    The CLI validates image decoding. Receipts describe the exact supplied bytes.
    """
    paths, records = [], []
    for index, value in enumerate(images or [], 1):
        source = Path(value).expanduser()
        source = source if source.is_absolute() else cwd / source
        if source.is_symlink() or not source.is_file():
            raise ValueError("Codex image input must be a regular, non-symlink file")
        source = source.resolve()
        if source.is_relative_to(auth_dir):
            raise ValueError("Codex image inputs cannot come from the authentication directory")
        with source.open("rb") as stream:
            data = stream.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("Codex image input exceeds the 20 MiB transport limit")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime, extension = "image/png", "png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime, extension = "image/jpeg", "jpg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            mime, extension = "image/gif", "gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime, extension = "image/webp", "webp"
        else:
            raise ValueError("Codex image input must contain PNG, JPEG, WebP or GIF bytes")
        digest = hashlib.sha256(data).hexdigest()
        name = f"input-{index:04d}-{digest[:16]}.{extension}"
        path = scratch / name
        path.write_bytes(data)
        paths.append(path)
        record = {"index": index, "sha256": digest, "bytes": len(data), "mime_type": mime}
        if log_dir is not None:
            destination = log_dir / "inputs"
            destination.mkdir(parents=True, exist_ok=True)
            (destination / name).write_bytes(data)
            record["path"] = "inputs/" + name
        records.append(record)
    if log_dir is not None and records:
        (log_dir / "images.json").write_text(json.dumps(records, indent=2) + "\n")
    return paths, records


def _assert_role_event(event: dict[str, Any]) -> None:
    item = event.get("item")
    # The CLI reports startup warnings as error items (including unstable
    # feature notices). They carry text only and do not execute a tool.
    if isinstance(item, dict) and item.get("type") not in ("agent_message", "reasoning", "error"):
        raise RoleToolUseError(f"Codex role attempted a disallowed item: {item.get('type', 'unknown')}")
    # Also reject raw protocol tool calls should a CLI version expose these.
    if event.get("type") in ("function_call", "custom_tool_call", "tool_call"):
        raise RoleToolUseError("Codex role attempted a tool call")


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def _execute(cmd: list[str], prompt: str, env: dict[str, str], cwd: Path,
             timeout_seconds: float, mode: str, event_stream) -> tuple[list[dict], int, str]:
    events: list[dict] = []
    received: queue.Queue = queue.Queue()
    with tempfile.TemporaryFile(mode="w+t") as stderr_file:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=stderr_file, text=True,
                                    start_new_session=True)
        except OSError as exc:
            raise CodexError(f"Cannot start Codex CLI: {type(exc).__name__}") from None

        def read_stdout() -> None:
            try:
                for line in proc.stdout:
                    received.put(line)
            finally:
                received.put(None)

        thread = threading.Thread(target=read_stdout, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout_seconds
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexError("Codex CLI timed out")
                try:
                    line = received.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                if line is None:
                    break
                if event_stream:
                    event_stream.write(line)
                    event_stream.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    raise CodexError("Codex --json emitted a non-JSON stdout record") from None
                if not isinstance(event, dict):
                    raise CodexError("Codex --json emitted a non-object record")
                events.append(event)
                if mode == "role":
                    _assert_role_event(event)
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except (BrokenPipeError, subprocess.TimeoutExpired):
            raise CodexError("Codex CLI ended early or timed out") from None
        finally:
            _stop_process(proc)
            proc.stdout.close()
            thread.join(timeout=1)
        stderr_file.seek(0)
        return events, proc.returncode, stderr_file.read()


def _run_codex(prompt: str, *, settings: CodexSettings, cwd: Path,
              mode: str = "role", system: str | None = None, json_only: bool = False,
              output_schema: dict | None = None, log_dir: Path | None = None,
              images: list[Path] | None = None,
              _accounting: dict[str, Any]) -> CodexResult:
    """Run one CLI turn. ``role`` has no workspace context or tool permission.

    ``system`` is prepended to the user request, not advertised as a separately
    authenticated system-message channel. JSON is checked after the call.
    """
    cwd = Path(cwd).resolve()
    if not cwd.is_dir():
        raise ValueError("Codex working directory does not exist")
    if mode not in ("role", "edit"):
        raise ValueError("Codex mode must be 'role' or 'edit'")
    if settings.auth_dir == cwd or cwd in settings.auth_dir.parents:
        raise ValueError("Dedicated Codex auth must be outside the working artifact")
    if log_dir is not None:
        resolved_logs = Path(log_dir).resolve()
        if settings.auth_dir == resolved_logs or resolved_logs in settings.auth_dir.parents:
            raise ValueError("Dedicated Codex auth must be outside the output logs")
    request = ((system + "\n\n") if system else "") + prompt
    if json_only:
        request += "\n\nReturn exactly one JSON value without Markdown fences."
    with auth_lock(settings.auth_dir, settings.timeout_seconds):
        version = codex_version(settings)
        _accounting["cli_version"] = version
        with tempfile.TemporaryDirectory(prefix="rsi-codex-role-") as scratch:
            # A role sees supplied evidence only; it must not discover the cwd's
            # project instructions, skills, MCP servers, or other files.
            execution_dir = Path(scratch) if mode == "role" else cwd
            image_paths, image_records = _image_inputs(images, cwd=cwd, scratch=Path(scratch).resolve(),
                                                       auth_dir=settings.auth_dir, log_dir=log_dir)
            if image_records:
                _accounting["images"] = image_records
            schema_path = None
            if output_schema is not None:
                schema_path = Path(scratch) / "output-schema.json"
                schema_path.write_text(json.dumps(output_schema))
            with contextlib.ExitStack() as stack:
                event_stream = None
                if log_dir is not None:
                    log_dir = Path(log_dir)
                    log_dir.mkdir(parents=True, exist_ok=True)
                    event_stream = stack.enter_context((log_dir / "events.jsonl").open("w"))
                    (log_dir / "request.txt").write_text(request)
                _accounting["codex_cli_calls"] = 1
                events, returncode, stderr = _execute(
                    _command(settings, mode, execution_dir, schema_path, image_paths), request,
                    _cli_env(settings), execution_dir, settings.timeout_seconds, mode,
                    event_stream,
                )
                _accounting.update(events=events, returncode=returncode)
    completed = [e for e in events if e.get("type") == "turn.completed"]
    failures = [e for e in events if e.get("type") in ("turn.failed", "error")]
    if log_dir is not None:
        (log_dir / "stderr.txt").write_text(stderr)
    if returncode or failures or not completed:
        if log_dir is not None:
            (log_dir / "result.json").write_text(json.dumps({
                "cli_version": version, "model": settings.model, "mode": mode,
                "status": "failed", "returncode": returncode, "auth_mode": "chatgpt",
                "cost_usd": None,
            }, indent=2))
        failure_text = (json.dumps(failures) + " " + stderr).lower()
        if any(token in failure_text for token in ("usage limit", "quota", "rate limit", "429")):
            raise CodexQuotaError("Codex subscription quota/rate limit prevented completion; no fallback was used")
        if any(token in failure_text for token in ("401", "refresh token", "not logged in", "please log in", "authentication")):
            raise CodexAuthError("Codex login failed; renew the dedicated ChatGPT login before resuming")
        raise CodexError(f"Codex CLI did not complete successfully (exit={returncode})")
    messages = [e["item"].get("text", "") for e in events
                if e.get("type") == "item.completed" and
                isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message"]
    text = messages[-1] if messages else ""
    if not text:
        raise CodexError("Codex completed without a final agent message")
    if json_only or output_schema is not None:
        try:
            json.loads(text)
        except ValueError:
            raise CodexError("Codex final response was not valid JSON") from None
    raw_usage = completed[-1].get("usage") or {}
    usage = {key: raw_usage.get(key) if isinstance(raw_usage.get(key), int) else None
             for key in ("input_tokens", "cached_input_tokens", "output_tokens")}
    session_id = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
    result = CodexResult(text, events, usage, version, returncode, session_id, stderr)
    if log_dir is not None:
        (log_dir / "response.txt").write_text(text)
        (log_dir / "result.json").write_text(json.dumps({
            "cli_version": version, "model": settings.model, "mode": mode,
            "usage": usage, "session_id": session_id, "returncode": returncode,
            "auth_mode": "chatgpt", "cost_usd": None,
        }, indent=2))
        (log_dir / "stderr.txt").write_text(stderr)
    return result


def _known_usage(events: list[dict[str, Any]]) -> dict[str, int | None]:
    raw = next((event["usage"] for event in reversed(events)
                if event.get("type") in {"turn.completed", "turn.failed"}
                and isinstance(event.get("usage"), dict)), {})
    return {key: value if isinstance(value := raw.get(key), int)
            and not isinstance(value, bool) and value >= 0 else None
            for key in ("input_tokens", "cached_input_tokens", "output_tokens")}


def run_codex(prompt: str, *, settings: CodexSettings, cwd: Path,
              mode: str = "role", system: str | None = None, json_only: bool = False,
              output_schema: dict | None = None, log_dir: Path | None = None,
              images: list[Path] | None = None) -> CodexResult:
    """Run one normal CLI turn and retain accounting, including failed calls.

    ``codex_cli_calls`` counts exec invocation attempts, excluding --version;
    it does not claim to count provider requests made within that CLI process.
    Elapsed time includes local preparation and waiting for the auth lock.
    ``system`` is prepended text; it is not a separate system-message channel.
    ``images`` attaches explicit raster files to that turn through ``--image``.
    Inputs retain their order, hashes and bytes in logs without granting tools.
    """
    if log_dir is not None:
        log_dir = Path(log_dir).resolve()
        if settings.auth_dir == log_dir or log_dir in settings.auth_dir.parents:
            raise ValueError("Dedicated Codex auth must be outside the output logs")
    accounting: dict[str, Any] = {"codex_cli_calls": 0}
    started = time.monotonic()
    result = None
    failure = None
    try:
        result = _run_codex(prompt, settings=settings, cwd=cwd, mode=mode,
                            system=system, json_only=json_only,
                            output_schema=output_schema, log_dir=log_dir, images=images,
                            _accounting=accounting)
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        elapsed = time.monotonic() - started
        if result is not None:
            result.elapsed_seconds = elapsed
            result.codex_cli_calls = accounting["codex_cli_calls"]
            result.usage = _known_usage(result.events)
        if log_dir is not None:
            events = accounting.get("events", [])
            if not events and accounting["codex_cli_calls"]:
                # A role-tool rejection or timeout can happen before _execute
                # returns. Its flushed JSONL remains the source of partial usage.
                try:
                    for line in (log_dir / "events.jsonl").read_text().splitlines():
                        event = json.loads(line)
                        if isinstance(event, dict):
                            events.append(event)
                except (OSError, ValueError):
                    pass
            record = {
                "cli_version": accounting.get("cli_version"), "model": settings.model,
                "mode": mode, "status": "completed" if result is not None else "failed",
                "returncode": accounting.get("returncode"), "auth_mode": "chatgpt",
                "cost_usd": None, "usage": _known_usage(events),
                "codex_cli_calls": accounting["codex_cli_calls"], "elapsed_seconds": elapsed,
                "session_id": next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None),
            }
            if failure is not None:
                record["error_type"] = type(failure).__name__
            if "images" in accounting:
                record["images"] = accounting["images"]
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "result.json").write_text(json.dumps(record, indent=2))
            except OSError:
                if failure is None:
                    raise
