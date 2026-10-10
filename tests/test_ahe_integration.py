"""Run the actual pinned AHE loop with deterministic external boundaries."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from integrations.ahe.launcher import (Bridge, IntegrationError, PATCH_VERSION, ROOT,
    UPSTREAM_COMMIT, load_upstream, normalize_config, patch_upstream, run,
    validate, validate_manifest, verify_prepared, write_json)
from rsi_eval import EvaluationResult, TrialResult
from rsi_eval.artifacts import artifact_hash


@pytest.fixture
def checkout(tmp_path):
    source = ROOT / "tests/fixtures/ahe_native"
    manifest = json.loads((source / "SOURCE.json").read_text())
    assert manifest["commit"] == UPSTREAM_COMMIT
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == expected
    dst = tmp_path / "checkout"
    shutil.copytree(source, dst)
    (dst / "evolve.py").write_text(patch_upstream((dst / "evolve.py").read_text()))
    write_json(dst / "prepared.json", {"commit": UPSTREAM_COMMIT, "patch_version": PATCH_VERSION,
        "files": {str(p.relative_to(dst)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in dst.rglob("*") if p.is_file()}})
    return dst


@pytest.fixture
def config(tmp_path):
    seed = tmp_path / "seed"
    shutil.copytree(ROOT / "templates/codex_agent", seed)
    return {"codex": {"model": "test-model", "auth_dir": str(tmp_path / "auth"),
                      "expected_version": "test-version"},
            "seed": str(seed), "rounds": 2, "target_pass_rate": None,
            "run_options": {"concurrency": 1, "repetitions": 1},
            "evolve": {"name": "terminal-bench", "version": "2.0", "task_ids": ["extract-elf"]},
            "test": [{"name": "terminal-bench", "version": "2.1", "task_ids": ["extract-elf"]}]}


class External:
    def __init__(self, scores=(0, 1, 0, 1), *, tamper=False, wrong_hash=False,
                 status="completed", unchanged=False, invalid_manifest=False):
        self.scores, self.tamper, self.wrong_hash = scores, tamper, wrong_hash
        self.status, self.unchanged, self.invalid_manifest = status, unchanged, invalid_manifest
        self.evals, self.calls, self.edits = [], [], 0

    def evaluate(self, *, target, benchmark, options, output_dir):
        index = len(self.evals)
        self.evals.append((target, benchmark, options, artifact_hash(target.artifact_dir)))
        native = output_dir / "native/eval" / "extract-elf__abc123"
        (native / "agent").mkdir(parents=True)
        (native / "verifier").mkdir()
        trace = native / "agent/trajectory.json"
        trace.write_text(json.dumps({"events": [{"task": "extract-elf", "observed": "Use ELF section offsets", "iteration": index}]}))
        (native / "verifier/test-stdout.txt").write_text("FAILED header layout" if self.scores[index] == 0 else "PASSED")
        trial = TrialResult("extract-elf", 0, "completed", self.scores[index],
                            input_tokens=30, output_tokens=20, artifact_dir=str(native), trajectory_path=str(trace))
        return EvaluationResult(self.status, [trial], {}, {"input_tokens": 30, "output_tokens": 20},
                                str(output_dir), {"target": {"sha256": "wrong" if self.wrong_hash else artifact_hash(target.artifact_dir)}})

    def codex(self, prompt, *, settings, cwd, mode, log_dir, system):
        self.calls.append({"prompt": prompt, "system": system, "mode": mode})
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "request.txt").write_text(system + "\n" + prompt)
        if mode == "edit":
            self.edits += 1
            if not self.unchanged:
                with (cwd / "agent.py").open("a") as stream:
                    stream.write(f"\n# mechanism change {self.edits}\n")
            if self.tamper:
                (cwd.parent / "evolution_history.md").write_text("forged history")
            write_json(cwd / "change_manifest.json", {"iteration": self.edits, "changes": [{
                "id": f"chg-{self.edits}", "type": "improvement", "description": "Read ELF failure evidence",
                "files": ["agent.py"], "failure_pattern": "Header offsets",
                "predicted_fixes": ["wrong-task"] if self.invalid_manifest else ["extract-elf"],
                "risk_tasks": ["extract-elf"], "constraint_level": "tool_impl",
                "why_this_component": "Execute the parser through environment hooks"}]})
            text = f"Changed mechanism {self.edits}; checked prior feedback."
        else:
            assert "Trace verdict:" in prompt
            assert "Use ELF section offsets" in prompt
            text = "Root cause: header layout. Evidence: original trace. Proposed component: parser hook."
        return SimpleNamespace(text=text, usage={"input_tokens": 10, "output_tokens": 5},
                               cli_version="test-version", codex_cli_calls=1, elapsed_seconds=0.01)


def test_native_two_rounds_attribution_and_best_selection(checkout, config, tmp_path):
    external = External()
    out = tmp_path / "experiment"
    result = run(checkout, config, out, evaluator=external.evaluate, codex_runner=external.codex)
    assert result["status"] == "completed"
    assert result["rounds_evaluated"] == 2
    assert [x["mode"] for x in external.calls] == ["role", "edit", "role", "edit"]
    assert len(external.evals) == 4
    assert len({e[3] for e in external.evals[:3]}) == 3
    assert external.evals[3][3] == external.evals[1][3]
    assert result["best"]["iteration"] == 2
    first = json.loads((out / "runs/iteration_002/input/change_evaluation.json").read_text())
    assert first["change_evaluations"][0]["verdict"] == "EFFECTIVE"
    last = json.loads((out / "runs/iteration_003/input/change_evaluation.json").read_text())
    assert last["change_evaluations"][0]["verdict"] == "HARMFUL"
    assert "chg-1" in external.calls[3]["prompt"]
    assert "Changed mechanism 1" in external.calls[3]["prompt"]
    assert "EFFECTIVE" in external.calls[3]["prompt"]
    assert external.evals[0][1].version == "2.0" and external.evals[3][1].version == "2.1"
    assert result["method_calls"][0]["usage"]["input_tokens"] == 10
    assert result["evaluations"][0]["usage"]["input_tokens"] == 30


def test_slash_task_ids_keep_native_history_debugger_and_attribution(checkout, config, tmp_path, monkeypatch):
    from integrations.ahe import launcher
    # Include names that lossy slugging would conflate, plus path traversal and
    # Unicode; all stay semantic IDs and none become filesystem paths.
    ids = ["engineering/case", "engineering-case", "../case", "研究/任务"]
    config["evolve"]["task_ids"] = ids
    config["run_options"]["repetitions"] = 2
    config["test"] = []
    monkeypatch.setattr(launcher, "resolve", lambda selected: {"tasks": [{"id": task} for task in selected.task_ids]})
    external = External()

    def evaluate(**kwargs):
        result = external.evaluate(**kwargs)
        original = result.trials[0]
        result.trials = [TrialResult(task, repeat, original.status, original.reward,
                         input_tokens=30, output_tokens=20, artifact_dir=original.artifact_dir,
                         trajectory_path=original.trajectory_path) for task in ids for repeat in range(2)]
        return result

    def codex(prompt, **kwargs):
        response = external.codex(prompt, **kwargs)
        if kwargs["mode"] == "edit":
            path = kwargs["cwd"] / "change_manifest.json"
            manifest = json.loads(path.read_text())
            manifest["changes"][0].update(predicted_fixes=ids, risk_tasks=ids)
            write_json(path, manifest)
            assert "filesystem aliases" in prompt
        return response

    out = tmp_path / "experiment"
    result = run(checkout, config, out, evaluator=evaluate, codex_runner=codex)
    assert result["status"] == "completed"
    aliases = json.loads((out / "task_aliases.json").read_text())
    assert set(aliases) == set(ids) and len(set(aliases.values())) == len(ids)
    history = json.loads((out / "task_history.json").read_text())
    assert set(history) == set(ids)
    assert all([row[1] for row in history[task]] == ["fail", "pass", "fail"] for task in ids)
    native = load_upstream(checkout)
    for iteration in (1, 2):
        folder = out / "runs" / f"iteration_{iteration:03d}"
        view = folder / "input/eval/ahe-view"
        if not view.exists():
            view = next(folder.rglob("ahe-view"))
        rows = json.loads((view / "mapping.json").read_text())
        assert {row["task_id"] for row in rows} == set(ids)
        assert len(rows) == 8
        stats = native.compute_stats(view, k=2)
        assert set(stats["task_results"]) == set(ids)
        assert all(value["total"] == 2 for value in stats["per_task_rollouts"].values())
        for task in ids:
            assert native._find_trial_dir(view, task).is_dir()
            detail = folder / "input/analysis/detail" / (aliases[task] + ".md")
            assert detail.is_file() and task in detail.read_text()
        manifest = json.loads((folder / "evolve/change_manifest.json").read_text())
        assert manifest["changes"][0]["predicted_fixes"] == ids
    attribution = json.loads((out / "runs/iteration_002/input/change_evaluation.json").read_text())
    assert attribution["change_evaluations"][0]["verdict"] == "EFFECTIVE"
    assert all(task in json.dumps(attribution, ensure_ascii=False) for task in ids)
    assert not (out / "runs/iteration_001/input/analysis/case.md").exists()


def test_check_covers_all_test_runtime_prerequisites(checkout, config, monkeypatch):
    from integrations.ahe.launcher import preflight
    captured = []
    config["run_options"]["benchmark_runtime"] = {"native_tools": "evolve-tools"}
    config["test"] = [
        {"name": "harvey", "version": "1", "task_ids": ["case/a"], "benchmark_runtime": {"judge": {"model": "judge-a"}}},
        {"name": "frontier-eng", "version": "1", "task_ids": ["case/b"], "benchmark_runtime": {"runtime": "gpu"}},
    ]
    def check(**kwargs):
        captured.append(kwargs)
        return {"ok": len(captured) == 1, "checks": {"prerequisite": {"ok": len(captured) == 1}}}
    monkeypatch.setattr("rsi_eval.evaluation.check", check)
    report = preflight(checkout, config)
    assert not report["ok"] and report["model_calls"] == 0
    assert [(r["role"], r["index"]) for r in report["checks"]] == [("evolve", 0), ("test", 0), ("test", 1)]
    assert [r["options"].benchmark_runtime for r in captured] == [
        {"native_tools": "evolve-tools"}, {"judge": {"model": "judge-a"}}, {"runtime": "gpu"}]


def test_native_ahe_interruption_export_import_resume_without_original_paths(checkout, config, tmp_path, monkeypatch):
    from test_portability import portable_record
    from rsi_eval.portability import export_experiment, import_experiment
    from integrations.ahe.launcher import resume_iteration
    from rsi_eval.codex import CodexQuotaError
    root = tmp_path / "original"
    root.mkdir()
    shutil.copytree(config["seed"], root / "seed")
    shutil.copytree(checkout, root / "checkout")
    config["seed"] = str(root / "seed")
    record = portable_record(root, "ahe", config, monkeypatch)
    external = External()
    def interrupted(prompt, **kwargs):
        if kwargs["mode"] == "edit" and external.edits == 1:
            raise CodexQuotaError("fixture interruption after reading prior feedback")
        return external.codex(prompt, **kwargs)
    with pytest.raises(CodexQuotaError):
        run(root / "checkout", config, root / "run", evaluator=external.evaluate, codex_runner=interrupted)
    original_runtime = artifact_hash(root / "runtime")
    original_candidate = artifact_hash(root / "run/workspace")
    export_experiment(root, tmp_path / "bundle")
    destination = tmp_path / "moved"
    import_experiment(tmp_path / "bundle", destination, Path(record["environment"]), tmp_path / "new-auth")
    assert artifact_hash(destination / "runtime") == original_runtime
    assert artifact_hash(destination / "run/workspace") == original_candidate
    shutil.rmtree(root)
    shutil.rmtree(tmp_path / "bundle")
    moved_config = json.loads((destination / "configuration.json").read_text())
    assert moved_config["seed"] == str(destination / "seed")
    resumed = External(scores=(1, 0, 1))
    resumed.edits = 1
    out = destination / "run"
    result = run(destination / "checkout", moved_config, out,
                 evaluator=resumed.evaluate, codex_runner=resumed.codex, resume_iteration=resume_iteration(out))
    assert result["status"] == "completed" and result["rounds_evaluated"] == 2
    assert result["best"]["iteration"] == 2
    assert "EFFECTIVE" in next(call["prompt"] for call in resumed.calls if call["mode"] == "edit")
    assert all(str(root) not in row["output_dir"] for row in result["evaluations"])
    assert Path(result["selected_candidate"]).is_relative_to(destination)
    assert list((destination / "import-receipts").glob("*/original-controls/run/run_identity.json"))


@pytest.mark.parametrize("kwargs,match", [
    ({"tamper": True}, "immutable"),
    ({"wrong_hash": True}, "different candidate"),
    ({"status": "auth_failed"}, "complete"),
    ({"unchanged": True}, "actual candidate edit"),
    ({"invalid_manifest": True}, "outside the evolve selection"),
])
def test_fail_closed(checkout, config, tmp_path, kwargs, match):
    external = External(**kwargs)
    with pytest.raises(IntegrationError, match=match):
        run(checkout, config, tmp_path / "run", evaluator=external.evaluate, codex_runner=external.codex)
    assert json.loads((tmp_path / "run/summary.json").read_text())["status"] == "failed"


def test_native_rollback_restores_measured_snapshot(checkout, config, tmp_path):
    external = External()
    out = tmp_path / "run"
    run(checkout, config, out, evaluator=external.evaluate, codex_runner=external.codex)
    upstream = load_upstream(checkout)
    assert upstream.perform_auto_rollback(out, out / "workspace", 1)
    assert artifact_hash(out / "workspace") == external.evals[0][3]


def test_guards_and_runtime_path_forwarding(config, tmp_path):
    config["run_options"]["benchmark_runtime"] = {"source_dir": "base-source"}
    config["evolve"]["benchmark_runtime"] = {"source_dir": "evolve-source"}
    config["test"][0]["benchmark_runtime"] = {"source_dir": "test-source"}
    normalized = normalize_config(config, tmp_path)
    assert normalized["run_options"]["benchmark_runtime"]["source_dir"] == str(tmp_path / "base-source")
    assert normalized["evolve"]["benchmark_runtime"]["source_dir"] == str(tmp_path / "evolve-source")
    assert normalized["test"][0]["benchmark_runtime"]["source_dir"] == str(tmp_path / "test-source")
    assert config["run_options"]["benchmark_runtime"]["source_dir"] == "base-source"
    config["evolve"] = {"name": "drop", "version": "1.0-agent"}
    with pytest.raises(IntegrationError, match="binary"):
        validate(config)


def test_pinned_sources_and_auth_logging_removed(checkout):
    verify_prepared(checkout)
    script = (checkout / "evolve.py").read_text()
    assert "load_dotenv" not in script
    assert "current environment variables" not in script
    assert "_rsi_eval_final_iteration" in script
    (checkout / "evolve.py").write_text(script + "\n# changed")
    with pytest.raises(IntegrationError, match="source changed"):
        verify_prepared(checkout)


def test_native_target_stop_is_labelled_without_claiming_requested_rounds(checkout, config, tmp_path):
    config["target_pass_rate"] = 1
    external = External(scores=(1, 1))
    result = run(checkout, config, tmp_path / "run", evaluator=external.evaluate, codex_runner=external.codex)
    assert result["status"] == "stopped_target_reached"
    assert result["rounds_evaluated"] == 0
    assert len(external.evals) == 2 and not external.calls


def test_resume_restores_input_and_archives_aborted_attempt(checkout, config, tmp_path):
    out = tmp_path / "run"
    external = External()
    original = external.codex
    def fail_second_edit(*args, **kwargs):
        if kwargs["mode"] == "edit" and external.edits == 1:
            external.edits += 1
            (kwargs["cwd"] / "agent.py").write_text("invalid Python !!")
            raise RuntimeError("quota fixture")
        return original(*args, **kwargs)
    with pytest.raises(RuntimeError, match="quota fixture"):
        run(checkout, config, out, evaluator=external.evaluate, codex_runner=fail_second_edit)
    failed = json.loads((out / "method_calls.json").read_text())
    assert failed[-1]["status"] == "failed" and failed[-1]["error_type"] == "RuntimeError"
    resumed = External(scores=(1, 0, 1))
    resumed.edits = 1
    result = run(checkout, config, out, evaluator=resumed.evaluate, codex_runner=resumed.codex,
                 resume_iteration=2)
    assert result["status"] == "completed" and result["rounds_evaluated"] == 2
    assert len(result["evaluations"]) == 4
    assert len(result["method_calls"]) == 6
    assert list((out / "resume_history").glob("*/iteration_002"))
    assert "mechanism change 1" in (out / "runs/iteration_002/input/workspace/agent.py").read_text()
    assert "invalid Python" not in (out / "workspace/agent.py").read_text()


def test_existing_completed_run_cannot_be_reused(checkout, config, tmp_path):
    external = External()
    out = tmp_path / "run"
    run(checkout, config, out, evaluator=external.evaluate, codex_runner=external.codex)
    with pytest.raises(IntegrationError, match="already exists"):
        run(checkout, config, out, evaluator=external.evaluate, codex_runner=external.codex)
    with pytest.raises(IntegrationError, match="already completed"):
        run(checkout, config, out, evaluator=external.evaluate, codex_runner=external.codex, resume_iteration=2)


def test_swallowed_debugger_quota_prevents_any_later_model_or_evaluation(checkout, config, tmp_path):
    from rsi_eval.codex import CodexQuotaError
    from unittest.mock import patch
    native = load_upstream(checkout)
    original_debugger = native.run_parallel_adb_ask
    def swallow(**kwargs):
        try:
            return original_debugger(**kwargs)
        except CodexQuotaError:
            return "[adb exception] quota"
    native.run_parallel_adb_ask = swallow
    external = External()
    calls = []
    def quota(*args, **kwargs):
        calls.append(kwargs["mode"])
        raise CodexQuotaError("quota fixture")
    with patch("integrations.ahe.launcher.load_upstream", return_value=native):
        with pytest.raises(CodexQuotaError):
            run(checkout, config, tmp_path / "run", evaluator=external.evaluate, codex_runner=quota)
    assert calls == ["role"]
    assert len(external.evals) == 1
    assert json.loads((tmp_path / "run/summary.json").read_text())["status"] == "quota_exceeded"


def test_target_auth_error_has_own_status(checkout, config, tmp_path):
    from rsi_eval.codex import CodexAuthError
    external = External()
    def auth(**kwargs):
        result = external.evaluate(**kwargs)
        result.status = "failed"
        result.trials[0].status = "auth_error"
        return result
    with pytest.raises(CodexAuthError):
        run(checkout, config, tmp_path / "run", evaluator=auth, codex_runner=external.codex)
    assert not external.calls
    assert json.loads((tmp_path / "run/summary.json").read_text())["status"] == "auth_error"


def test_method_summary_is_archived_without_counting_as_candidate_edit(checkout, config, tmp_path):
    external = External()
    original = external.codex
    def codex(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["mode"] == "edit":
            (kwargs["cwd"] / "evolve_summary.md").write_text("Native method deliverable")
            assert f"`{tmp_path / 'run' / 'runs'}/iteration_" in args[0]
        return result
    out = tmp_path / "run"
    result = run(checkout, config, out, evaluator=external.evaluate, codex_runner=codex)
    assert result["status"] == "completed"
    for i in (1, 2):
        evolve = out / "runs" / f"iteration_{i:03d}" / "evolve"
        assert (evolve / "proposal_summary.md").read_text() == "Native method deliverable"
        assert json.loads((evolve / "proposed_manifest.json").read_text())["iteration"] == i
        assert json.loads((evolve / "edit_evidence.json").read_text())["changed_files"] == ["agent.py"]
    assert not (out / "workspace/evolve_summary.md").exists()
    assert not (out / "selected_candidate/evolve_summary.md").exists()


def test_rejected_manifest_survives_validation(checkout, config, tmp_path):
    external = External(invalid_manifest=True)
    out = tmp_path / "run"
    with pytest.raises(IntegrationError):
        run(checkout, config, out, evaluator=external.evaluate, codex_runner=external.codex)
    preserved = out / "runs/iteration_001/evolve/proposed_manifest.json"
    assert preserved.is_file()
    assert json.loads(preserved.read_text())["changes"][0]["predicted_fixes"] == ["wrong-task"]


def test_native_keep_decision_is_valid_but_not_a_second_changed_candidate(checkout, config, tmp_path):
    external = External(scores=(0, 1, 1, 1))
    original = external.codex
    def keep(*args, **kwargs):
        if kwargs["mode"] == "edit" and external.edits == 1:
            external.calls.append({"prompt": args[0], "system": kwargs["system"], "mode": "edit"})
            assert "EFFECTIVE" in args[0]
            write_json(kwargs["cwd"] / "change_manifest.json", {"iteration": 2, "changes": []})
            (kwargs["cwd"] / "evolve_summary.md").write_text("KEEP effective change: no remaining failure evidence")
            return SimpleNamespace(text="Keep incumbent", usage={}, cli_version="test-version",
                                   codex_cli_calls=1, elapsed_seconds=0.01)
        return original(*args, **kwargs)
    out = tmp_path / "run"
    result = run(checkout, config, out, evaluator=external.evaluate, codex_runner=keep)
    assert result["status"] == "completed"
    assert result["rounds_evaluated"] == 2
    assert result["changed_rounds"] == 1 and result["noop_rounds"] == 1
    assert result["acceptance"]["two_changed_rounds"] is False
    assert result["acceptance_passed"] is False
    assert external.evals[1][3] == external.evals[2][3] == external.evals[3][3]
    decision = json.loads((out / "runs/iteration_002/evolve/edit_evidence.json").read_text())
    assert decision["decision"] == "keep" and decision["changed"] is False
    attribution = json.loads((out / "runs/iteration_003/input/change_evaluation.json").read_text())
    assert attribution["change_evaluations"] == []


def test_export_rejects_best_snapshot_that_was_not_measured(checkout, config, tmp_path):
    external = External()
    def corrupt(**kwargs):
        result = external.evaluate(**kwargs)
        if len(external.evals) == 3:
            (tmp_path / "run/runs/iteration_002/input/workspace/prompt.md").write_text("changed after measurement")
        return result
    with pytest.raises(IntegrationError, match="Best snapshot differs"):
        run(checkout, config, tmp_path / "run", evaluator=corrupt, codex_runner=external.codex)
    assert len(external.evals) == 3
    assert not (tmp_path / "run/selected_candidate").exists()


def test_explicit_binary_mapping_and_selection_keep_native_results(checkout, config, tmp_path):
    policy = tmp_path / "objective.py"
    policy.write_text("def choose(payload):\n    return max(payload['candidates'], key=lambda row: int(row['id']))['id']\n")
    config["method_policy"] = {"variant": "latest-threshold-variant",
        "score": {"kind": "threshold", "threshold": .5},
        "selection": {"kind": "python", "path": str(policy), "function": "choose"}}
    external = External(scores=(.2, .7, .9, .9))
    out = tmp_path / "experiment"
    result = run(checkout, config, out, evaluator=external.evaluate, codex_runner=external.codex)
    assert result["best"]["iteration"] == 3
    assert external.evals[-1][3] == external.evals[2][3]
    assert result["method_policy"]["variant"] == "latest-threshold-variant"
    evaluation = Path(result["evaluations"][1]["output_dir"])
    native = json.loads((evaluation / "integration_result.json").read_text())
    mapped = json.loads((evaluation / "method_scores.json").read_text())
    assert native["trials"][0]["reward"] == .7
    assert mapped["trials"][0]["mapped_score"] == 1
    final = json.loads((out / "runs/iteration_003/method_selection.json").read_text())
    assert final["native_choice"]["iteration"] == 2
    assert final["selected"]["iteration"] == 3


def test_resume_infers_latest_managed_snapshot_and_refuses_completed(tmp_path):
    from integrations.ahe.launcher import resume_iteration
    output = tmp_path / "run"
    write_json(output / "run_identity.json", {})
    for number in (1, 2):
        (output / "runs" / f"iteration_{number:03d}/input/workspace").mkdir(parents=True)
    write_json(output / "summary.json", {"status": "quota_exceeded"})
    assert resume_iteration(output) == 2
    write_json(output / "summary.json", {"status": "completed"})
    with pytest.raises(IntegrationError, match="already completed"):
        resume_iteration(output)
