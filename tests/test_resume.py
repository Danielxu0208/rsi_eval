"""Continuation must keep real zero scores, identities and failed-call usage."""
import json
from pathlib import Path
import sys

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate, resume


@pytest.fixture
def experiment(tmp_path):
    candidate = tmp_path / "source"
    candidate.mkdir()
    (candidate / "agent.py").write_text("VALUE = 'frozen'\n")
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "harbor"
    fake.write_text(f"#!{sys.executable}\n" + f"LOG = {str(log)!r}\n" + '''import json,sys
from pathlib import Path
if '--version' in sys.argv:
    print('harbor 0.18.0 fixture'); raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
log = Path(LOG)
attempt = len(log.read_text().splitlines()) if log.exists() else 0
control = json.loads(log.with_suffix('.control.json').read_text()) if log.with_suffix('.control.json').exists() else {}
with log.open('a') as stream: stream.write(json.dumps(config)+'\\n')
job=Path(config['jobs_dir'])/config['job_name']
job.mkdir(parents=True)
for n,task in enumerate(config['tasks']):
    for k in range(config['n_attempts']):
        if attempt == 0 and n == 1 and k == 1: continue
        org,name=task['name'].split('/',1)
        path=job/f'trial-{n}-{k}'
        path.mkdir()
        failure=(attempt == 0 and n == 0 and k == 1) or (attempt in control.get('fail_attempts', []) and n == 0)
        row={'task_name':task['name'],'task_id':{'org':org,'name':name,'ref':task['ref']},
             'config':{'agent':config['agents'][0]},'finished_at':'today',
             'agent_result':{'n_input_tokens':10,'n_output_tokens':2,'n_cache_tokens':0},
             'verifier_result':{'rewards':{'reward':0 if n == 0 else 1}},
             'exception_info':{'exception_type':'CodexQuotaError','exception_message':'quota'} if failure else None}
        (path/'result.json').write_text(json.dumps(row))
(job/'result.json').write_text(json.dumps({'finished_at':'today','n_total_trials':len(config['tasks'])*config['n_attempts']}))
''')
    fake.chmod(0o700)
    target = TargetSpec(candidate, "fixture:Agent", {"artifact_dir": str(candidate)})
    selection = BenchmarkSelection("terminal-bench", "2.0", ("extract-elf", "log-summary-date-ranges"))
    options = RunOptions(repetitions=2, harbor_executable=str(fake))
    output = tmp_path / "run"
    first = evaluate(target=target, benchmark=selection, options=options, output_dir=output)
    assert first.status == "partial"
    return target, selection, options, output, log


def test_resume_runs_only_missing_slots_and_preserves_raw_evidence(experiment):
    target, selection, options, output, log = experiment
    raw = {p: p.read_bytes() for p in (output / "native").rglob("result.json")}
    original_report = (output / "result.json").read_bytes()
    result = evaluate(target=target, benchmark=selection, options=options, output_dir=output, resume=True)
    assert result.status == "completed", result.error
    assert [(t.task_id, t.repetition, t.reward) for t in result.trials] == [
        ("extract-elf", 0, 0), ("extract-elf", 1, 0),
        ("log-summary-date-ranges", 0, 1), ("log-summary-date-ranges", 1, 1)]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 2
    assert calls[1]["n_attempts"] == 1
    assert len(calls[1]["tasks"]) == 2
    assert all(path.read_bytes() == content for path, content in raw.items())
    assert json.loads((output / "resume-history/initial-result.json").read_text()) == json.loads(original_report)
    assert result.usage["observed_input_tokens"] == 50  # Includes the failed quota call.
    assert result.usage["input_tokens"] is None  # Missing native attempt usage is not zero.
    assert result.usage["cost_usd"] is None
    assert resume(output).status == "completed"
    assert len(log.read_text().splitlines()) == 2


def test_standalone_resume_uses_frozen_candidate_after_source_changes(experiment):
    target, _, _, output, _ = experiment
    (target.artifact_dir / "agent.py").write_text("VALUE = 'later experiment'\n")
    result = resume(output)
    assert result.status == "completed"
    assert (output / "resume-attempts/0001/candidate/agent.py").read_text() == "VALUE = 'frozen'\n"


