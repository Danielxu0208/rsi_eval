"""Locate optional shipped resources in a checkout or an installed wheel."""
from importlib.resources import files
from pathlib import Path


def codex_template() -> Path:
    root = Path(__file__).resolve().parents[2]
    local = root / "templates" / "codex_agent"
    if (local / "agent.py").is_file():
        return local
    return Path(str(files("rsi_eval_templates").joinpath("codex_agent")))


def integration_resource(method: str, name: str) -> Path:
    if method not in {"meta_harness", "rrsi", "ahe", "harnessx"}:
        raise ValueError(f"Unknown method: {method}")
    return Path(str(files(f"integrations.{method}").joinpath(name)))
