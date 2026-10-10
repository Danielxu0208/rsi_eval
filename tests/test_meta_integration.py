"""Exercise the actual pinned native search loop with fake external processes."""
from __future__ import annotations

import ast
import asyncio
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.meta_harness.launcher import (
    EXAMPLE, PROJECT_ROOT, UPSTREAM_COMMIT, IntegrationError,
    prepare_checkout, run_experiment, audit_experiment,
)


@pytest.fixture
def template(tmp_path):
    directory = tmp_path / "template"
    directory.mkdir()
    (directory / "agent.py").write_text(
        "def build_prompt(instruction, artifact_dir):\n"
        "    return (artifact_dir / 'prompt.md').read_text().format(instruction=instruction)\n"
    )
    (directory / "prompt.md").write_text("Solve the task: {instruction}\n")
    return directory


def config():
    return {"iterations": 2,
            "evolve": {"name": "terminal-bench", "version": "2.1", "task_ids": ["cancel-async-tasks"]},
            "test": {"name": "swe-bench-verified", "version": "1", "task_ids": ["astropy__astropy-12907"]}}


class FakeExternal:
    """Write native Harbor-shaped outputs; no network, model or containers."""
    def __init__(self, *, fail_phase=None, unchanged=False, tamper=False, no_candidates=False,
                 evidence_tamper=None, measured_phase=None, measured_state="task_timeout"):
        self.calls = []
        self.prompts = []
        self.fail_phase = fail_phase
        self.unchanged = unchanged
        self.tamper = tamper
        self.no_candidates = no_candidates
        self.evidence_tamper = evidence_tamper
        self.measured_phase = measured_phase
        self.measured_state = measured_state

    def codex(self, prompt, *, cwd, settings, mode, log_dir):
        assert mode == "edit"
        self.prompts.append(prompt)
        iteration = int(re.search(r"Run iteration (\d+)\.", prompt).group(1))
        log_dir.mkdir(parents=True)
        (log_dir / "events.jsonl").write_text('{"type": "fake_session"}\n')
        if self.no_candidates:
            return SimpleNamespace(usage={})
        parent = cwd / "agents/baseline_codex"
        candidate = cwd / "agents" / f"candidate_{iteration}"
        shutil.copytree(parent, candidate)
        if not self.unchanged:
            with (candidate / "prompt.md").open("a") as f:
                f.write(f"General mechanism {iteration}: inspect errors and verify changes.\n")
        if self.tamper:
            (parent / "prompt.md").write_text("tampered")
        if self.evidence_tamper == "result":
            next((cwd / "jobs").rglob("result.json")).write_text('{"reward": 1}')
        elif self.evidence_tamper == "trajectory":
            next((cwd / "jobs").rglob("trajectory.json")).write_text('{"forged": true}')
        elif self.evidence_tamper == "new":
            (cwd / "jobs/forged-evidence.json").write_text('{}')
        pending = cwd / "logs/search/pending_eval.json"
        pending.write_text(json.dumps({"iteration": iteration, "candidates": [{
            "name": f"candidate_{iteration}",
            "import_path": f"agents.candidate_{iteration}:AgentHarness",
            "hypothesis": "General feedback-directed change", "changes": "prompt",
        }]}))
        return SimpleNamespace(usage={"input_tokens": 10, "output_tokens": 20})

    def evaluate(self, *, target, benchmark, options, output_dir):
        selection, output = benchmark, output_dir
        phase = "test" if "final-test" in str(output) else "smoke" if "smoke-" in str(output) else "evolve"
        self.calls.append((phase, target, selection, options))
        native = output / "native/eval"
        native.mkdir(parents=True)
        trials = []
        is_error = phase == self.fail_phase
        reward = 1.0 if target.artifact_dir.name == "candidate_1" else 0.0
        for task in selection.task_ids:
            trial_dir = native / f"{task}__fixture"
            trial_dir.mkdir()
            (trial_dir / "result.json").write_text(json.dumps({
                "task_name": task, "verifier_result": {"rewards": {"reward": reward}},
                "agent_result": {"n_input_tokens": 100, "n_output_tokens": 20},
            }))
            (trial_dir / "agent").mkdir()
            (trial_dir / "agent/trajectory.json").write_text('{"steps": []}')
            status = "infra_error" if is_error else self.measured_state if phase == self.measured_phase else "completed"
            trials.append(SimpleNamespace(status=status, reward=None if is_error else reward))
        data = {"status": "failed" if is_error else "completed", "artifact_dir": str(native),
                "trials": [{"status": t.status, "reward": t.reward} for t in trials]}
        from rsi_eval.artifacts import artifact_hash
        data["resolved"] = {"target": {"sha256": artifact_hash(target.artifact_dir)}}
        (output / "result.json").write_text(json.dumps(data))
        return SimpleNamespace(**{**data, "trials": trials}, to_dict=lambda: data)


