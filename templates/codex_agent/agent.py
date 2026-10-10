"""Candidate code: edit the strategy, prompt, and task-local helper tools here."""
from pathlib import Path


def build_prompt(instruction: str, artifact_dir: Path) -> str:
    # replace rather than format: generated prompts may contain literal JSON.
    return (artifact_dir / "prompt.md").read_text().replace("{instruction}", instruction)


async def prepare_environment(environment, artifact_dir: Path) -> None:
    tools = artifact_dir / "tools"
    if tools.is_dir():
        await environment.upload_dir(tools, "/tmp/rsi-tools")


async def after_run(environment, artifact_dir: Path) -> None:
    """Optional candidate-specific postprocessing inside the task environment."""
