"""Small, backend-neutral values passed between a method and an evaluator."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TargetSpec:
    artifact_dir: Path
    entrypoint: str
    config: dict[str, Any] = field(default_factory=dict)
    model: str | None = None


@dataclass(frozen=True)
class BenchmarkSelection:
    name: str
    version: str
    task_ids: tuple[str, ...] | None = None
    manifest: Path | None = None


@dataclass(frozen=True)
class RunOptions:
    repetitions: int = 1
    concurrency: int = 1
    harbor_executable: str = "harbor"
    environment: str = "docker"
    timeout: float | None = None
    benchmark_runtime: dict[str, Any] = field(default_factory=dict)
    runtime_snapshot: str | None = None
    retain_runtime_images: bool = False


@dataclass
class TrialResult:
    task_id: str
    repetition: int
    status: str
    reward: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    artifact_dir: str = ""
    trajectory_path: str | None = None
    error: str | None = None


@dataclass
class EvaluationResult:
    status: str
    trials: list[TrialResult]
    metrics: dict[str, Any]
    usage: dict[str, Any]
    artifact_dir: str
    resolved: dict[str, Any]
    error: str | None = None
    report_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvaluationResult:
        return cls(**{**value, "trials": [TrialResult(**t) for t in value["trials"]]})