def run(tmp_path, template, external, cfg=None, **kwargs):
    settings = SimpleNamespace(model="test-model", auth_dir=tmp_path / "auth",
                               expected_version="test-version", reasoning_effort="medium", timeout_seconds=60)
    return run_experiment(cfg or config(), tmp_path / "run", template=template,
                          settings=settings, codex_fn=external.codex,
                          evaluate_fn=external.evaluate,
                          sdk_types=(SimpleNamespace, SimpleNamespace, SimpleNamespace), **kwargs)


def test_real_upstream_loop_two_changed_candidates_smoke_and_test(tmp_path, template):
    external = FakeExternal()
    result = run(tmp_path, template, external)
    assert result["status"] == "completed"
    assert result["winner"] == "candidate_1"  # native strict frontier, not last candidate
    assert [item[0] for item in external.calls] == ["evolve", "smoke", "evolve", "smoke", "evolve", "test"]
    assert len(external.prompts) == 2
    assert "astropy__astropy-12907" not in "\n".join(external.prompts)
    assert all(item[3].repetitions == 1 and item[3].concurrency == 1 for item in external.calls)
    assert external.calls[-1][1].artifact_dir.name == "candidate_1"
    assert external.calls[-1][2].name == "swe-bench-verified"
    rows = [json.loads(line) for line in Path(result["history"]).read_text().splitlines()]
    assert [row["iteration"] for row in rows] == [1, 2]
    assert [row["avg_pass_rate"] for row in rows] == [1.0, 0.0]
    assert rows[0]["rollout_metrics"]["total_input_tokens"] == 100


@pytest.mark.parametrize("no_candidates", [False, True])
def test_unified_independent_test_loads_native_selected_candidate(tmp_path, template, no_candidates):
    from rsi_eval.method_test import selected_target
    external = FakeExternal(no_candidates=no_candidates)
    result = run(tmp_path, template, external)
    record = {"method": "meta-harness", "experiment": str(tmp_path), "configuration": {
        **config(), "codex": {"model": "test-model", "auth_dir": str(tmp_path / "auth"), "expected_version": "test-version"}}}
    target, _, evidence = selected_target(record)
    assert target.artifact_dir == Path(result["winner_artifact"])
    assert evidence["winner"] == result["winner"]
    (target.artifact_dir / "prompt.md").write_text("changed after native measurement")
    with pytest.raises(ValueError, match="changed after measurement"):
        selected_target(record)


@pytest.mark.parametrize("name", ["drop", "frontier-eng"])
def test_continuous_smoke_and_final_test_do_not_change_native_selection(tmp_path, template, name):
    cfg = config()
    cfg["smoke"] = {"name": name, "version": "fixture", "task_ids": ["continuous-smoke"]}
    cfg["test"] = {"name": name, "version": "fixture", "task_ids": ["continuous-test"]}
    external = FakeExternal()
    result = run(tmp_path, template, external, cfg)
    assert result["status"] == "completed"
    assert result["winner"] == "candidate_1"
    continuous_calls = [call for call in external.calls if call[2].name == name]
    assert [call[0] for call in continuous_calls] == ["smoke", "smoke", "test"]