def test_explicit_request_cannot_change_candidate_or_task_set(experiment):
    target, selection, options, output, log = experiment
    (target.artifact_dir / "agent.py").write_text("different\n")
    with pytest.raises(ValueError, match="target.sha256"):
        evaluate(target=target, benchmark=selection, options=options, output_dir=output, resume=True)
    assert len(log.read_text().splitlines()) == 1


def test_tampered_native_identity_or_frozen_candidate_is_rejected(experiment):
    _, _, _, output, log = experiment
    native = next((output / "native/eval").glob("*/result.json"))
    row = json.loads(native.read_text())
    row["task_id"]["ref"] = "sha256:" + "0" * 64
    native.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="immutable task"):
        resume(output)
    assert len(log.read_text().splitlines()) == 1


def test_resume_checks_saved_attempt_receipts(experiment):
    _, _, _, output, log = experiment
    resume(output)
    native = next((output / "resume-attempts/0001/native/eval").glob("*/result.json"))
    row = json.loads(native.read_text())
    row["verifier_result"]["rewards"]["reward"] = 0.5
    native.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="evidence changed"):
        resume(output)
    assert len(log.read_text().splitlines()) == 2


def test_resume_lock_prevents_a_second_writer(experiment):
    from rsi_eval.resume import evaluation_lock
    _, _, _, output, _ = experiment
    with evaluation_lock(output):
        with pytest.raises(RuntimeError, match="already running"):
            resume(output)


def test_repeated_partial_resume_accounts_failed_attempts_once(experiment):
    _, _, _, output, log = experiment
    log.with_suffix(".control.json").write_text(json.dumps({"fail_attempts": [1]}))
    partial = resume(output)
    assert partial.status == "partial"
    assert len(log.read_text().splitlines()) == 2  # No implicit retry after quota.
    complete = resume(output)
    assert complete.status == "completed"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 3
    assert len(calls[-1]["tasks"]) == 1
    assert calls[-1]["tasks"][0]["name"].endswith("/extract-elf")
    assert calls[-1]["n_attempts"] == 1
    assert complete.usage["observed_input_tokens"] == 60
    assert complete.usage["input_tokens"] is None
    assert complete.usage["observed_trial_attempts"] == 6
    assert len(complete.trials) == 4
    unchanged = resume(output)
    assert unchanged.usage == complete.usage
    assert len(log.read_text().splitlines()) == 3
    # One selected raw directory for each original logical slot, with no failed
    # earlier-attempt directory duplicated in the compatibility view.
    view = Path(complete.artifact_dir)
    selected = list(view.glob("*__resume-*"))
    assert len(selected) == 4 and all(p.is_symlink() for p in selected)
    assert {p.resolve() for p in selected} == {Path(t.artifact_dir) for t in complete.trials}
    assert not any("0001/native/eval/trial-0-0" in str(p.resolve()) for p in selected)
    manifest = json.loads((view / "selection.json").read_text())
    assert len({(row["task_id"], row["repetition"]) for row in manifest["trials"]}) == 4


def test_attempt_identity_is_compared_with_parent_even_with_valid_own_receipts(experiment):
    from rsi_eval.resume import _receipts
    _, _, _, output, log = experiment
    resume(output)
    attempt = output / "resume-attempts/0001"
    resolved_file = attempt / "resolved.json"
    resolved = json.loads(resolved_file.read_text())
    resolved["target"]["model"] = "different-model"
    resolved_file.write_text(json.dumps(resolved))
    for path in (attempt / "native/eval").glob("*/result.json"):
        row = json.loads(path.read_text())
        row["config"]["agent"]["model_name"] = "different-model"
        path.write_text(json.dumps(row))
    state_file = output / "resume.json"
    state = json.loads(state_file.read_text())
    state["attempts"][0]["receipts"] = _receipts(attempt)
    state_file.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="target.model"):
        resume(output)
    assert len(log.read_text().splitlines()) == 2


