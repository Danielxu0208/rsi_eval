from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tomllib
import types

import pytest

from rsi_eval.materializers import harvey_lab as lab
from rsi_eval.materializers import harvey_lab_verifier as verifier


def catalog():
    return json.loads((Path(__file__).parents[1] / 'src/rsi_eval/data/catalog_harvey_lab.json').read_text())[0]


def fixture_spec(tmp_path):
    source = tmp_path / 'official'
    (source / 'lab_core/sandbox').mkdir(parents=True)
    (source / 'lab_core/sandbox/Dockerfile').write_text('FROM python:3.12-slim\n')
    for name in ('pyproject.toml', 'uv.lock', 'README.md', 'LICENSE'):
        (source / name).write_text(name)
    (source / 'tasks/shared').mkdir(parents=True)
    (source / 'tasks/shared/a.txt').write_text('complete public document')
    tasks = []
    for name in ('first', 'second'):
        task = source / 'tasks/practice' / name / 'task.json'
        task.parent.mkdir(parents=True)
        task.write_text(json.dumps({'title': name, 'instructions': 'Write report.md.',
            'docs_dir': '../../shared', 'criteria': [{'id': 'C-1', 'title': 'Complete', 'match_criteria': 'PASS if complete'}]}))
        tasks.append({'id': 'practice/' + name, 'title': name, 'practice_area': 'practice',
                      'task_sha256': lab.sha256(task), 'documents_path': 'tasks/shared', 'n_criteria': 1})
    core = {p.relative_to(source).as_posix(): lab.sha256(p) for p in source.rglob('*')
            if p.is_file() and 'tasks' not in p.relative_to(source).parts}
    docs = lab.tree_manifest(source / 'tasks/shared')
    return {'source': {'commit': lab.COMMIT, 'repository': lab.REPOSITORY, 'files': core,
            'document_trees': {'tasks/shared': {'sha256': lab.tree_hash(docs), 'file_count': 1, 'bytes': 24}}},
            'tasks': tasks, 'runtime_options': {'source_dir': str(source)}}


def row(passed=1, expected=1):
    return {'score': float(passed == expected), 'all_pass': passed == expected,
            'n_criteria': expected, 'n_passed': passed, 'n_grading_errors': 0,
            'criteria_results': [{'id': str(i), 'verdict': 'pass' if i < passed else 'fail'} for i in range(expected)]}


def test_full_catalog_has_every_public_task_and_shared_corpus():
    spec = catalog()
    assert len(spec['tasks']) == len({t['id'] for t in spec['tasks']}) == 2010
    assert len({t['practice_area'] for t in spec['tasks']}) == 27
    assert sum(t['n_criteria'] for t in spec['tasks']) == 114437
    assert sum(d['file_count'] for d in spec['source']['document_trees'].values()) == 60971
    assert len(spec['source']['document_trees']) == 1761
    assert sum(t['documents_path'] == 'tasks/firm-knowledge/dms' for t in spec['tasks']) == 250
    assert all(t['documents_path'] in spec['source']['document_trees'] for t in spec['tasks'])
    assert spec['protocol']['judge_models'] == lab.DEFAULT_JUDGES


def test_missing_judge_keys_are_explicit_not_zero(monkeypatch):
    for key in lab.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    checks = lab.preflight({'runtime_options': {}})
    assert not checks['judge:ANTHROPIC_API_KEY']['ok']
    assert not checks['judge:OPENAI_API_KEY']['ok']
    custom = lab.preflight({'runtime_options': {'judges': ['gpt-5.5']}})
    assert 'judge:ANTHROPIC_API_KEY' in custom  # Native filename fallback is also an API call.
    reverse = lab.preflight({'runtime_options': {'judges': list(reversed(lab.DEFAULT_JUDGES))}})
    assert reverse['native_judge_profile']['detail'] == 'Standard dual'


