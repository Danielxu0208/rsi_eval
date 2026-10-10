"""Evaluation and evidence adapter; the upstream RRSI algorithm owns search."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from rrsi.domain import Domain
from rrsi.evaluate import TaskResult

from .failures import stop_run
from .prepare import validate_evolve_scoring, verify_frozen_selections
from rsi_eval.method_policy import normalize_policy, policy_identity, score_trial
from integrations.runtime_options import effective_options


def tree_digest(directory: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            h.update(str(path.relative_to(directory)).encode())
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def selection_from_dict(data: dict):
    from rsi_eval import BenchmarkSelection
    return BenchmarkSelection(
        name=data["name"], version=data["version"],
        task_ids=tuple(data["task_ids"]) if data.get("task_ids") is not None else None,
        manifest=Path(data["manifest"]) if data.get("manifest") else None,
    )


def options_from_dict(data: dict, repetitions: int, selection: dict | None = None):
    from rsi_eval import RunOptions
    return RunOptions(**{**effective_options(data, selection), "repetitions": repetitions})


def target_from_config(harness_dir: Path, config: dict):
    from rsi_eval import TargetSpec
    return TargetSpec(
        artifact_dir=harness_dir,
        entrypoint="rsi_eval.harbor_codex:Agent",
        model=config["policy_model"],
        config={
            "codex_auth_dir": config["codex"]["auth_dir"],
            "codex_version": config["codex"].get("expected_version"),
            "reasoning_effort": config["codex"].get("reasoning_effort", "medium"),
            **config.get("target_config", {}),
        },
    )


class CodexDomain(Domain):
    name = "coding_codex"
    harness_path = "harness"
    source_exts = {".py", ".md", ".txt", ".json", ".sh"}
    briefs = {
        role: (
            "A frozen Codex CLI policy performs repository and terminal tasks. "
            "The editable candidate provides agent.py build_prompt(instruction, artifact_dir), "
            "prompt.md, optional async prepare_environment/after_run hooks, and tools/. "
            "Only hooks and files actually used by that candidate affect behavior. "
            "The trusted evaluator controls task selection, model, credentials and verifier. "
            "Use raw execution evidence to identify reusable mechanisms, never task answers."
        ) for role in ("analyst", "digester", "proposer", "critic")
    }
    critic_patterns = [
        (r"/tests\b|/solution\b|/logs/verifier", "verifier or oracle path in diff"),
        (r"\breward\.(?:txt|json)\b|test_outputs|task\.toml", "grading artifact in diff"),
        (r"auth\.json|codex_auth_dir|OPENAI_API_KEY|CODEX_API_KEY", "authentication outside the editable interface"),
    ]
    component_signals = [
        ("client_tool", [r"tools/", r"register_tool", r"ToolRegistry"]),
        ("skill", [r"skills/", r"SkillRegistry"]),
        ("memory", [r"\bMemory\(", r"\.remember\(", r"\.recall\("]),
        ("subagent", [r"\bsubcall\(", r"subagent", r"sub_agent"]),
        ("context_mgmt", [r"summari[sz]", r"context", r"compress"]),
        ("output_plumbing", [r"truncat", r"output_limit", r"after_run"]),
        ("control_flow", [r"prepare_environment", r"\b(?:while|for|if|try)\b"]),
        ("config", [r"config\.json", r"threshold", r"max_bytes"]),
        ("prompt", [r"prompt\.md", r"build_prompt", r"prompt"]),
    ]

    def __init__(self, domain_root: Path):
        self.root = domain_root
        from rsi_eval.relocation import relocate
        self.config = relocate(json.loads((domain_root / "experiment.json").read_text()), domain_root)
        ids = self.config["evolve"].get("task_ids")
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("RRSI evolve.task_ids must be a nonempty, unique explicit list")
        if self.config.get("method_policy"):
            self.config["method_policy"] = normalize_policy(self.config["method_policy"], domain_root,
                                                          candidate_roots=[domain_root / "harness"])
        verify_frozen_selections(self.config)
        validate_evolve_scoring(self.config["evolve"], self.config.get("method_policy"))
        self._ids = list(ids)
        self.critic_patterns = list(self.critic_patterns) + [
            (rf"(?<![\w-]){re.escape(task)}(?![\w-])", f"evolve task id {task!r} in diff")
            for task in self._ids
        ]

    def evolve_ids(self):
        return list(self._ids)

    def heldout_ids(self):
        # Cross-benchmark tests use the integration's `test` command, which
        # submits the frozen incumbent to the caller's independent selection.
        return []

    def smoke_ids(self, incumbent_per_task=None):
        # smoke() uses its separately configured selection and executes a real
        # task after checking the candidate's Python interface.
        return []

    def run(self, root, runs_dir, job, ids, k, log_prefix=""):
        from rsi_eval import evaluate

        root, runs_dir = Path(root), Path(runs_dir)
        if list(ids) != self._ids:
            raise ValueError("Evolve task set changed; start a separate RRSI run")
        harness = self.harness_dir(root)
        job_dir = runs_dir / "jobs" / job
        job_dir.mkdir(parents=True, exist_ok=True)
        output = job_dir / "evaluation"
        request = {
            "candidate_sha256": tree_digest(harness), "evolve": self.config["evolve"],
            "method_policy": policy_identity(self.config.get("method_policy")),
            "selection_provenance": self.config.get("selection_provenance", {}),
            "policy_model": self.config["policy_model"], "k": k,
            "options": effective_options(self.config.get("run_options", {}), self.config["evolve"]),
            "target_config": self.config.get("target_config", {}),
            "codex_version": self.config["codex"].get("expected_version"),
            "reasoning_effort": self.config["codex"].get("reasoning_effort", "medium"),
        }
        old_request = job_dir / "request.json"
        result_file = output / "result.json"
        if result_file.exists() and old_request.exists():
            from rsi_eval.relocation import read_json
            previous = read_json(result_file)
            if read_json(old_request) == request and previous.get("status") == "completed":
                return
        if output.exists():
            output.rename(job_dir / f"evaluation.stale.{time.time_ns()}")
        old_request.write_text(json.dumps(request, indent=2))
        result = evaluate(
            target=target_from_config(harness, self.config),
            benchmark=selection_from_dict(self.config["evolve"]),
            options=options_from_dict(self.config.get("run_options", {}), k, self.config["evolve"]),
            output_dir=output,
        )
        output.mkdir(parents=True, exist_ok=True)
        result_file.write_text(json.dumps(result.to_dict(), indent=2))
        fatal = next((t.status for t in result.trials if t.status in ("auth_error", "quota_exceeded")), None)
        if fatal:
            stop_run(runs_dir, fatal, f"evaluation {job}")

    @staticmethod
    def _report(runs_dir, job):
        from rsi_eval.relocation import read_json
        path = Path(runs_dir) / "jobs" / job / "evaluation" / "result.json"
        return read_json(path) if path.exists() else {}

    @classmethod
    def _records(cls, runs_dir, job):
        return cls._report(runs_dir, job).get("trials", [])

    def _grouped(self, runs_dir, job, ids, k):
        grouped = {task: {} for task in ids}
        for rec in self._records(runs_dir, job):
            task = rec["task_id"]
            if task not in grouped:
                matches = [x for x in ids if x.rsplit("/", 1)[-1] == task.rsplit("/", 1)[-1]]
                if len(matches) != 1:
                    raise ValueError(f"Unexpected/ambiguous task {task!r} in evaluation")
                task = matches[0]
            rep = rec["repetition"]
            if not isinstance(rep, int) or not 0 <= rep < k or rep in grouped[task]:
                raise ValueError(f"Duplicate or invalid repetition for {task}: {rep}")
            grouped[task][rep] = rec
        return grouped

    def score(self, runs_dir, job, ids, k):
        report = self._report(runs_dir, job)
        extra = {"benchmark": self.config["evolve"],
                 "candidate_request": str(Path(runs_dir) / "jobs" / job / "request.json"),
                 "evaluation_status": report.get("status", "missing"),
                 "method_policy": policy_identity(self.config.get("method_policy"))}
        if report.get("status") != "completed":
            # A failed job can still contain successful individual trial files.
            # Invalidate the whole measurement so upstream retries infrastructure
            # once and then applies eval_invalid, without selecting a partial job.
            return {task: TaskResult(rewards=[0.0] * k, tokens=[None] * k, missing=k)
                    for task in ids}, extra
        grouped = self._grouped(runs_dir, job, ids, k)
        per = {}
        for task, records in grouped.items():
            rewards, tokens, missing = [], [], 0
            for rep in range(k):
                rec = records.get(rep)
                if (rec is None or rec["status"] not in ("completed", "task_timeout", "agent_error")
                        or rec.get("reward") is None):
                    rewards.append(0.0)
                    tokens.append(None)
                    missing += 1
                    continue
                reward = score_trial(self.config.get("method_policy"), rec, self.config["evolve"], unit_interval=True)
                counts = (rec.get("input_tokens"), rec.get("output_tokens"))
                if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts) or sum(counts) <= 0:
                    raise ValueError(f"Missing policy token usage for {task}/{rep}; cannot apply RRSI cost rule")
                rewards.append(float(reward))
                tokens.append(sum(counts))
            per[task] = TaskResult(rewards=rewards, tokens=tokens, missing=missing,
                                   extra={"raw_trials": list(records.values()), "method_policy": extra["method_policy"]})
        if self.config.get("method_policy"):
            policy_report = Path(runs_dir) / "jobs" / job / "method_scores.json"
            policy_report.write_text(json.dumps({"method_policy": extra["method_policy"],
                "trials": [{"task_id": task, "mapped_scores": tr.rewards,
                            "raw_trials": tr.extra["raw_trials"]} for task, tr in per.items()]}, indent=2))
        return per, extra

    def load_trial(self, runs_dir, job, task_id, trial):
        # Native build_traces passes the index in the ordered rewards list.
        from rsi_eval.relocation import read_json
        request = read_json(Path(runs_dir) / "jobs" / job / "request.json")
        grouped = self._grouped(runs_dir, job, self._ids, request["k"])
        rec = grouped.get(task_id, {}).get(trial)
        return {**rec, "task_id": task_id} if rec else None

    def render_trace(self, rec, detail=False):
        text = json.dumps(rec, ensure_ascii=False, indent=2)
        trajectory = rec.get("trajectory_path")
        if trajectory and Path(trajectory).is_file():
            text += "\n\n=== NATIVE TRAJECTORY ===\n" + Path(trajectory).read_text(errors="replace")
        elif rec.get("artifact_dir"):
            artifact = Path(rec["artifact_dir"])
            paths = sorted(artifact.glob("*.json")) + sorted(artifact.glob("*.jsonl"))
            for path in paths:
                text += f"\n\n=== {path.name} ===\n" + path.read_text(errors="replace")
        return text if detail else text[:150_000]

    def task_row(self, task_id, rec, tr):
        return f"{task_id} | rewards={tr.rewards if tr else '?'} | tokens={tr.tokens if tr else '?'} | status={rec.get('status')}"

    def smoke(self, root, runs_dir, job, ids):
        from rsi_eval import evaluate

        harness = self.harness_dir(Path(root))
        script = '''import importlib.util, pathlib, sys
p = pathlib.Path(sys.argv[1])
for f in p.rglob('*.py'):
    compile(f.read_text(), str(f), 'exec')
spec = importlib.util.spec_from_file_location('rrsi_candidate_smoke', p / 'agent.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert callable(module.build_prompt)
prompt = module.build_prompt('Inspect the working directory.', p)
assert isinstance(prompt, str) and prompt.strip()
for name in ('prepare_environment', 'after_run'):
    if hasattr(module, name): assert callable(getattr(module, name))
print('RRSI_INTERFACE_OK')
'''
        import os
        check = subprocess.run([sys.executable, "-B", "-c", script, str(harness)],
                               cwd=harness, capture_output=True, text=True, timeout=30,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        ok = check.returncode == 0 and "RRSI_INTERFACE_OK" in check.stdout
        if not ok:
            return False, {"stage": "candidate_interface", "detail": (check.stdout + check.stderr)[-3000:]}
        output = Path(runs_dir) / "jobs" / job / "evaluation"
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.rename(output.with_name(f"evaluation.stale.{time.time_ns()}"))
        result = evaluate(
            target=target_from_config(harness, self.config),
            benchmark=selection_from_dict(self.config["smoke"]),
            options=options_from_dict({**self.config.get("run_options", {}), "concurrency": 1}, 1, self.config["smoke"]),
            output_dir=output,
        )
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(json.dumps(result.to_dict(), indent=2))
        fatal = next((t.status for t in result.trials if t.status in ("auth_error", "quota_exceeded")), None)
        if fatal:
            stop_run(Path(runs_dir), fatal, f"smoke {job}")
        valid = (result.status == "completed" and len(result.trials) == len(self.config["smoke"]["task_ids"])
                 and all(t.status == "completed" for t in result.trials))
        return valid, {"stage": "real_task_liveness", "report": str(output / "result.json"),
                       "statuses": [t.status for t in result.trials],
                       "candidate_sha256": tree_digest(harness),
                       "note": "Verifier reward need not be one; smoke is separate from scored evaluation."}