@pytest.mark.parametrize("change", ["escape", "duplicate", "overlap"])
def test_resume_state_paths_and_logical_slot_identity_are_validated(experiment, change):
    _, _, _, output, log = experiment
    resume(output)
    state_file = output / "resume.json"
    state = json.loads(state_file.read_text())
    attempt = state["attempts"][0]
    if change == "escape":
        attempt["directory"] = "../foreign-run"
    elif change == "duplicate":
        attempt["slots"]["extract-elf"] = [1, 1]
    else:
        attempt["slots"]["extract-elf"] = [0]
    state_file.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="directory|unique|overlaps"):
        resume(output)
    assert len(log.read_text().splitlines()) == 2


def test_added_evidence_and_redirected_views_are_rejected(experiment, tmp_path):
    _, _, _, output, log = experiment
    result = resume(output)
    view = Path(result.artifact_dir)
    link = next(view.glob("*__resume-*"))
    original = link.resolve()
    link.unlink()
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="view was redirected"):
        resume(output)
    link.unlink()
    link.symlink_to(original, target_is_directory=True)
    native = next((output / "native/eval").glob("*/result.json")).parent
    (native / "extra-verifier-log.txt").write_text("late changed evidence")
    with pytest.raises(ValueError, match="evidence changed"):
        resume(output)
    assert len(log.read_text().splitlines()) == 2