def test_managed_patch_preserves_search_history_frontier_functions(tmp_path):
    source = PROJECT_ROOT / "meta-harness"
    before = subprocess.run(["git", "-C", str(source), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
    example = prepare_checkout(source, tmp_path / "checkout")
    original = subprocess.run(["git", "-C", str(source), "show", f"{UPSTREAM_COMMIT}:{EXAMPLE}/meta_harness.py"], capture_output=True, text=True, check=True).stdout
    orig_functions = {node.name: ast.dump(node) for node in ast.parse(original).body if isinstance(node, ast.FunctionDef)}
    patched_functions = {node.name: ast.dump(node) for node in ast.parse((example / "meta_harness.py").read_text()).body if isinstance(node, ast.FunctionDef)}
    assert orig_functions == patched_functions
    after = subprocess.run(["git", "-C", str(source), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
    assert before == after


@pytest.mark.parametrize("phase", ["evolve", "smoke", "test"])
def test_infrastructure_failure_is_not_a_zero_score_or_success(tmp_path, template, phase):
    with pytest.raises(IntegrationError, match="did not complete"):
        run(tmp_path, template, FakeExternal(fail_phase=phase))
    assert json.loads((tmp_path / "run/result.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("state", ["auth_error", "quota_exceeded"])
@pytest.mark.parametrize("failure_call,proposals", [(1, 0), (2, 1), (3, 1), (6, 2)])
def test_auth_or_quota_stops_native_loop_and_preserves_progress(tmp_path, template, state,
                                                              failure_call, proposals):
    class External(FakeExternal):
        def evaluate(self, **kwargs):
            result = super().evaluate(**kwargs)
            if len(self.calls) == failure_call:
                data = result.to_dict()
                data["status"] = "failed"
                data["trials"] = [{"status": state, "reward": None}]
                (kwargs["output_dir"] / "result.json").write_text(json.dumps(data))
                return SimpleNamespace(**{**data, "trials": [SimpleNamespace(**data["trials"][0])]},
                                       to_dict=lambda: data)
            return result

    external = External()
    with pytest.raises(IntegrationError, match="did not complete"):
        run(tmp_path, template, external)
    assert len(external.calls) == failure_call
    assert len(external.prompts) == proposals
    progress = json.loads((tmp_path / "run/evaluations.json").read_text())
    assert len(progress) == failure_call
    assert progress[-1]["result"]["trials"] == [{"status": state, "reward": None}]
    assert json.loads((tmp_path / "run/result.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("error_name", ["CodexAuthError", "CodexQuotaError"])
def test_proposer_auth_or_quota_stops_without_another_model_call(tmp_path, template, error_name):
    from rsi_eval import codex

    error = getattr(codex, error_name)

    class External(FakeExternal):
        def codex(self, prompt, **kwargs):
            self.prompts.append(prompt)
            raise error("dedicated login unavailable")

    external = External()
    with pytest.raises(error):
        run(tmp_path, template, external)
    assert len(external.prompts) == 1
    assert [call[0] for call in external.calls] == ["evolve"]
    assert len(json.loads((tmp_path / "run/evaluations.json").read_text())) == 1
    assert json.loads((tmp_path / "run/result.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("options", [{"unchanged": True}, {"no_candidates": True}])
def test_noop_or_missing_candidate_does_not_satisfy_acceptance(tmp_path, template, options):
    external = FakeExternal(**options)
    result = run(tmp_path, template, external)
    assert result["status"] == "completed"
    assert result["acceptance_passed"] is False
    assert result["winner"] == "codex-baseline"
    assert [call[0] for call in external.calls] == ["evolve", "test"]
    assert len(result["round_outcomes"]) == 2
    with pytest.raises(IntegrationError, match="Integration acceptance failed"):
        audit_experiment(tmp_path / "run")
    before = len(external.calls), len(external.prompts)
    assert run(tmp_path, template, external, resume=True) == result
    assert (len(external.calls), len(external.prompts)) == before


def test_previous_candidate_is_immutable(tmp_path, template):
    with pytest.raises(IntegrationError, match="immutable"):
        run(tmp_path, template, FakeExternal(tamper=True))


@pytest.mark.parametrize("kind", ["result", "trajectory", "new"])
def test_proposer_cannot_mutate_or_create_native_feedback(tmp_path, template, kind):
    with pytest.raises(IntegrationError, match="evaluation evidence"):
        run(tmp_path, template, FakeExternal(evidence_tamper=kind))


@pytest.mark.parametrize("phase", ["evolve", "test"])
@pytest.mark.parametrize("state", ["task_timeout", "agent_error"])
def test_native_measured_failures_keep_their_real_scores(tmp_path, template, phase, state):
    result = run(tmp_path, template, FakeExternal(measured_phase=phase, measured_state=state))
    assert result["status"] == "completed"
    assert result["winner"] == "candidate_1"


@pytest.mark.parametrize("state", ["task_timeout", "agent_error"])
def test_smoke_retains_native_liveness_requirement(tmp_path, template, state):
    external = FakeExternal(measured_phase="smoke", measured_state=state)
    result = run(tmp_path, template, external)
    assert result["status"] == "completed" and not result["acceptance_passed"]
    assert result["winner"] == "codex-baseline"
    assert len(external.prompts) == 2
    assert [call[0] for call in external.calls] == ["evolve", "smoke", "smoke", "test"]


def test_resume_after_no_proposal_round_uses_next_native_iteration(tmp_path, template):
    class External(FakeExternal):
        stopped = False
        def codex(self, prompt, **kwargs):
            if "Run iteration 2." in prompt and not self.stopped:
                self.stopped = True
                raise KeyboardInterrupt("stop before second round")
            return super().codex(prompt, **kwargs)
    external = External(no_candidates=True)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, template, external)
    result = run(tmp_path, template, external, resume=True)
    assert result["status"] == "completed" and not result["acceptance_passed"]
    assert [int(re.search(r"Run iteration (\d+)", prompt).group(1)) for prompt in external.prompts] == [1, 2]
    assert [row["iteration"] for row in result["round_outcomes"]] == [1, 2]


def test_imported_meta_partial_run_reuses_native_history_and_measured_candidate(tmp_path, template, monkeypatch):
    from test_portability import portable_record
    from rsi_eval.portability import export_experiment, import_experiment
    from integrations.meta_harness.launcher import normalize_config
    root = tmp_path / "experiment"
    external = ResumeExternal(stop_call=6)  # Interrupted final test; search has settled.
    cfg = config()
    cfg["codex"] = {"model": "test-model", "auth_dir": str(tmp_path / "auth"), "expected_version": "test-version"}
    def invoke(directory, auth, resume=False):
        options = SimpleNamespace(model="test-model", auth_dir=auth, expected_version="test-version", reasoning_effort="medium", timeout_seconds=60)
        current = {**cfg, "codex": {**cfg["codex"], "auth_dir": str(auth)}}
        return run_experiment(current, directory / "run", template=template, settings=options,
            codex_fn=external.codex, evaluate_fn=external.evaluate,
            sdk_types=(SimpleNamespace, SimpleNamespace, SimpleNamespace), resume=resume)
    with pytest.raises(KeyboardInterrupt):
        invoke(root, tmp_path / "auth")
    record = portable_record(root, "meta-harness", normalize_config(cfg, Path.cwd()), monkeypatch)
    bundle, destination = tmp_path / "bundle", tmp_path / "moved"
    export_experiment(root, bundle)
    import_experiment(bundle, destination, Path(record["environment"]), tmp_path / "new-auth")
    shutil.rmtree(root)
    result = invoke(destination, tmp_path / "new-auth", resume=True)
    assert result["status"] == "completed" and result["winner"] == "candidate_1"
    assert len(external.prompts) == 2
    assert len(external.calls) == 7  # Only the interrupted final-test invocation is retried.
    assert [call[0] for call in external.calls].count("evolve") == 3
    assert str(destination) in result["winner_artifact"]


def test_mixed_evolve_sets_have_distinct_native_frontier_names(tmp_path, template):
    cfg = config()
    cfg["evolve"] = [cfg["evolve"], {"name": "terminal-bench", "version": "2.0", "task_ids": ["cancel-async-tasks"]}]
    result = run(tmp_path, template, FakeExternal(), cfg)
    keys = [key for key in result["frontier"] if key != "_best"]
    assert len(keys) == 2
    assert any("2.0" in key for key in keys)
    assert any("2.1" in key for key in keys)


def test_example_task_ids_resolve_against_pinned_catalog():
    catalog = json.loads((PROJECT_ROOT / "src/rsi_eval/data/catalog.json").read_text())
    for file in (PROJECT_ROOT / "integrations/meta_harness/configs").glob("*.json"):
        cfg = json.loads(file.read_text())
        selections = []
        for key in ("evolve", "test"):
            selections.extend(cfg[key] if isinstance(cfg[key], list) else [cfg[key]])
        for selection in selections:
            benchmark = next(row for row in catalog if (row["name"], row["version"]) == (selection["name"], selection["version"]))
            assert set(selection["task_ids"]) <= {task["id"] for task in benchmark["tasks"]}


def test_launcher_calls_real_sdk_with_fake_harbor_executable(tmp_path, template):
    from rsi_eval import evaluate, TargetSpec, BenchmarkSelection, RunOptions

    harbor = tmp_path / "fake-harbor"
    harbor.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("harbor 0.18.0")
    raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index("--config") + 1]).read_text())
native = Path(config["jobs_dir"]) / config["job_name"]
native.mkdir(parents=True)
artifact = Path(config["agents"][0]["kwargs"]["artifact_dir"])
reward = float("General mechanism 1" in (artifact / "prompt.md").read_text())
for task in config["tasks"]:
    name = task["name"].split("/")[-1]
    directory = native / (name + "__fake")
    directory.mkdir()
    (directory / "result.json").write_text(json.dumps({
        "task_name": task["name"], "finished_at": "2026-01-01T00:00:00Z",
        "task_id": {"org": task["name"].split("/")[0], "name": name, "ref": task["ref"]},
        "config": {"agent": config["agents"][0]},
        "verifier": {"finished_at": "2026-01-01T00:00:00Z"},
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_result": {"n_input_tokens": 100, "n_output_tokens": 20, "n_cache_tokens": 0}
    }))
(native / "result.json").write_text(json.dumps({"stats": {"n_errors": 0},
    "finished_at": "2026-01-01T00:00:00Z", "n_total_trials": len(config["tasks"]) * config["n_attempts"]}))
''')
    harbor.chmod(0o755)
    cfg = config()
    cfg["harbor_executable"] = str(harbor)
    external = FakeExternal()
    settings = SimpleNamespace(model="test-model", auth_dir=tmp_path / "auth",
                               expected_version="test-version", reasoning_effort="medium", timeout_seconds=60)
    result = run_experiment(cfg, tmp_path / "run", template=template,
                            settings=settings, codex_fn=external.codex, evaluate_fn=evaluate,
                            sdk_types=(TargetSpec, BenchmarkSelection, RunOptions))
    assert result["status"] == "completed"
    assert result["winner"] == "candidate_1"
    evaluations = json.loads((tmp_path / "run/evaluations.json").read_text())
    assert len(evaluations) == 6
    assert all(row["result"]["status"] == "completed" for row in evaluations)
    assert all(row["result"]["resolved"]["benchmark"]["tasks"][0]["ref"].startswith("sha256:") for row in evaluations)
    assert all(row["result"]["usage"]["cost_usd"] is None for row in evaluations)


def test_actual_harbor_metrics_exist_for_explicit_pinned_tasks(tmp_path, template):
    """Regression for a real oracle that passed but crashed in metric display."""
    from harbor.job import Job
    from harbor.models.job.config import JobConfig
    from rsi_eval import BenchmarkSelection, TargetSpec, preview
    from rsi_eval.evaluation import _job_config

    for name, version, task in [
        ("terminal-bench", "2.0", "extract-elf"),
        ("terminal-bench", "2.1", "extract-elf"),
        ("swe-bench-verified", "1", "astropy__astropy-12907"),
    ]:
        resolved = preview(target=TargetSpec(template, "oracle"),
                           benchmark=BenchmarkSelection(name, version, (task,)),
                           output_dir=tmp_path / f"{name}-{version}")
        assert resolved["benchmark"]["tasks"][0]["source"]
        config = JobConfig.model_validate(_job_config(resolved, tmp_path / "snapshot"))
        metrics = asyncio.run(Job._resolve_metrics(config, config.tasks))
        for task_config in config.tasks:
            group = task_config.source or "adhoc"
            assert metrics[group]
            assert metrics[group][0].compute([{"reward": 1.0}]) == {"mean": 1.0}


def test_per_selection_runtime_reaches_sdk_without_changing_search_budget(tmp_path, template):
    from integrations.meta_harness.launcher import normalize_config
    original = config()
    original['run_options'] = {'harbor_executable': 'tools/harbor', 'timeout': 777,
                               'repetitions': 9, 'concurrency': 9,
                               'benchmark_runtime': {'source_dir': 'data/default', 'judge_env_file': 'default.env'}}
    original['smoke'] = {**original['evolve'], 'benchmark_runtime': {}}
    original['test']['benchmark_runtime'] = {'judge_env_file': 'test.env'}
    normalized = normalize_config(original, tmp_path)
    external = FakeExternal()
    result = run(tmp_path, template, external, normalized)
    assert result['status'] == 'completed'
    for phase, _, selection, options in external.calls:
        assert not hasattr(selection, 'benchmark_runtime')
        assert options.harbor_executable == str(tmp_path / 'tools/harbor')
        assert options.timeout == 777
        assert options.repetitions == options.concurrency == 1
        expected = ({'source_dir': str(tmp_path / 'data/default'), 'judge_env_file': str(tmp_path / 'default.env')}
                    if phase == 'evolve' else {} if phase == 'smoke' else {'judge_env_file': str(tmp_path / 'test.env')})
        assert options.benchmark_runtime == expected
    assert original['run_options']['benchmark_runtime']['source_dir'] == 'data/default'


class ResumeExternal(FakeExternal):
    def __init__(self, *, stop_call=3, interrupt_proposal=False):
        super().__init__()
        self.stop_call = stop_call
        self.interrupt_proposal = interrupt_proposal
        self.resumed = []

    def evaluate(self, *, target, benchmark, options, output_dir, resume=False):
        self.resumed.append(resume)
        actual_output = output_dir / "retry" if resume else output_dir
        result = super().evaluate(target=target, benchmark=benchmark, options=options, output_dir=actual_output)
        if len(self.calls) == self.stop_call:
            raise KeyboardInterrupt("fixture evaluation stopped after writing native output")
        return result

    def codex(self, prompt, **kwargs):
        result = super().codex(prompt, **kwargs)
        if self.interrupt_proposal and len(self.prompts) == 1:
            (kwargs['log_dir'] / 'result.json').write_text(json.dumps({'usage': result.usage}))
            raise KeyboardInterrupt("fixture proposer stopped after file write")
        return result


def test_resume_reuses_baseline_and_proposal_continues_partial_evaluation(tmp_path, template):
    external = ResumeExternal()
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, template, external)
    baseline = next((tmp_path / 'run/upstream').rglob('codex-baseline-t1/benchmark-0/result.json'))
    original = baseline.read_bytes()
    result = run(tmp_path, template, external, resume=True)
    assert result['status'] == 'completed'
    assert len(external.prompts) == 2  # accepted first proposal was not regenerated
    assert len(external.calls) == 7  # only the interrupted evaluation was retried
    assert external.resumed.count(True) == 1
    assert baseline.read_bytes() == original
    rows = [json.loads(line) for line in Path(result['history']).read_text().splitlines()]
    assert [row['iteration'] for row in rows] == [1, 2]
    checkpoint = json.loads((tmp_path / 'run/resume.json').read_text())
    assert len(checkpoint['proposer_attempts']) == 2
    assert (tmp_path / 'run/attempts/result-0001.json').exists()


def test_resume_final_test_does_not_repeat_search_or_change_completed_output(tmp_path, template):
    external = ResumeExternal(stop_call=6)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, template, external)
    result = run(tmp_path, template, external, resume=True)
    assert len(external.calls) == 7 and len(external.prompts) == 2
    original = (tmp_path / 'run/result.json').read_bytes()
    assert run(tmp_path, template, external, resume=True) == result
    assert (tmp_path / 'run/result.json').read_bytes() == original
    assert len(external.calls) == 7 and len(external.prompts) == 2


def test_resume_archives_uncommitted_proposer_files_and_preserves_usage(tmp_path, template):
    external = ResumeExternal(stop_call=None, interrupt_proposal=True)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, template, external)
    result = run(tmp_path, template, external, resume=True)
    assert result['status'] == 'completed'
    assert len(external.prompts) == 3
    checkpoint = json.loads((tmp_path / 'run/resume.json').read_text())
    assert [row['status'] for row in checkpoint['proposer_attempts']] == ['interrupted', 'completed', 'completed']
    assert checkpoint['proposer_attempts'][0]['usage'] == {'input_tokens': 10, 'output_tokens': 20}
    archived = tmp_path / 'run/attempts/proposal-0001/workspace-additions/agents/candidate_1/prompt.md'
    assert archived.exists()
    assert len(list((tmp_path / 'run/upstream').rglob('iter1-attempt*/events.jsonl'))) == 2


@pytest.mark.parametrize('change', ['config', 'template', 'candidate', 'trajectory'])
def test_resume_rejects_changed_identity_or_evidence(tmp_path, template, change):
    external = ResumeExternal()
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, template, external)
    cfg = config()
    if change == 'config':
        cfg['iterations'] = 3
    elif change == 'template':
        (template / 'prompt.md').write_text('different template')
    elif change == 'candidate':
        next((tmp_path / 'run/upstream').rglob('candidate_1/prompt.md')).write_text('tampered')
    else:
        next((tmp_path / 'run/upstream').rglob('trajectory.json')).write_text('tampered')
    calls = len(external.calls)
    with pytest.raises(IntegrationError, match='Resume rejected'):
        run(tmp_path, template, external, cfg, resume=True)
    assert len(external.calls) == calls


def test_resume_frontier_before_history_does_not_apply_selection_twice(tmp_path, template, monkeypatch):
    from integrations.meta_harness.launcher import Bindings
    original = Bindings.update_history
    stopped = False

    def interrupt_once(self, *args, **kwargs):
        nonlocal stopped
        if not stopped:
            stopped = True
            raise KeyboardInterrupt('after native frontier before native history')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Bindings, 'update_history', interrupt_once)
    policy = tmp_path / 'select.py'
    policy.write_text('def choose(payload):\n    return payload["native_choice"]\n')
    cfg = config()
    cfg['method_policy'] = {'selection': {'kind': 'python', 'path': str(policy), 'function': 'choose'}}
    external = ResumeExternal(stop_call=None)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, template, external, cfg)
    result = run(tmp_path, template, external, cfg, resume=True)
    assert result['status'] == 'completed'
    assert len(external.calls) == 6 and len(external.prompts) == 2
    checkpoint = json.loads((tmp_path / 'run/resume.json').read_text())
    assert len([row for row in checkpoint['bindings']['policy_records'] if row['kind'] == 'selection']) == 3


def test_explicit_score_and_selection_policy_preserves_raw_results(tmp_path, template, monkeypatch):
    from rsi_eval import benchmarks
    original = benchmarks.list_benchmarks
    monkeypatch.setattr(benchmarks, 'list_benchmarks', lambda: original() + [{
        'name': 'continuous-fixture', 'version': '1', 'aggregation': {'type': 'drop_em_f1'},
        'tasks': [{'id': 'continuous', 'name': 'fixture/task', 'ref': 'sha256:' + '1' * 64}],
        'dataset_ref': 'fixture'}])
    policy = tmp_path / 'select.py'
    policy.write_text('def choose(payload):\n    return payload["candidates"][-1]["id"]\n')
    cfg = config()
    cfg['evolve'] = {'name': 'continuous-fixture', 'version': '1', 'task_ids': ['continuous']}
    cfg['method_policy'] = {'score': {'kind': 'threshold', 'threshold': 0.5},
                            'selection': {'kind': 'python', 'path': str(policy), 'function': 'choose'}}

    class Continuous(FakeExternal):
        def evaluate(self, **kwargs):
            result = super().evaluate(**kwargs)
            data = result.to_dict()
            for trial in data['trials']:
                trial['reward'] = 0.8 if trial['reward'] else 0.2
            for native in Path(data['artifact_dir']).glob('*/result.json'):
                raw = json.loads(native.read_text())
                raw['verifier_result']['rewards']['reward'] = data['trials'][0]['reward']
                native.write_text(json.dumps(raw))
            kwargs['output_dir'].joinpath('result.json').write_text(json.dumps(data))
            return SimpleNamespace(**{**data, 'trials': [SimpleNamespace(**t) for t in data['trials']]},
                                   to_dict=lambda: data)

    result = run(tmp_path, template, Continuous(), cfg)
    assert result['status'] == 'completed' and result['variant'] is True
    assert result['winner'] == 'candidate_2'  # explicit strategy replaces native max rule
    records = json.loads((tmp_path / 'run/evaluations.json').read_text())
    assert {trial['reward'] for record in records for trial in record['result']['trials']} == {0.2, 0.8}
    mapped = json.loads((tmp_path / 'run/method-policy.json').read_text())
    assert {row['method_score'] for row in mapped if row['kind'] == 'score'} == {0.0, 1.0}


def test_native_write_ahead_record_finishes_one_transaction_after_interruption(tmp_path, monkeypatch):
    from integrations.meta_harness.state import RunStore
    store = RunStore(tmp_path, {'test': 'identity'}, resume=False)
    frontier = tmp_path / 'upstream/frontier.json'
    frontier.parent.mkdir()
    frontier.write_text('{"old": true}')
    store.save({'round': 0})

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt('after atomic frontier write before checkpoint')

    monkeypatch.setattr(store, 'save', interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.commit_native(frontier, '{"new": true}', {'round': 1})
    recovered = RunStore(tmp_path, {'test': 'identity'}, resume=True)
    assert recovered.data['bindings'] == {'round': 1}
    assert json.loads(frontier.read_text()) == {'new': True}
    assert not (tmp_path / 'native-write.json').exists()


def test_score_policy_cannot_return_fractional_native_passes(tmp_path, template):
    cfg = config()
    cfg['method_policy'] = {'score': {'kind': 'linear', 'min': -1, 'max': 1}}
    external = FakeExternal()
    with pytest.raises(ValueError, match='binary mapped scores'):
        run(tmp_path, template, external, cfg)
    assert len(external.prompts) == 0
    assert len(external.calls) == 1


def test_selection_policy_cannot_bypass_candidate_gates(tmp_path, template):
    policy = tmp_path / 'select.py'
    policy.write_text('def choose(payload):\n    return "unevaluated_candidate"\n')
    cfg = config()
    cfg['method_policy'] = {'selection': {'kind': 'python', 'path': str(policy), 'function': 'choose'}}
    with pytest.raises(ValueError, match='outside native gates'):
        run(tmp_path, template, FakeExternal(), cfg)


def test_same_experiment_cannot_run_in_two_processes(tmp_path, template):
    import fcntl
    output = tmp_path / 'run'
    output.mkdir()
    with (output / '.meta-run.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(IntegrationError, match='Another process'):
            run(tmp_path, template, FakeExternal())
        fcntl.flock(lock, fcntl.LOCK_UN)


@pytest.mark.parametrize("relocate", [False, True])
def test_native_loop_resume_uses_merged_sdk_trials_without_repeating_completed_tasks(tmp_path, template, monkeypatch, relocate):
    """Exercise resume through the real SDK and native frontier, not a fake result."""
    from rsi_eval import evaluate, TargetSpec, BenchmarkSelection, RunOptions
    counter = tmp_path / 'invocations.json'
    harbor = tmp_path / 'resumable-harbor'
    script = '''import json, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("harbor 0.18.0")
    raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index("--config") + 1]).read_text())
counter = Path(COUNTER_PATH)
records = json.loads(counter.read_text()) if counter.exists() else []
records.append([task["name"].split("/")[-1] for task in config["tasks"]])
counter.write_text(json.dumps(records))
first = len(records) == 1
native = Path(config["jobs_dir"]) / config["job_name"]
native.mkdir(parents=True)
artifact = Path(config["agents"][0]["kwargs"]["artifact_dir"])
is_baseline = "General mechanism" not in (artifact / "prompt.md").read_text()
for task in config["tasks"][:1] if first else config["tasks"]:
    name = task["name"].split("/")[-1]
    directory = native / (name + "__fake")
    directory.mkdir()
    reward = float(is_baseline and name == "log-summary-date-ranges")
    (directory / "result.json").write_text(json.dumps({
        "task_name": task["name"], "finished_at": "2026-01-01T00:00:00Z",
        "task_id": {"org": task["name"].split("/")[0], "name": name, "ref": task["ref"]},
        "config": {"agent": config["agents"][0]},
        "verifier": {"finished_at": "2026-01-01T00:00:00Z"},
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_result": {"n_input_tokens": 100, "n_output_tokens": 20, "n_cache_tokens": 0}
    }))
(native / "result.json").write_text(json.dumps({"stats": {"n_errors": int(first)},
    "finished_at": "2026-01-01T00:00:00Z", "n_total_trials": len(config["tasks"]) * config["n_attempts"]}))
raise SystemExit(int(first))
'''
    harbor.write_text(f'#!{sys.executable}\n' + script.replace('COUNTER_PATH', repr(str(counter))))
    harbor.chmod(0o755)
    cfg = config()
    cfg['iterations'] = 1
    cfg['evolve'] = {'name': 'terminal-bench', 'version': '2.0',
                     'task_ids': ['extract-elf', 'log-summary-date-ranges']}
    cfg['test'] = {'name': 'terminal-bench', 'version': '2.1', 'task_ids': ['extract-elf']}
    cfg['harbor_executable'] = str(harbor)
    external = FakeExternal()
    settings = SimpleNamespace(model='test-model', auth_dir=tmp_path / 'auth', expected_version='test-version',
                               reasoning_effort='medium', timeout_seconds=60)
    kwargs = dict(template=template, settings=settings, codex_fn=external.codex, evaluate_fn=evaluate,
                  sdk_types=(TargetSpec, BenchmarkSelection, RunOptions))
    experiment = tmp_path / 'experiment'
    with pytest.raises(IntegrationError, match='did not complete'):
        run_experiment(cfg, experiment / 'run', **kwargs)
    first_native = next((experiment / 'run/upstream').rglob('extract-elf__fake/result.json'))
    original = first_native.read_bytes()
    if relocate:
        from test_portability import portable_record
        from rsi_eval.portability import export_experiment, import_experiment
        record = portable_record(experiment, 'meta-harness', {**cfg, 'codex': {
            'model': settings.model, 'auth_dir': str(settings.auth_dir), 'expected_version': settings.expected_version}}, monkeypatch)
        export_experiment(experiment, tmp_path / 'bundle')
        destination = tmp_path / 'moved'
        import_experiment(tmp_path / 'bundle', destination, Path(record['environment']), settings.auth_dir)
        first_native = destination / first_native.relative_to(experiment)
        shutil.rmtree(experiment)
        experiment = destination
    result = run_experiment(cfg, experiment / 'run', resume=True, **kwargs)
    assert result['status'] == 'completed'
    assert result['winner'] == 'codex-baseline'
    assert result['frontier']['_best']['avg_pass_rate'] == 0.5
    assert first_native.read_bytes() == original
    commands = json.loads(counter.read_text())
    assert commands[:2] == [['extract-elf', 'log-summary-date-ranges'], ['log-summary-date-ranges']]
    assert len(commands) == 5 and len(external.prompts) == 1


def test_completed_report_tampering_and_legacy_resume_are_refused(tmp_path, template):
    external = FakeExternal()
    run(tmp_path, template, external)
    path = tmp_path / 'run/result.json'
    data = json.loads(path.read_text())
    data['winner'] = 'fabricated'
    path.write_text(json.dumps(data))
    with pytest.raises(IntegrationError, match='completed method report changed'):
        run(tmp_path, template, external, resume=True)
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    historical = legacy / 'result.json'
    historical.write_text('{"status":"completed"}')
    with pytest.raises(IntegrationError, match='no durable checkpoint'):
        run_experiment(config(), legacy, resume=True)
    assert list(legacy.iterdir()) == [historical]