def test_materializer_keeps_rubric_private_and_deduplicates_documents(tmp_path, monkeypatch):
    spec = fixture_spec(tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-private-openai')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'unit-test-private-anthropic')
    output = tmp_path / 'prepared'
    output.mkdir()
    tasks = lab.prepare(spec, output)
    assert [t['id'] for t in tasks] == [t['id'] for t in spec['tasks']]
    contexts = []
    for result in tasks:
        root = Path(result['path'])
        cfg = tomllib.loads((root / 'task.toml').read_text())
        assert cfg['verifier']['environment_mode'] == 'separate'
        assert cfg['artifacts'] == ['/workspace/output']
        assert cfg['verifier']['env']['OPENAI_API_KEY'] == '${OPENAI_API_KEY}'
        compose = json.loads((root / 'environment/docker-compose.yaml').read_text())
        contexts.append(compose['services']['main']['build']['context'])
        assert compose['services']['main']['volumes'] == ['harvey-documents:/workspace/documents:ro']
        assert not any('rubric' in p.name or p.name == 'task.json' for p in (root / 'environment').rglob('*'))
        assert (root / 'tests/native/tasks' / result['id'] / 'task.json').is_file()
        assert not (root / 'solution').exists()  # No fake native oracle.
        for p in root.rglob('*'):
            if p.is_file():
                assert b'unit-test-private' not in p.read_bytes()
    assert contexts[0] == contexts[1]
    assert Path(contexts[0]).name == spec['source']['document_trees']['tasks/shared']['sha256']
    assert len(list((output / '.sources/upstream').rglob('a.txt'))) == 1


def test_materialized_task_parses_in_harbor(tmp_path):
    TaskConfig = pytest.importorskip('harbor.models.task.config').TaskConfig
    spec = fixture_spec(tmp_path)
    spec['runtime_options']['platform'] = 'linux/amd64'
    output = tmp_path / 'prepared'
    output.mkdir()
    root = Path(lab.prepare(spec, output)[0]['path'])
    assert 'FROM --platform=linux/amd64 ' in (root / 'tests/Dockerfile').read_text()
    cfg = TaskConfig.model_validate(tomllib.loads((root / 'task.toml').read_text()))
    assert cfg.verifier.environment_mode.value == 'separate'
    assert cfg.artifacts == ['/workspace/output']


def test_changed_document_or_rubric_is_rejected(tmp_path):
    spec = fixture_spec(tmp_path)
    source = Path(spec['runtime_options']['source_dir'])
    (source / 'tasks/shared/a.txt').write_text('changed')
    with pytest.raises(ValueError, match='document tree checksum'):
        lab.snapshot_source(spec, tmp_path / 'snapshot')


def test_shared_document_mapping_cannot_escape_source(tmp_path):
    spec = fixture_spec(tmp_path)
    spec['tasks'][0]['documents_path'] = '../outside'
    with pytest.raises(ValueError, match='unsafe'):
        lab.snapshot_source(spec, tmp_path / 'snapshot')


def test_native_dual_all_pass_uses_half_score_not_strict_both():
    scores = {'per_judge': {'a': row(1), 'b': row(0)}, 'dual_all_pass_rate': .5, 'dual_criterion_pass': .5}
    assert verifier.validate_scores(scores, ['a', 'b'], 1) == {
        'reward': .5, 'criterion_pass_rate': .5, 'strict_all_pass': 0., 'n_criteria': 2, 'n_passed': 1, 'judge_count': 2}


@pytest.mark.parametrize('kind', ['missing', 'error', 'nonfinite', 'inconsistent'])
def test_invalid_native_judge_result_has_no_reward(kind):
    scores = {'per_judge': {'a': row(1), 'b': row(0)}, 'dual_all_pass_rate': .5, 'dual_criterion_pass': .5}
    if kind == 'missing':
        del scores['per_judge']['b']
    elif kind == 'error':
        scores['per_judge']['b']['criteria_results'][0]['verdict'] = 'error'
    elif kind == 'nonfinite':
        scores['dual_all_pass_rate'] = float('nan')
    else:
        scores['dual_all_pass_rate'] = 1.
    with pytest.raises(RuntimeError):
        verifier.validate_scores(scores, ['a', 'b'], 1)


def test_output_symlink_is_rejected(tmp_path):
    output = tmp_path / 'output'
    output.mkdir()
    (output / 'escape').symlink_to('/etc/passwd')
    with pytest.raises(RuntimeError, match='non-regular'):
        verifier.output_manifest(output)


def test_native_entrypoint_preserves_scores_and_rejects_judge_failure(tmp_path, monkeypatch):
    root = tmp_path / 'lab'
    task = root / 'tasks/practice/test/task.json'
    task.parent.mkdir(parents=True)
    task.write_text('{}')
    output = tmp_path / 'output'
    output.mkdir()
    (output / 'report.md').write_text('candidate artifact')
    cfg = {'core_sha256': {}, 'task_id': 'practice/test', 'task_sha256': verifier.sha256(task),
           'judges': ['a'], 'parallel': 1, 'expected_n_criteria': 1, 'source_commit': lab.COMMIT,
           'document_tree_sha256': '0' * 64}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(cfg))
    evaluation = types.ModuleType('lab_core.evaluation.run_eval')
    evaluation.evaluate_run_dual = lambda *a, **k: None
    def evaluate(*a, **k):
        scores = row(1)
        (root / 'results/rsi-eval/scores.json').write_text(json.dumps(scores))
        return scores
    evaluation.evaluate_run = evaluate
    judge = types.ModuleType('lab_core.evaluation.judge')
    judge.Judge = lambda **k: None
    report = types.ModuleType('lab_core.evaluation.report')
    report.generate_report = lambda **k: None
    for name, module in [('lab_core.evaluation.run_eval', evaluation), ('lab_core.evaluation.judge', judge), ('lab_core.evaluation.report', report)]:
        monkeypatch.setitem(sys.modules, name, module)
    logs = tmp_path / 'logs'
    assert verifier.main(config, output, logs, root) == 0
    assert json.loads((logs / 'reward.json').read_text())['reward'] == 1
    assert (logs / 'scores.json').is_file()
    import shutil
    shutil.rmtree(root / 'results')
    def fail(*a, **k):
        raise RuntimeError('authentication failed: unit-secret-key')
    evaluation.evaluate_run = fail
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-secret-key')
    assert verifier.main(config, output, logs, root) == 1
    assert not (logs / 'reward.json').exists()
    assert 'unit-secret-key' not in (logs / 'grading-error.json').read_text()