def test_resume_after_interruption_before_child_snapshot_finishes(experiment, monkeypatch):
    import rsi_eval.evaluation as evaluation
    _, _, _, output, log = experiment
    original_evaluate = evaluation.evaluate
    def interrupted(**kwargs):
        directory = Path(kwargs["output_dir"])
        (directory / "candidate").mkdir(parents=True)
        (directory / "candidate/agent.py").write_text("unfinished copy")
        raise KeyboardInterrupt()
    monkeypatch.setattr(evaluation, "evaluate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        resume(output)
    monkeypatch.setattr(evaluation, "evaluate", original_evaluate)
    assert resume(output).status == "completed"
    state = json.loads((output / "resume.json").read_text())
    assert state["attempts"][0]["status"] == "interrupted_during_snapshot"
    assert (output / "resume-attempts/0001/candidate/agent.py").read_text() == "unfinished copy"
    assert len(log.read_text().splitlines()) == 2


def evidence_tree(directory):
    """Capture file bytes, links and directories without following selected views."""
    result = {}
    for path in directory.rglob("*"):
        name = str(path.relative_to(directory))
        if path.is_symlink():
            result[name] = ("symlink", str(path.readlink()))
        elif path.is_file():
            result[name] = ("file", path.read_bytes())
        else:
            result[name] = ("directory",)
    return result


@pytest.mark.parametrize("continued", [False, True])
def test_check_resume_reads_complete_history_without_writes_or_execution(experiment, monkeypatch, continued):
    import rsi_eval.evaluation as evaluation
    from rsi_eval.resume import check_resume
    target, selection, options, output, log = experiment
    if continued:
        log.with_suffix(".control.json").write_text(json.dumps({"fail_attempts": [1]}))
        assert resume(output).status == "partial"
        assert resume(output).status == "completed"
    before = evidence_tree(output)
    calls = log.read_bytes()
    def forbidden(**kwargs):
        pytest.fail("Read-only resume inspection must not evaluate or probe external tools")
    monkeypatch.setattr(evaluation, "evaluate", forbidden)
    monkeypatch.setattr(evaluation, "_probe", forbidden)
    report = check_resume(output, target=target, benchmark=selection, options=options)
    assert report == {"ok": True, "output_dir": str(output),
                      "status": "completed" if continued else "partial",
                      "expected_trials": 4, "completed_trials": 4 if continued else 2,
                      "pending_trials": 0 if continued else 2,
                      "attempts": 2 if continued else 0, "model_calls": 0}
    assert evidence_tree(output) == before
    assert log.read_bytes() == calls


@pytest.mark.parametrize("change, message", [
    ("implementation", "implementation changed"),
    ("candidate", "Candidate snapshot changed"),
    ("prepared_task", "Prepared task changed"),
    ("initial_receipt", "evidence changed"),
    ("history", "Archived initial result changed"),
    ("early_attempt", "evidence changed"),
    ("late_attempt", "evidence changed"),
    ("attempt_identity", "target.model"),
    ("overlap", "overlaps"),
    ("view", "view was redirected"),
])
def test_check_resume_rejects_invalid_evidence_before_any_work(experiment, tmp_path, change, message):
    from rsi_eval.resume import check_resume, _receipts
    _, _, _, output, log = experiment
    log.with_suffix(".control.json").write_text(json.dumps({"fail_attempts": [1]}))
    assert resume(output).status == "partial"
    complete = resume(output)
    assert complete.status == "completed"
    state_file = output / "resume.json"
    state = json.loads(state_file.read_text())
    if change == "implementation":
        path = output / "resolved.json"
        value = json.loads(path.read_text())
        value["execution_identity"] = "old-implementation"
        path.write_text(json.dumps(value))
    elif change == "candidate":
        (output / "candidate/agent.py").write_text("mutated")
    elif change == "prepared_task":
        path = output / "resolved.json"
        value = json.loads(path.read_text())
        value["execution_tasks"] = [{"id": "extract-elf", "path": str(output / "candidate"),
                                      "sha256": "0" * 64}]
        path.write_text(json.dumps(value))
    elif change == "initial_receipt":
        (output / "native/eval/late-log.txt").write_text("mutated")
    elif change == "history":
        (output / "resume-history/initial-result.json").write_text("{}")
    elif change in {"early_attempt", "late_attempt"}:
        number = "0001" if change == "early_attempt" else "0002"
        (output / f"resume-attempts/{number}/native/eval/late-log.txt").write_text("mutated")
    elif change == "attempt_identity":
        directory = output / "resume-attempts/0002"
        path = directory / "resolved.json"
        value = json.loads(path.read_text())
        value["target"]["model"] = "different-model"
        path.write_text(json.dumps(value))
        for path in (directory / "native/eval").glob("*/result.json"):
            row = json.loads(path.read_text())
            row["config"]["agent"]["model_name"] = "different-model"
            path.write_text(json.dumps(row))
        state["attempts"][1]["receipts"] = _receipts(directory)
        state_file.write_text(json.dumps(state))
    elif change == "overlap":
        state["attempts"][1]["slots"]["extract-elf"] = [0]
        state_file.write_text(json.dumps(state))
    else:
        link = next(Path(complete.artifact_dir).glob("*__resume-*"))
        link.unlink()
        link.symlink_to(tmp_path, target_is_directory=True)
    before = evidence_tree(output)
    calls = log.read_bytes()
    with pytest.raises(ValueError, match=message):
        check_resume(output)
    assert evidence_tree(output) == before
    assert log.read_bytes() == calls


def test_check_resume_rejects_changed_request_and_concurrent_writer(experiment):
    from dataclasses import replace
    from rsi_eval.resume import check_resume, evaluation_lock
    target, selection, options, output, log = experiment
    with pytest.raises(ValueError, match="changed options"):
        check_resume(output, target=target, benchmark=selection,
                     options=replace(options, repetitions=1))
    with evaluation_lock(output):
        with pytest.raises(RuntimeError, match="already running"):
            check_resume(output)
    assert len(log.read_text().splitlines()) == 1
    assert not (output / "resume.json").exists()


def test_check_resume_preserves_interrupted_attempt_without_archiving(experiment, monkeypatch):
    import rsi_eval.evaluation as evaluation
    from rsi_eval.resume import check_resume
    _, _, _, output, log = experiment
    def interrupted(**kwargs):
        directory = Path(kwargs["output_dir"])
        (directory / "candidate").mkdir(parents=True)
        (directory / "candidate/agent.py").write_text("unfinished copy")
        raise KeyboardInterrupt()
    monkeypatch.setattr(evaluation, "evaluate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        resume(output)
    before = evidence_tree(output)
    report = check_resume(output)
    assert report["ok"] and report["attempts"] == 1 and report["pending_trials"] == 2
    assert evidence_tree(output) == before
    assert "status" not in json.loads((output / "resume.json").read_text())["attempts"][0]
    assert len(log.read_text().splitlines()) == 1
