"""Text-generation transport for upstream RRSI's existing JSON action loops.

This module does not execute model actions. RRSI remains responsible for its
workspace tools, critic, edit accounting and candidate selection.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

_lock = threading.Lock()


def generate(
    prompt: str,
    system: str | None = None,
    max_retries: int = 6,
    json_only: bool = False,
    model: str | None = None,
    max_tokens: int = 20_000,
    cache_prefix: str | None = None,
    *,
    config_path: str | Path,
) -> str:
    """Keep the native return contract, using the authenticated Codex CLI.

    ``cache_prefix`` is retained as input text, not an Anthropic cache request.
    The CLI cannot enforce the original API's generation-token parameter; we
    reject an over-budget completed response rather than silently ignoring it.
    Transport failures are surfaced instead of consuming further quota through
    an implicit retry loop. Native RRSI still handles invalid action responses.
    """
    from rsi_eval.codex import CodexAuthError, CodexQuotaError, CodexSettings, run_codex
    from .failures import stop_run

    from rsi_eval.relocation import relocate
    config = relocate(json.loads(Path(config_path).read_text()), Path(config_path))
    settings_data = dict(config["codex"])
    if model is not None and model != settings_data["model"]:
        raise ValueError(f"RRSI role model {model!r} differs from configured Codex model")
    settings_data["auth_dir"] = Path(settings_data["auth_dir"]).expanduser().resolve()
    settings = CodexSettings(**settings_data)
    log_root = Path(os.environ.get("RRSI_CODEX_LOG_DIR", str(Path(config_path).parent / "runs" / "coding_codex" / "codex_roles")))
    call_dir = log_root / uuid.uuid4().hex
    call_dir.mkdir(parents=True, exist_ok=False)
    # An empty directory and role mode prevent Codex's own agent tools from
    # bypassing RRSI's action executor. run_codex audits the emitted tool events.
    cwd = call_dir / "workspace"
    cwd.mkdir()
    full_prompt = (cache_prefix + "\n\n" if cache_prefix else "") + prompt
    if json_only:
        full_prompt += "\n\nReturn exactly one JSON action object without Markdown fences."
    with _lock:  # Also bounds the native analyst's parallel digester calls.
        run_dir = Path(os.environ.get("RRSI_RUN_DIR", str(log_root.parent)))
        # Other digesters may already be queued when one exhausts the login.
        if (run_dir / "STOP").exists():
            raise SystemExit(f"RRSI STOP is present: {run_dir / 'STOP'}")
        try:
            result = run_codex(
                full_prompt, settings=settings, cwd=cwd, mode="role", system=system,
                # Malformed action JSON belongs to the native RRSI repair loop.
                # The transport checks CLI completion and tool use, not action shape.
                json_only=False, log_dir=call_dir,
            )
        except (CodexAuthError, CodexQuotaError) as exc:
            reason = "quota_exceeded" if isinstance(exc, CodexQuotaError) else "auth_error"
            stop_run(run_dir, reason, f"Codex role call {call_dir.name}")
    output_tokens = result.usage.get("output_tokens")
    if output_tokens is not None and output_tokens > max_tokens:
        raise RuntimeError(f"Codex response exceeded RRSI output budget ({output_tokens}>{max_tokens})")
    return result.text