def test_invalid_runtime_configuration_is_rejected():
    for opts in ({'judges': ['codex']}, {'judges': lab.DEFAULT_JUDGES + ['gpt-4']}, {'judges': ['gpt-5.5', 'gpt-5.5']}, {'judge_parallel': 0}, {'unknown': 1}):
        with pytest.raises(ValueError):
            lab.options({'runtime_options': opts})


def test_private_judge_failure_classifies_provider_diagnostics(tmp_path):
    private = tmp_path / 'verifier'
    private.mkdir()
    (private / 'grading-error.json').write_text(json.dumps({'source_commit': lab.COMMIT, 'reward_emitted': False,
        'error': 'Some criteria could not be graded'}))
    path = private / 'scores_claude.json'
    path.write_text(json.dumps({'criteria_results': [{'verdict': 'error', 'reasoning': 'grading error: AuthenticationError HTTP 401'}]}))
    assert lab.verifier_failure({}, tmp_path)['status'] == 'auth_error'
    path.write_text(json.dumps({'criteria_results': [{'verdict': 'error', 'reasoning': 'grading error: RateLimitError HTTP 429'}]}))
    assert lab.verifier_failure({}, tmp_path)['status'] == 'quota_exceeded'
    path.unlink()
    assert lab.verifier_failure({}, tmp_path)['status'] == 'infra_error'
    (private / 'grading-error.json').unlink()
    assert lab.verifier_failure({}, tmp_path) is None
