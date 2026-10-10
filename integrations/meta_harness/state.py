"""Durable, local checkpoints for the upstream Meta-Harness execution boundary."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    os.replace(temporary, path)


def tree(root: Path) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in {"__pycache__", ".git"} for part in relative.parts):
            continue
        if path.is_symlink():
            # Native views intentionally reference immutable SDK trial outputs.
            result[str(relative)] = "symlink:" + os.readlink(path)
        elif path.is_file() and not str(relative).endswith("/logs/search/pending_eval.json"):
            result[str(relative)] = digest(path)
    return result


class RunStore:
    """A checkpoint is authoritative only after an execution boundary succeeds.

    Original evaluations and proposer sessions remain in place. Native frontier
    and history writes use a write-ahead record so interruption cannot duplicate
    a history row or leave a frontier one transaction ahead of its checkpoint.
    """

    def __init__(self, output: Path, identity: dict, *, resume: bool):
        self.output = output
        self.path = output / "resume.json"
        self.transaction = output / "native-write.json"
        self.protected_root = output / "upstream"
        if resume:
            if not self.path.exists():
                raise ValueError("This run has no durable checkpoint; use a new output directory")
            self.data = json.loads(self.path.read_text())
            if self.data.get("version") != 1 or self.data.get("identity") != identity:
                raise ValueError("Resume rejected: experiment, template, integration source, model or CLI settings changed")
            self.recover_transaction()
            current = tree(self.protected_root)
            for path, expected in self.data.get("protected", {}).items():
                if current.get(path) != expected:
                    raise ValueError(f"Resume rejected: recorded code or evidence changed: {path}")
            self.recover_proposer()
            current = tree(self.protected_root)
            extra = current.keys() - self.data.get("protected", {}).keys()
            if extra:
                raise ValueError(f"Resume rejected: unrecorded code or evidence: {sorted(extra)[:5]}")
        else:
            self.data = {"version": 1, "identity": identity, "bindings": {}, "protected": {},
                         "proposer_attempts": [], "proposals": {}}
            self.save()

    def save(self, bindings: dict | None = None) -> None:
        if bindings is not None:
            self.data["bindings"] = bindings
        self.data["protected"] = tree(self.protected_root)
        write_json(self.path, self.data)

    def archive_result(self) -> None:
        path = self.output / "result.json"
        if path.exists():
            directory = self.output / "attempts"
            directory.mkdir(exist_ok=True)
            destination = directory / f"result-{len(list(directory.glob('result-*.json'))) + 1:04d}.json"
            shutil.copy2(path, destination)

    def commit_native(self, path: Path, content: str, bindings: dict) -> None:
        record = {"path": str(path.relative_to(self.protected_root)),
                  "previous": digest(path) if path.exists() else None,
                  "content": content, "bindings": bindings}
        write_json(self.transaction, record)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".atomic")
        temporary.write_text(content)
        os.replace(temporary, path)
        self.save(bindings)
        self.transaction.unlink()

    def recover_transaction(self) -> None:
        if not self.transaction.exists():
            return
        record = json.loads(self.transaction.read_text())
        path = self.protected_root / record["path"]
        current = digest(path) if path.exists() else None
        desired = hashlib.sha256(record["content"].encode()).hexdigest()
        if current not in {record["previous"], desired}:
            raise ValueError("Resume rejected: interrupted native write was modified")
        # Verify all other recorded evidence before rolling this write forward.
        current_tree = tree(self.protected_root)
        for relative, expected in self.data.get("protected", {}).items():
            if relative != record["path"] and current_tree.get(relative) != expected:
                raise ValueError(f"Resume rejected: recorded code or evidence changed: {relative}")
        temporary = path.with_name(path.name + ".atomic")
        temporary.write_text(record["content"])
        os.replace(temporary, path)
        self.save(record["bindings"])
        self.transaction.unlink()

    def begin_proposer(self, iteration: int, session: Path, before: dict[str, str]) -> int:
        number = len(self.data["proposer_attempts"]) + 1
        self.data["active_proposer"] = {"iteration": iteration, "number": number,
                                         "session": str(session), "before": before}
        # Preserve the last valid checkpoint while the proposer runs.
        write_json(self.path, self.data)
        return number

    def finish_proposer(self, *, status: str, usage: Any = None, error: str | None = None) -> None:
        record = self.data.pop("active_proposer")
        record.pop("before", None)
        record.update(status=status, usage=usage, error=error)
        self.data["proposer_attempts"].append(record)
        self.save()

    def recover_proposer(self) -> None:
        active = self.data.get("active_proposer")
        if not active:
            return
        # A process died before the proposer boundary committed. Keep every
        # session byte, and archive new workspace files before retrying.
        example = self.protected_root / "reference_examples/terminal_bench_2"
        before = active["before"]
        additions = []
        for path in example.rglob("*"):
            relative = path.relative_to(example)
            if not path.is_file() or any(p in {"__pycache__", ".git"} for p in relative.parts):
                continue
            if relative.parts[0] in {"jobs", "logs"}:
                continue
            if str(relative) not in before:
                additions.append(path)
        archived = self.output / "attempts" / f"proposal-{active['number']:04d}" / "workspace-additions"
        # Whole new artifact directories also contain ignored interpreter
        # caches. Archive those together so the retry can reuse its own name.
        agents = example / "agents"
        for directory in list(agents.iterdir()):
            prefix = str(directory.relative_to(example)) + "/"
            if directory.is_dir() and not directory.is_symlink() and not any(name.startswith(prefix) for name in before):
                destination = archived / directory.relative_to(example)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(directory), str(destination))
        for path in additions:
            if not path.exists():
                continue
            destination = archived / path.relative_to(example)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
        # Remove only empty directories left by the archived candidate.
        for path in sorted((example / "agents").rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()
        pending = example / "logs/search/pending_eval.json"
        if pending.exists():
            destination = archived.parent / "pending_eval.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pending), str(destination))
        session = Path(active["session"])
        current = tree(self.protected_root)
        session_relative = str(session.relative_to(self.protected_root)) + "/"
        extras = current.keys() - self.data.get("protected", {}).keys()
        if any(not path.startswith(session_relative) for path in extras):
            raise ValueError("Resume rejected: proposer created evidence outside its session")
        usage = active.get("usage")
        if (session / "result.json").exists():
            try:
                result = json.loads((session / "result.json").read_text())
                if isinstance(result.get("usage"), dict):
                    usage = result["usage"]
            except (OSError, json.JSONDecodeError):
                # A killed process can leave a partial report. Keep the bytes;
                # missing usage remains unknown rather than blocking recovery.
                pass
        self.finish_proposer(status=active.get("status", "interrupted"), usage=usage,
                             error=active.get("error", "Proposer execution did not commit; partial files archived before retry"))
