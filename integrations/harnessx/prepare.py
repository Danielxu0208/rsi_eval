"""Pinned optional checkout and narrowly scoped compatibility patches."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from . import UPSTREAM_COMMIT, UPSTREAM_URL

PATCHES = {
    "recipe/tb2_evolver/run.py": (
        ("        require_evidence=not args.no_require_evidence,\n", ""),
    ),
    "harnessx/meta_harness/agent.py": (
        ("replay_timeout_s=min(replay_timeout_s, 20.0),", "replay_timeout_s=replay_timeout_s,"),
    ),
}


def git(root, *args):
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def patched_files(checkout: Path) -> dict:
    hashes = {}
    for relative, replacements in PATCHES.items():
        text = git(checkout, "show", f"{UPSTREAM_COMMIT}:{relative}") + "\n"
        for before, after in replacements:
            if text.count(before) != 1:
                raise ValueError(f"Upstream patch anchor drifted: {relative}")
            text = text.replace(before, after)
        hashes[relative] = hashlib.sha256(text.encode()).hexdigest()
    return hashes


def check_checkout(checkout: Path) -> dict:
    checkout = Path(checkout).resolve()
    if git(checkout, "rev-parse", "HEAD") != UPSTREAM_COMMIT:
        raise ValueError("HarnessX checkout does not match pinned commit")
    expected = patched_files(checkout)
    for path, digest in expected.items():
        if hashlib.sha256((checkout / path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"HarnessX integration patch missing or modified: {path}")
    changed = set(git(checkout, "diff", "HEAD", "--name-only").splitlines())
    if changed != set(PATCHES):
        raise ValueError("Unexpected tracked changes in HarnessX runtime checkout")
    return {"upstream_commit": UPSTREAM_COMMIT, "patches": expected, "checkout": str(checkout)}


def prepare(checkout: Path, source: str = UPSTREAM_URL) -> dict:
    checkout = Path(checkout).resolve()
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-hardlinks", source, str(checkout)], check=True)
        git(checkout, "checkout", "--detach", UPSTREAM_COMMIT)
    if git(checkout, "rev-parse", "HEAD") != UPSTREAM_COMMIT:
        raise ValueError("Refusing to modify checkout at another revision")
    if (checkout / ".rsi-eval-harnessx.json").exists():
        return check_checkout(checkout)
    if git(checkout, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Refusing to overwrite tracked checkout changes")
    for relative, replacements in PATCHES.items():
        path = checkout / relative
        text = path.read_text()
        for before, after in replacements:
            if text.count(before) != 1:
                raise ValueError(f"Upstream patch anchor drifted: {relative}")
            text = text.replace(before, after)
        path.write_text(text)
    result = check_checkout(checkout)
    (checkout / ".rsi-eval-harnessx.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
