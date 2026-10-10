import json
from pathlib import Path

import pytest

from rsi_eval import BenchmarkSelection, TargetSpec, preview
from rsi_eval import materializers
from rsi_eval.benchmarks import resolve
from rsi_eval.evaluation import collect_result
from rsi_eval.materializers import jobbench


def spec():
    return resolve(BenchmarkSelection('jobbench', jobbench.VERSION, ('main--online_merchants--task1',)))


@pytest.mark.parametrize(('error', 'status'), [
    ('Judge failed: API exit 1; Error code: 401, invalid_api_key', 'auth_error'),
    ('Judge failed: Error code 429: rate limit reached', 'quota_exceeded'),
    ('Judge response schema is invalid', 'infra_error'),
    ('Native verifier file input-14019.txt missing', 'infra_error'),
])
def test_jobbench_hook_classifies_only_private_verifier_reports(tmp_path, error, status):
    (tmp_path / 'verifier').mkdir()
    (tmp_path / 'verifier/verification.json').write_text(json.dumps({'status': 'error', 'error': error}))
    diagnosis = materializers.verifier_failure(spec(), tmp_path)
    assert diagnosis['status'] == status
    assert error in diagnosis['error']


def test_agent_failure_lookalikes_are_ignored(tmp_path):
    (tmp_path / 'agent').mkdir()
    (tmp_path / 'agent/verification.json').write_text(json.dumps({'status': 'error', 'error': '401 authentication failed'}))
    assert materializers.verifier_failure(spec(), tmp_path) is None


@pytest.mark.parametrize('payload', [{'status': 'ok', 'reward': 1}, [], {'status': 'error', 'error': 4}, 'broken'])
def test_invalid_or_inconsistent_native_reports_remain_infrastructure_failures(tmp_path, payload):
    (tmp_path / 'verifier').mkdir()
    (tmp_path / 'verifier/verification.json').write_text(json.dumps(payload))
    assert materializers.verifier_failure(spec(), tmp_path)['status'] == 'infra_error'


def test_hook_cannot_override_failure_with_a_score_or_completed_status(monkeypatch, tmp_path):
    monkeypatch.setattr(jobbench, 'verifier_failure', lambda spec, path: {'status': 'completed', 'error': 'bad'})
    with pytest.raises(ValueError, match='failure status'):
        materializers.verifier_failure(spec(), tmp_path)


def native_result(tmp_path, reward=None):
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    (candidate / 'agent.py').write_text('VALUE = 1\n')
    target = TargetSpec(candidate, 'fixture:Agent')
    selected = BenchmarkSelection('jobbench', jobbench.VERSION, ('main--online_merchants--task1',))
    output = tmp_path / 'run'
    resolved = preview(target=target, benchmark=selected, output_dir=output)
    task = tmp_path / 'prepared-task'
    resolved['execution_tasks'] = [{'id': selected.task_ids[0], 'path': str(task)}]
    trial = output / 'native/eval/trial'
    trial.mkdir(parents=True)
    (trial / 'result.json').write_text(json.dumps({'task_name': task.name, 'task_id': {'path': str(task)},
        'finished_at': '2026-10-10T00:00:00Z', 'config': {'agent': {'import_path': target.entrypoint, 'model_name': None}},
        'verifier_result': {'rewards': {'reward': reward}} if reward is not None else None,
        'exception_info': None if reward is not None else {'exception_type': 'RewardFileNotFoundError', 'exception_message': 'Missing reward'},
        'agent_result': {'n_input_tokens': 100, 'n_output_tokens': 10, 'n_cache_tokens': 0}}))
    (output / 'native/eval/result.json').write_text(json.dumps({'n_total_trials': 1, 'finished_at': '2026-10-10T00:00:00Z'}))
    (trial / 'verifier').mkdir()
    (trial / 'verifier/verification.json').write_text(json.dumps({'status': 'error', 'error': 'API Error 401: invalid API key'}))
    return resolved


def test_collector_classifies_api_failure_without_reward_or_false_zero(tmp_path):
    result = collect_result(native_result(tmp_path), returncode=0)
    assert result.status == 'failed'
    assert result.trials[0].status == 'auth_error'
    assert result.trials[0].reward is None
    assert result.metrics['benchmark_metrics']['score_100'] is None
    assert 'JobBench judge' in result.trials[0].error
    assert result.usage['input_tokens'] == 100


def test_collector_never_overrides_an_actual_measured_reward(tmp_path):
    result = collect_result(native_result(tmp_path, reward=0), returncode=0)
    assert result.status == 'completed'
    assert result.trials[0].status == 'completed' and result.trials[0].reward == 0
