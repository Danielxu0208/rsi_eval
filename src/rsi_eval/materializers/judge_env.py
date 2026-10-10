"""Read explicit judge credentials without adding secrets to run provenance."""
from __future__ import annotations

import os
from pathlib import Path
import re


def load_judge_env(options: dict, allowed_keys: set[str]) -> dict[str, str]:
    """Read an allowlisted subset of host variables and an optional private file.

    Files use one KEY=value assignment per line, with optional matching quotes.
    No shell expansion, command substitution or multiline values are performed.
    Unrelated keys fail explicitly instead of reaching an agent or interpreter.
    """
    values = {key: os.environ[key] for key in allowed_keys if os.environ.get(key)}
    filename = options.get("judge_env_file")
    if filename is None:
        return values
    path = Path(filename).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("judge_env_file must be a private environment file smaller than 1 MiB")
    for index, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key not in allowed_keys:
            raise ValueError(f"judge_env_file line {index} contains an unsupported assignment")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"judge_env_file line {index} has unmatched quotes")
            value = value[1:-1]
        if "\x00" in value:
            raise ValueError(f"judge_env_file line {index} contains an invalid value")
        if value:
            values[key] = value
        else:
            values.pop(key, None)
    return values
