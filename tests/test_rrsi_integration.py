"""Exercise upstream RRSI with deterministic external model/evaluator fixtures."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.rrsi import UPSTREAM_COMMIT
from integrations.rrsi.__main__ import audit, completion_report, child_environment, run_native, test_incumbent as evaluate_incumbent
from integrations.rrsi.prepare import git, normalize_config, prepare

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    source = ROOT / "rrsi"
    if not (source / ".git").exists():
        pytest.skip("Pinned upstream RRSI checkout is required for integration contract tests")
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "agent.py").write_text("from pathlib import Path\ndef build_prompt(instruction, artifact_dir):\n    return (artifact_dir / 'prompt.md').read_text() + '\\n' + instruction\n")
    (seed / "prompt.md").write_text("Check the requested result.\n")
    config = normalize_config({
        "codex": {"model": "fixture-model", "auth_dir": str(tmp_path / "auth"), "expected_version": "fixture-version"},
        "evolve": {"name": "terminal-bench", "version": "2.1", "task_ids": ["extract-elf"]},
        "test": {"name": "swe-bench-verified", "version": "1", "task_ids": ["astropy__astropy-12907"]},
        "rrsi": {"T": 2, "k": 1, "m": 1, "eval_parallel": 1},
    }, tmp_path)
    checkout = tmp_path / "managed"
    metadata = prepare(source, checkout, config, seed=seed)
    monkeypatch.syspath_prepend(str(checkout))
    for name in list(sys.modules):
        if name == "rrsi" or name.startswith("rrsi.") or name == "integrations.rrsi.domain_bridge":
            del sys.modules[name]
    yield checkout, config, metadata
    for name in list(sys.modules):
        if name == "rrsi" or name.startswith("rrsi.") or name == "integrations.rrsi.domain_bridge":
            del sys.modules[name]


def test_prepare_is_pinned_tracked_and_does_not_patch_original(prepared, tmp_path):
    checkout, config, metadata = prepared
    assert metadata["upstream_commit"] == UPSTREAM_COMMIT
    assert git(checkout, "rev-parse", "HEAD^") == UPSTREAM_COMMIT
    changed = set(git(checkout, "diff", "--name-only", "HEAD^", "HEAD").splitlines())
    assert "rrsi/llm.py" in changed
    assert "domains/coding_codex/harness/agent.py" in changed
    assert all(p == "rrsi/llm.py" or p == ".rsi-eval-integration.json" or p.startswith("domains/coding_codex/") for p in changed)
    assert "codex_generate" not in (ROOT / "rrsi" / "rrsi" / "llm.py").read_text()
    repeated = prepare(ROOT / "rrsi", checkout, config, seed=tmp_path / "seed")
    assert repeated["integration_commit"] == metadata["integration_commit"]


def install_external_fixtures(monkeypatch):
    import rsi_eval
    import rsi_eval.codex
    calls, generations = [], []
    writes, malformed_sent = 0, False

    def fake_codex(prompt, *, system=None, **kwargs):
        nonlocal writes, malformed_sent
        generations.append({"system": system, "prompt": prompt, "mode": kwargs["mode"]})
        if "strict reviewer" in (system or ""):
            reply = {"verdict": "accept", "reasons": [], "risk_notes": []}
        elif (system or "").startswith("You are the batch analyst"):
            if "[you] digest_many" not in prompt:
                reply = {"action": "digest_many", "requests": [{"task_id": "extract-elf", "lens": "failure"}]}
            else:
                reply = {"action": "report", "failure_modes": [{"mode": "incomplete_verification", "n_tasks": 1, "affected_tasks": ["extract-elf"], "description": "The policy did not verify its final changes."}], "capability_gaps": [], "success_habits": []}
        elif "trajectory digester" in (system or ""):
            if "[you]" not in prompt:
                reply = {"action": "read_file", "path": "extract-elf.txt"}
            else:
                reply = {"action": "return", "digest": {"task_id": "extract-elf", "lens": "failure", "failure_mode": "incomplete_verification", "evidence": [{"where": "step 1", "quote": "verification missing"}]}}
        elif not malformed_sent:
            malformed_sent = True
            return SimpleNamespace(text="malformed fixture response", usage={"input_tokens": 20, "output_tokens": 4})
        elif "OK: edited" not in prompt.rsplit("=== INTERACTION LOG ===", 1)[-1]:
            writes += 1
            old = "Check the requested result.\n" if writes == 1 else f"Check the requested result. Verify the changes in {writes - 1} bounded passes.\n"
            reply = {"action": "edit_file", "path": "prompt.md", "old": old, "new": f"Check the requested result. Verify the changes in {writes} bounded passes.\n"}
        else:
            reply = {"action": "done", "summary": "Add a bounded verification instruction", "edits": [{"id": "verify", "component": "prompt", "hypothesis": "An explicit bounded check improves completion.", "targets_mode": "incomplete_verification", "predicted_affected": ["extract-elf"], "retroactive_check": "Inspect subsequent verification traces."}]}
        return SimpleNamespace(text=json.dumps(reply), usage={"input_tokens": 50, "output_tokens": 20})

    def fake_evaluate(*, target, benchmark, options, output_dir):
        from rsi_eval import EvaluationResult, TrialResult
        prompt = (target.artifact_dir / "prompt.md").read_text()
        calls.append({"artifact": str(target.artifact_dir), "prompt": prompt, "benchmark": benchmark.name, "output": str(output_dir)})
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = output_dir / "native" / "eval" / "fixture__trial"
        artifact.mkdir(parents=True)
        trajectory = artifact / "trajectory.json"
        trajectory.write_text(json.dumps({"steps": [{"observation": "verification missing", "candidate_prompt": prompt}]}))
        reward = 1.0 if "bounded passes" in prompt else 0.0
        input_tokens = 100 if "2 bounded passes" in prompt else 50
        trials = [TrialResult(task_id=tid, repetition=rep, status="completed", reward=reward,
                              input_tokens=input_tokens, output_tokens=10, artifact_dir=str(artifact), trajectory_path=str(trajectory))
                  for tid in benchmark.task_ids for rep in range(options.repetitions)]
        return EvaluationResult(status="completed", trials=trials, metrics={"pass_rate": reward}, usage={}, artifact_dir=str(output_dir / "native" / "eval"), resolved={"task_ids": list(benchmark.task_ids)})

    monkeypatch.setattr(rsi_eval.codex, "run_codex", fake_codex)
    monkeypatch.setattr(rsi_eval, "evaluate", fake_evaluate)
    return calls, generations


def test_two_native_rounds_actions_selection_and_cross_benchmark(prepared, tmp_path, monkeypatch):
    checkout, config, _ = prepared
    calls, generations = install_external_fixtures(monkeypatch)
    from rrsi.config import RRSIConfig
    from rrsi.domain import load_domain
    from rrsi.loop import Run
    domain = load_domain("coding_codex")
    cfg = RRSIConfig.load(domain.root / "rrsi.json")
    runs = tmp_path / "runs"
    monkeypatch.setenv("RRSI_CODEX_LOG_DIR", str(runs / "role_calls"))
    run = Run(domain, cfg, checkout, runs)
    run.baseline()
    run.round(0)
    run.round(1)
    result = audit(checkout, runs, rounds=2)
    assert len(result["rounds"]) == 2
    assert completion_report(checkout, runs)["acceptance_passed"]
    decisions0 = json.loads((run.runs / "r0" / "decisions.json").read_text())
    decisions1 = json.loads((run.runs / "r1" / "decisions.json").read_text())
    assert decisions0[0]["admissible"] is True
    assert decisions1[0]["admissible"] is False  # Real native cost gate, not forced acceptance.
    assert "cost rule" in decisions1[0]["reason"]
    assert len(calls) == 5  # baseline + two independent smoke/formal pairs
    assert all("/wt/" in c["artifact"] for c in calls)
    assert all(g["mode"] == "role" for g in generations)
    assert any("trajectory digester" in g["system"] and "verification missing" in g["prompt"] for g in generations)
    assert any("ACCEPTED" in g["prompt"] for g in generations if "STRICT JSON protocol" in g["system"])
    assert any("ERROR: not valid JSON" in g["prompt"] for g in generations)
    final = evaluate_incumbent(checkout, runs, tmp_path / "final")
    assert final.status == "completed"
    assert calls[-1]["benchmark"] == "swe-bench-verified"
    assert "1 bounded passes" in calls[-1]["prompt"]  # Rejected second candidate isn't tested.
    assert "final_artifacts" in calls[-1]["artifact"]


def test_missing_tokens_and_failed_trial_are_not_silent_success(prepared, tmp_path):
    from rrsi.domain import load_domain
    domain = load_domain("coding_codex")
    output = tmp_path / "runs" / "jobs" / "base" / "evaluation"
    output.mkdir(parents=True)
    trial = {"task_id": "extract-elf", "repetition": 0, "status": "completed", "reward": 1.0,
             "input_tokens": None, "output_tokens": 10}
    (output / "result.json").write_text(json.dumps({"status": "completed", "trials": [trial]}))
    with pytest.raises(ValueError, match="Missing policy token"):
        domain.score(tmp_path / "runs", "base", ["extract-elf"], 1)
    trial["status"] = "infra_error"
    (output / "result.json").write_text(json.dumps({"status": "completed", "trials": [trial]}))
    per, _ = domain.score(tmp_path / "runs", "base", ["extract-elf"], 1)
    assert per["extract-elf"].missing == 1
    assert per["extract-elf"].rewards == [0.0]


@pytest.mark.parametrize("status", ["task_timeout", "agent_error"])
def test_measured_agent_failures_preserve_native_reward_and_require_usage(prepared, tmp_path, status):
    from rrsi.domain import load_domain
    from rrsi.evaluate import aggregate
    domain = load_domain("coding_codex")
    output = tmp_path / "runs/jobs/base/evaluation"
    output.mkdir(parents=True)
    trial = {"task_id": "extract-elf", "repetition": 0, "status": status, "reward": 0.5,
             "input_tokens": 40, "output_tokens": 10}
    for reward in (0.5, 0.0, None):
        trial["reward"] = reward
        (output / "result.json").write_text(json.dumps({"status": "completed", "trials": [trial]}))
        per, extra = domain.score(tmp_path / "runs", "base", ["extract-elf"], 1)
        measured = aggregate("base", 1, per, extra)
        if reward is None:
            assert measured.missing == 1 and measured.C is None
        else:
            assert measured.S == reward and measured.C == 50 and measured.missing == 0
    trial.update(reward=0.0, input_tokens=None)
    (output / "result.json").write_text(json.dumps({"status": "completed", "trials": [trial]}))
    with pytest.raises(ValueError, match="Missing policy token"):
        domain.score(tmp_path / "runs", "base", ["extract-elf"], 1)


@pytest.mark.parametrize("status", ["partial", "failed", None])
def test_incomplete_job_invalidates_even_successful_trial_files(prepared, tmp_path, status):
    from rrsi.domain import load_domain
    from rrsi.evaluate import aggregate
    domain = load_domain("coding_codex")
    output = tmp_path / "runs/jobs/base/evaluation"
    output.mkdir(parents=True)
    trial = {"task_id": "extract-elf", "repetition": 0, "status": "completed", "reward": 1.0,
             "input_tokens": 40, "output_tokens": 10}
    (output / "result.json").write_text(json.dumps({"status": status, "trials": [trial]}))
    per, extra = domain.score(tmp_path / "runs", "base", ["extract-elf"], 1)
    measured = aggregate("base", 1, per, extra)
    assert measured.missing == measured.n_expected == 1
    assert measured.C is None


def test_native_loop_retries_partial_job_then_rejects_without_scored_candidate(prepared, tmp_path, monkeypatch):
    import rsi_eval
    checkout, _, _ = prepared
    calls, _ = install_external_fixtures(monkeypatch)
    successful_fixture = rsi_eval.evaluate

    def incomplete_job(**kwargs):
        result = successful_fixture(**kwargs)
        if "/jobs/r0A/evaluation" in str(kwargs["output_dir"]):
            result.status = "partial"  # Trial succeeded, but Harbor job did not finish.
            result.error = "Harbor job exited with status 1"
        return result

    monkeypatch.setattr(rsi_eval, "evaluate", incomplete_job)
    from rrsi.config import RRSIConfig
    from rrsi.domain import load_domain
    from rrsi.loop import Run
    domain = load_domain("coding_codex")
    runs = tmp_path / "runs"
    monkeypatch.setenv("RRSI_CODEX_LOG_DIR", str(runs / "role_calls"))
    run = Run(domain, RRSIConfig.load(domain.root / "rrsi.json"), checkout, runs)
    run.baseline()
    baseline = json.loads((run.runs / "frontier.json").read_text())["incumbent"]["commit"]
    run.round(0)
    assert len([call for call in calls if "/jobs/r0A/evaluation" in call["output"]]) == 2
    assert not (run.runs / "r0/A/eval.json").exists()
    assert json.loads((run.runs / "frontier.json").read_text())["incumbent"]["commit"] == baseline
    with pytest.raises(ValueError, match="no real, changed, fully measured candidate"):
        audit(checkout, runs, rounds=1)
    with pytest.raises(ValueError, match="unresolved evaluation failure"):
        completion_report(checkout, runs, rounds=1)


@pytest.mark.parametrize("outcome", ["no_proposal", "critic_reject"])
def test_legitimate_native_rejections_complete_but_fail_strict_acceptance(prepared, tmp_path, monkeypatch, outcome):
    checkout, _, _ = prepared
    calls, _ = install_external_fixtures(monkeypatch)
    from rrsi.config import RRSIConfig
    from rrsi.domain import load_domain
    from rrsi.loop import Run
    import rrsi.loop
    domain = load_domain("coding_codex")
    cfg = RRSIConfig.load(domain.root / "rrsi.json")
    cfg.repair_rounds = 0
    if outcome == "no_proposal":
        monkeypatch.setattr(rrsi.loop, "propose", lambda *a, **kw: {"status": "done", "n_edits": 0})
    else:
        monkeypatch.setattr(rrsi.loop, "review", lambda *a, **kw: {"verdict": "reject", "reasons": ["fixture rejection"]})
    runs = tmp_path / "runs"
    monkeypatch.setenv("RRSI_CODEX_LOG_DIR", str(runs / "role_calls"))
    run = Run(domain, cfg, checkout, runs)
    run.baseline()
    baseline = run.frontier()["incumbent"]["commit"]
    run.round(0)
    report = completion_report(checkout, runs, rounds=1)
    assert report["status"] == "completed" and not report["acceptance_passed"]
    assert report["rounds"][0]["decisions"][0]["reason"] == outcome
    assert report["incumbent"]["commit"] == baseline
    assert len(calls) == 1
    with pytest.raises(ValueError, match="no real, changed"):
        audit(checkout, runs, rounds=1)
    result = evaluate_incumbent(checkout, runs, tmp_path / "heldout")
    assert result.status == "completed" and "bounded passes" not in calls[-1]["prompt"]


def test_imported_rrsi_native_incumbent_remains_testable_without_original_path(prepared, tmp_path, monkeypatch):
    import shutil
    from test_portability import portable_record
    from rsi_eval.portability import export_experiment, import_experiment
    from rsi_eval.method_test import test_selected
    checkout, config, _ = prepared
    root = tmp_path / "experiment"
    root.mkdir()
    checkout.rename(root / "checkout")
    checkout = root / "checkout"
    monkeypatch.syspath_prepend(str(checkout))
    calls, _ = install_external_fixtures(monkeypatch)
    from rrsi.config import RRSIConfig
    from rrsi.domain import load_domain
    from rrsi.loop import Run
    domain = load_domain("coding_codex")
    monkeypatch.setenv("RRSI_CODEX_LOG_DIR", str(root / "run/role_calls"))
    run = Run(domain, RRSIConfig.load(domain.root / "rrsi.json"), checkout, root / "run")
    run.baseline()
    run.round(0)
    record = portable_record(root, "rrsi", config, monkeypatch)
    bundle, destination = tmp_path / "bundle", tmp_path / "moved"
    export_experiment(root, bundle)
    import_experiment(bundle, destination, Path(record["environment"]), tmp_path / "new-auth")
    shutil.rmtree(root)
    # A managed resume starts a new interpreter; discard the fixture's old
    # module __file__ values before loading the relocated native implementation.
    for name in list(sys.modules):
        if name == "rrsi" or name.startswith("rrsi.") or name == "integrations.rrsi.domain_bridge":
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(destination / "checkout"))
    importlib.invalidate_caches()
    from rrsi.config import RRSIConfig as MovedConfig
    from rrsi.loop import Run as MovedRun
    from integrations.rrsi.domain_bridge import CodexDomain
    moved_domain = CodexDomain(destination / "checkout/domains/coding_codex")
    monkeypatch.setenv("RRSI_CODEX_LOG_DIR", str(destination / "run/role_calls"))
    resumed = MovedRun(moved_domain, MovedConfig.load(moved_domain.root / "rrsi.json"),
                  destination / "checkout", destination / "run")
    resumed.round(1)
    assert completion_report(destination / "checkout", destination / "run")["status"] == "completed"
    assert not root.exists()
    imported = json.loads((destination / "method.json").read_text())
    checks = []
    def fake_check(**kwargs):
        # This integration fixture replaces external model/evaluator processes;
        # its prerequisite probe must use the same offline boundary.
        checks.append(kwargs)
        assert Path(kwargs["target"].config["codex_auth_dir"]) == tmp_path / "new-auth"
        return {"ok": True, "checks": {"fixture": {"ok": True}}}
    result = test_selected(imported, tmp_path / "independent-test", checker=fake_check)
    assert result["status"] == "completed"
    assert len(checks) == 1 and checks[0]["benchmark"].name == "swe-bench-verified"
    assert "1 bounded passes" in calls[-1]["prompt"]
    assert str(tmp_path / "independent-test/candidate") == calls[-1]["artifact"]
    assert git(destination / "checkout", "status", "--porcelain", "--untracked-files=no") == ""


@pytest.mark.parametrize("stage", ["formal", "smoke", "role"])
def test_native_driver_stops_on_auth_or_quota_without_advancing_round(prepared, tmp_path, monkeypatch, stage):
    import rsi_eval
    import rsi_eval.codex
    from rrsi import driver
    from rrsi.config import RRSIConfig
    from rrsi.domain import load_domain
    from rrsi.loop import Run

    checkout, _, _ = prepared
    calls, _ = install_external_fixtures(monkeypatch)
    successful_fixture = rsi_eval.evaluate
    role_calls = []

    def failing_evaluation(**kwargs):
        result = successful_fixture(**kwargs)
        job = "r0A_smoke" if stage == "smoke" else "r0A"
        if stage != "role" and f"/jobs/{job}/evaluation" in str(kwargs["output_dir"]):
            result.status = "failed"
            result.trials[0].status = "auth_error"
            result.trials[0].reward = None
        return result

    def exhausted_role(*args, **kwargs):
        role_calls.append(kwargs)
        raise rsi_eval.codex.CodexQuotaError("fixture quota exhausted")

    monkeypatch.setattr(rsi_eval, "evaluate", failing_evaluation)
    if stage == "role":
        monkeypatch.setattr(rsi_eval.codex, "run_codex", exhausted_role)
    domain = load_domain("coding_codex")
    runs = tmp_path / "runs"
    run = Run(domain, RRSIConfig.load(domain.root / "rrsi.json"), checkout, runs)
    monkeypatch.setenv("RRSI_RUN_DIR", str(run.runs))
    monkeypatch.setenv("RRSI_CODEX_LOG_DIR", str(run.runs / "codex_roles"))
    run.baseline()
    run.calibrate(["base"])
    invoked_rounds = []

    def round_process(argv, **kwargs):
        # Native drive still owns scheduling; only its OS process boundary is
        # replaced. Its child executes the actual native Run.round implementation.
        index = int(argv[argv.index("--t") + 1])
        invoked_rounds.append(index)
        try:
            run.round(index)
        except SystemExit:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(driver, "subprocess", SimpleNamespace(run=round_process, STDOUT=subprocess.STDOUT))
    driver.drive(checkout / "rrsi.py", "coding_codex", runs, T=2)
    assert invoked_rounds == [0]
    assert (run.runs / "STOP").exists()
    fatal = json.loads((run.runs / "fatal_error.json").read_text())
    assert fatal["reason"] == ("quota_exceeded" if stage == "role" else "auth_error")
    assert len(json.loads((run.runs / "frontier.json").read_text())["trajectory"]) == 1
    assert not (run.runs / "r1").exists()
    assert len(calls) == {"role": 1, "smoke": 2, "formal": 3}[stage]
    if stage == "role":
        assert len(role_calls) == 1
    with pytest.raises(RuntimeError, match="STOP is present"):
        run_native(checkout, runs, "run")


def test_queued_digester_roles_do_not_call_codex_after_quota_stop(prepared, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import rsi_eval.codex
    from integrations.rrsi.backend import generate

    checkout, _, _ = prepared
    run_dir = tmp_path / "runs/coding_codex"
    monkeypatch.setenv("RRSI_RUN_DIR", str(run_dir))
    monkeypatch.setenv("RRSI_CODEX_LOG_DIR", str(run_dir / "codex_roles"))
    calls = []

    def exhausted(*args, **kwargs):
        calls.append(kwargs)
        raise rsi_eval.codex.CodexQuotaError("fixture exhausted")

    monkeypatch.setattr(rsi_eval.codex, "run_codex", exhausted)
    config_path = checkout / "domains/coding_codex/experiment.json"
    with pytest.raises(SystemExit, match="STOP"):
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: generate("digest", config_path=config_path), range(6)))
    assert len(calls) == 1
    assert (run_dir / "STOP").exists()


def test_fresh_subprocess_uses_persistent_backend_and_tracked_seed(prepared, tmp_path):
    checkout, _, _ = prepared
    # Fake only the external Codex response. Import and call the patched native
    # llm module from a fresh child, just as driver.py's round processes do.
    script = '''import json, pathlib
from types import SimpleNamespace
import rsi_eval.codex
def fake(prompt, **kwargs):
    assert kwargs['mode'] == 'role'
    assert kwargs['settings'].model == 'fixture-model'
    return SimpleNamespace(text='{"action":"list_files"}', usage={'output_tokens':1})
rsi_eval.codex.run_codex = fake
from rrsi.llm import generate
from rrsi.domain import load_domain
from rrsi.loop import Run
from rrsi.config import RRSIConfig
d = load_domain('coding_codex')
r = Run(d, RRSIConfig.load(d.root / 'rrsi.json'), pathlib.Path.cwd(), pathlib.Path('child_runs'))
r.ensure_branch()
wt = r.checkout('child_check', r.branch)
assert (d.harness_dir(wt) / 'agent.py').is_file()
assert json.loads(generate('next', json_only=True, model='fixture-model'))['action'] == 'list_files'
print('CHILD_CODEX_OK')
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=checkout, env=child_environment(checkout, tmp_path / "logs"), text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHILD_CODEX_OK" in result.stdout


def test_cli_test_selection_manifest_is_relative_to_selection_file(tmp_path, monkeypatch):
    from integrations.rrsi import __main__ as launcher

    selections = tmp_path / "selections"
    selections.mkdir()
    manifest = selections / "tasks.json"
    manifest.write_text('["extract-elf"]')
    choice = selections / "benchmark.json"
    choice.write_text(json.dumps({"name": "terminal-bench", "version": "2.0", "manifest": "tasks.json"}))
    shell_dir = tmp_path / "unrelated-cwd"
    shell_dir.mkdir()
    monkeypatch.chdir(shell_dir)
    captured = {}

    def fake_test(checkout, runs, output, selection):
        captured.update(selection)
        return SimpleNamespace(status="completed", to_dict=lambda: {"status": "completed"})

    monkeypatch.setattr(launcher, "test_incumbent", fake_test)
    code = launcher.main(["test", "--checkout", "managed", "--runs", "runs",
                          "--output", "test-output", "--selection", str(choice)])
    assert code == 0
    assert Path(captured["manifest"]) == manifest
    assert json.loads(Path(captured["manifest"]).read_text()) == ["extract-elf"]


def test_runtime_options_survive_candidate_worktree_and_child_cwd(prepared, tmp_path):
    _, original, _ = prepared
    original['run_options'] = {'harbor_executable': 'tools/harbor', 'concurrency': 1,
        'benchmark_runtime': {'source_dir': 'data/default', 'judge_env_file': 'global.env',
                              'task_overrides': {'extract-elf': {'assets_dir': 'assets'}}}}
    original['evolve']['benchmark_runtime'] = {'source_dir': 'data/evolve'}
    original['smoke']['benchmark_runtime'] = {}
    original['test']['benchmark_runtime'] = {'judge_env_file': 'test.env'}
    configured = normalize_config(original, tmp_path)
    checkout = tmp_path / 'runtime-checkout'
    prepare(ROOT / 'rrsi', checkout, configured, seed=tmp_path / 'seed')
    worktree = tmp_path / 'candidate-worktree'
    git(checkout, 'worktree', 'add', '--detach', str(worktree), 'HEAD')
    program = '''import json
from dataclasses import asdict
from pathlib import Path
from integrations.rrsi.domain_bridge import options_from_dict, selection_from_dict
config=json.loads(Path('domains/coding_codex/experiment.json').read_text())
print(json.dumps({role:asdict(options_from_dict(config['run_options'],3,config[role])) for role in ('evolve','smoke','test')}))
for role in ('evolve','smoke','test'):selection_from_dict(config[role])
'''
    proc = subprocess.run([sys.executable, '-c', program], cwd=worktree,
                          env=child_environment(checkout, tmp_path / 'runs'), capture_output=True, text=True, check=True)
    result = json.loads(proc.stdout)
    assert result['evolve']['benchmark_runtime'] == {'source_dir': str(tmp_path / 'data/evolve')}
    assert result['smoke']['benchmark_runtime'] == {}
    assert result['test']['benchmark_runtime'] == {'judge_env_file': str(tmp_path / 'test.env')}
    assert all(options['repetitions'] == 3 and options['harbor_executable'] == str(tmp_path / 'tools/harbor') for options in result.values())
    assert configured['run_options']['benchmark_runtime']['task_overrides']['extract-elf']['assets_dir'] == str(tmp_path / 'assets')


def test_external_test_selection_resolves_runtime_paths(tmp_path):
    from integrations.rrsi.__main__ import load_selection
    selection_file = tmp_path / 'selection.json'
    selection_file.write_text(json.dumps({'name': 'harvey-lab', 'version': 'public-a15650f-v1',
        'manifest': 'tasks.json', 'benchmark_runtime': {'judge_env_file': 'judge.env', 'source_dir': 'source'}}))
    selection = load_selection(selection_file)
    assert selection['manifest'] == str(tmp_path / 'tasks.json')
    assert selection['benchmark_runtime'] == {'judge_env_file': str(tmp_path / 'judge.env'), 'source_dir': str(tmp_path / 'source')}


def test_explicit_score_mapping_retains_raw_results_and_token_gate(prepared, tmp_path):
    from rsi_eval.method_policy import normalize_policy
    from rrsi.domain import load_domain
    from rrsi.evaluate import aggregate
    domain = load_domain("coding_codex")
    domain.config["method_policy"] = normalize_policy({"score": {"kind": "linear", "min": 0, "max": 100}}, tmp_path)
    output = tmp_path / "runs/jobs/base/evaluation"
    output.mkdir(parents=True)
    trial = {"task_id": "extract-elf", "repetition": 0, "status": "completed", "reward": 70,
             "input_tokens": 40, "output_tokens": 10}
    original = json.dumps({"status": "completed", "trials": [trial]})
    (output / "result.json").write_text(original)
    per, extra = domain.score(tmp_path / "runs", "base", ["extract-elf"], 1)
    measured = aggregate("base", 1, per, extra)
    assert measured.S == .7 and measured.C == 50
    assert per["extract-elf"].extra["raw_trials"][0]["reward"] == 70
    assert (output / "result.json").read_text() == original
    assert (output.parent / "method_scores.json").is_file()
    trial["input_tokens"] = None
    (output / "result.json").write_text(json.dumps({"status": "completed", "trials": [trial]}))
    with pytest.raises(ValueError, match="Missing policy token"):
        domain.score(tmp_path / "runs", "base", ["extract-elf"], 1)


def test_custom_selection_preserves_native_cost_and_screening_gates(prepared, tmp_path):
    from rrsi.config import RRSIConfig
    from rrsi.domain import load_domain
    from rrsi.evaluate import aggregate, TaskResult
    from rrsi.selection import Candidate
    from integrations.rrsi.selection import select_round
    from rsi_eval.method_policy import normalize_policy
    domain = load_domain("coding_codex")
    policy = tmp_path / "choose.py"
    policy.write_text("def choose(payload):\n    assert [c['id'] for c in payload['candidates']] == ['A', 'B']\n    return 'A'\n")
    domain.config["method_policy"] = normalize_policy({"selection": {"kind": "python", "path": str(policy), "function": "choose"}}, tmp_path)
    def ev(name, reward, tokens):
        return aggregate(name, 1, {"one": TaskResult([reward], tokens=[tokens])})
    candidates = [Candidate("A", [{"component": "prompt"}], ev("a", .6, 10)),
                  Candidate("B", [{"component": "prompt"}], ev("b", .7, 10)),
                  Candidate("C", [{"component": "prompt"}], ev("c", 1., 100000)),
                  Candidate("D", [], gate_failure="critic_reject")]
    incumbent = ev("base", .5, 10)
    winner, decisions = select_round(candidates, incumbent, .5, .017,
                                     RRSIConfig.load(domain.root / "rrsi.json"), {}, guard_fn=domain.guards)
    assert winner.variant == "A"
    assert decisions[1].native_selected is True
    assert decisions[0].policy_selected is True
    assert not decisions[2].admissible and "cost rule" in decisions[2].reason
    assert not decisions[3].admissible


def test_resume_archives_known_stop_and_inferrs_first_unsettled_round(prepared, tmp_path, monkeypatch):
    from integrations.rrsi import __main__ as launcher
    checkout, _, _ = prepared
    runs = tmp_path / "resume-runs"
    directory = runs / "coding_codex"
    directory.mkdir(parents=True)
    (directory / "frontier.json").write_text(json.dumps({"trajectory": [{"t": 0}, {"t": 1}]}))
    (directory / "STOP").write_text("quota_exceeded")
    (directory / "fatal_error.json").write_text(json.dumps({"reason": "quota_exceeded", "source": "fixture"}))
    calls = []
    monkeypatch.setattr(launcher, "run_native", lambda *args: calls.append(args) or 0)
    assert launcher.resume_native(checkout, runs) == 0
    assert calls[0][-1] == ["--start", "1"]
    assert not (directory / "STOP").exists()
    assert len(list((directory / "resume_history").glob("*/STOP"))) == 1
    (directory / "STOP").write_text("user requested stop")
    with pytest.raises(ValueError, match="Unknown STOP"):
        launcher.resume_native(checkout, runs)


def test_policy_source_and_selection_route_inherit_into_candidate_worktree(prepared, tmp_path):
    from copy import deepcopy
    checkout, original, _ = prepared
    configured = deepcopy(original)
    source = tmp_path / "fixed_policy.py"
    source.write_text("def choose(payload):\n    return payload['native_choice']\n")
    configured["method_policy"] = {"variant": "fixture-selection",
        "selection": {"kind": "python", "path": str(source), "function": "choose"}}
    configured = normalize_config(configured, tmp_path)
    managed = tmp_path / "policy-checkout"
    prepare(ROOT / "rrsi", managed, configured, seed=tmp_path / "seed")
    worktree = tmp_path / "policy-candidate"
    git(managed, "worktree", "add", "--detach", str(worktree), "HEAD")
    source.unlink()  # The original file is not a runtime dependency.
    assert "from integrations.rrsi.selection import select_round" in (worktree / "rrsi/loop.py").read_text()
    program = '''import json
from pathlib import Path
from integrations.rrsi.domain_bridge import CodexDomain
from rsi_eval.method_policy import select_candidate
root=Path('domains/coding_codex')
d=CodexDomain(root)
assert select_candidate(d.config['method_policy'], [{'id':'A'}], 'A') == 'A'
print(json.dumps({'ids':d.evolve_ids(),'variant':d.config['method_policy']['variant']}))
'''
    proc = subprocess.run([sys.executable, "-c", program], cwd=worktree,
                          env=child_environment(managed, tmp_path / "runs"), capture_output=True, text=True, check=True)
    assert json.loads(proc.stdout) == {"ids": ["extract-elf"], "variant": "fixture-selection"}
