import copy

import pytest

from rsi_eval.metrics import benchmark_metrics, metric_error
from rsi_eval.types import TrialResult


def spec(kind='rubric_mean'):
    return {'aggregation': {'type': kind, 'group_by': ['split', 'subject']},
            'selection': 'full', 'tasks': [
                {'id': 'a', 'split': 'main', 'subject': 'alpha', 'rubric_total_weight': 100},
                {'id': 'b', 'split': 'main', 'subject': 'beta', 'rubric_total_weight': 2},
                {'id': 'c', 'split': 'easy', 'subject': 'beta', 'rubric_total_weight': 9}]}


def trial(task, reward, repetition=0, status='completed'):
    return TrialResult(task, repetition, status, reward)


def test_rubric_mean_uses_equal_tasks_and_keeps_published_splits_separate():
    result = benchmark_metrics([trial('a', 0), trial('b', 1), trial('c', 0.5)], spec(), 1)
    assert result['complete'] and result['mean_reward'] == 0.5
    assert result['score_100'] == 50
    assert result['task_count'] == result['expected_trials'] == result['observed_trials'] == 3
    assert result['by_split']['main'] == {'mean_reward': 0.5, 'task_count': 2, 'complete': True,
        'expected_trials': 2, 'observed_trials': 2, 'score_100': 50}
    assert result['by_subject']['beta']['mean_reward'] == 0.75


def test_absent_groups_keep_selected_counts_and_do_not_become_zero():
    result = benchmark_metrics([], spec(), 2)
    assert not result['complete'] and result['mean_reward'] is None
    assert result['expected_trials'] == 6 and result['observed_trials'] == 0
    assert result['by_split']['main'] == {'mean_reward': None, 'task_count': 2, 'complete': False,
        'expected_trials': 4, 'observed_trials': 0, 'score_100': None}
    assert result['by_split']['easy']['task_count'] == 1


def test_partial_selection_retains_only_complete_group_measurements():
    result = benchmark_metrics([trial('a', 1), trial('b', 0)], spec(), 1)
    assert not result['complete'] and result['mean_reward'] is None
    assert result['by_split']['main']['complete']
    assert result['by_split']['main']['mean_reward'] == 0.5
    assert result['by_split']['easy']['mean_reward'] is None
    assert result['by_split']['easy']['task_count'] == 1


def test_invalid_job_provenance_prevents_group_scores_even_with_measured_rows():
    result = benchmark_metrics([trial('a', 1), trial('b', 0), trial('c', 1)], spec(), 1, valid_run=False)
    assert result['mean_reward'] is None
    assert all(not row['complete'] and row['mean_reward'] is None for row in result['by_split'].values())


def test_repetitions_are_averaged_without_reweighting_tasks():
    rows = [trial('a', 0, 0), trial('a', 1, 1), trial('b', 1, 0), trial('b', 1, 1),
            trial('c', 0, 0), trial('c', 0, 1)]
    result = benchmark_metrics(rows, spec(), 2)
    assert result['complete'] and result['mean_reward'] == 0.5
    assert result['by_split']['main']['mean_reward'] == 0.75
    assert result['by_split']['main']['task_count'] == 2
    assert result['by_split']['main']['observed_trials'] == 4


@pytest.mark.parametrize('bad_rows', [
    [trial('a', 1, 0), trial('a', 1, 0)],
    [trial('a', 1, 0)],
    [trial('a', 1, 0), trial('a', None, 1, 'infra_error')],
    [trial('a', 1, 0), trial('a', float('nan'), 1)],
])
def test_missing_duplicate_or_invalid_repetition_is_incomplete(bad_rows):
    selected = spec(); selected['tasks'] = selected['tasks'][:1]
    result = benchmark_metrics(bad_rows, selected, 2)
    assert not result['complete'] and result['mean_reward'] is None
    assert not result['by_split']['main']['complete']


def test_unselected_extra_results_cannot_create_a_complete_group():
    rows = [trial('a', 1), trial('b', 1), trial('c', 1), trial('foreign', 1)]
    result = benchmark_metrics(rows, spec(), 1)
    assert result['observed_trials'] == 4 and result['expected_trials'] == 3
    assert not result['complete'] and not result['by_split']['main']['complete']


@pytest.mark.parametrize('reward', [-1, 1.1, True, float('inf'), None, '0.2'])
def test_invalid_rubric_rewards_are_rejected(reward):
    assert metric_error({'reward': reward}, spec(), 'a')


def test_binary_groups_preserve_accuracy_contract():
    selected = spec('accuracy_by_group')
    result = benchmark_metrics([trial('a', 1), trial('b', 0), trial('c', 1)], selected, 1)
    assert result['accuracy'] == pytest.approx(2 / 3)
    assert result['by_split']['main']['accuracy'] == 0.5
    assert 'score_100' not in result
    assert metric_error({'reward': 0.5}, selected, 'a')
    assert not benchmark_metrics([trial('a', 0.5), trial('b', 0), trial('c', 1)], selected, 1)['complete']


def test_raw_metric_cannot_override_the_validated_reward_field():
    selected = spec()
    row = trial('a', 0.5)
    row.metrics = {'reward': 2}
    result = benchmark_metrics([row, trial('b', 0), trial('c', 1)], selected, 1)
    assert result['complete'] and result['mean_reward'] == 0.5


def test_collector_placeholder_keeps_expected_coverage_without_observed_trials():
    rows = [TrialResult(task, 0, 'incomplete', error='trial result was not produced') for task in ('a', 'b', 'c')]
    result = benchmark_metrics(rows, spec(), 1, valid_run=False)
    assert result['task_count'] == 3 and result['observed_trials'] == 0
    assert result['by_split']['main']['task_count'] == 2
    assert result['by_split']['main']['observed_trials'] == 0
