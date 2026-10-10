import ast
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tarfile
import tomllib
from urllib.parse import unquote

import pytest

from rsi_eval.benchmarks import list_benchmarks
from rsi_eval.materializers import jobbench

FIXTURE = Path(__file__).parent / 'fixtures/jobbench'
IDS = ['main--online_merchants--task1', 'easy--bookkeeping_accounting_and_auditing_clerks--task1']


def catalog():
    return next(s for s in list_benchmarks() if s['name'] == 'jobbench')


def source(tmp_path):
    spec = catalog()
    root = tmp_path / 'source'
    jobbench.extract_source(FIXTURE / 'evaluator.tar.gz', root, spec['source']['files'])
    return root


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_complete_public_manifest_and_frozen_source():
    spec = catalog()
    assert len(spec['tasks']) == len({t['id'] for t in spec['tasks']}) == 128
    assert {split: sum(t['split'] == split for t in spec['tasks']) for split in ('main', 'easy')} == {'main': 65, 'easy': 63}
    assert sum(len(t['source_files']) for t in spec['tasks']) == 878
    assert sum(f['size'] for t in spec['tasks'] for f in t['source_files']) == 279496802
    assert sum(bool(t['search_files']) for t in spec['tasks']) == 57
    assert hashlib.sha256((FIXTURE / 'evaluator.tar.gz').read_bytes()).hexdigest() == jobbench.ARCHIVE_SHA256
    assert spec['source']['commit'] == jobbench.COMMIT
    assert spec['source']['revision'] == jobbench.REVISION
    assert spec['oracle']['supported_task_ids'] == []
    assert spec['aggregation'] == {'type': 'rubric_mean', 'group_by': ['split']}
    for task in spec['tasks']:
        names = {f['path'] for f in task['source_files']}
        assert {'RUBRICS.json', 'task_card.md', 'task_folder/TASK_INSTRUCTIONS.txt'} <= names
        assert not any(name.startswith('files_required_to_search') for name in names)
        assert task['rubric_count'] > 0 and task['rubric_total_weight'] > 0


def fixture_prepare(monkeypatch, tmp_path):
    spec = copy.deepcopy(catalog())
    by_id = {t['id']: t for t in spec['tasks']}
    spec['tasks'] = [by_id[i] for i in IDS]
    def downloaded(url, destination, sha):
        if url == spec['source']['archive_url']:
            original = FIXTURE / 'evaluator.tar.gz'
        else:
            original = FIXTURE / 'dataset' / unquote(url.split('/resolve/' + jobbench.REVISION + '/')[1])
        assert hashlib.sha256(original.read_bytes()).hexdigest() == sha
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        return destination
    monkeypatch.setattr(jobbench, 'download', downloaded)
    output = tmp_path / 'tasks'
    tasks = jobbench.prepare(spec, output)
    return spec, tasks, output


def test_native_renderer_preserves_inputs_and_private_grader(monkeypatch, tmp_path):
    spec, tasks, output = fixture_prepare(monkeypatch, tmp_path)
    assert [t['id'] for t in tasks] == IDS
    original = output / '.sources/evaluator'
    for row in tasks:
        root = Path(row['path'])
        task = next(t for t in spec['tasks'] if t['id'] == row['id'])
        config = tomllib.loads((root / 'task.toml').read_text())
        assert config['verifier']['environment_mode'] == 'separate'
        assert config['artifacts'] == ['/workspace/output']
        assert config['verifier']['env']['JUDGE_API_KEY'] == '${JUDGE_API_KEY:-}'
        assert config['verifier']['env']['JUDGE_MODEL'] == 'grok-4.3'
        assert config['agent']['timeout_sec'] == 7200
        assert config['agent']['network_mode'] == 'public'
        assert config['verifier']['network_mode'] == 'public'
        assert (root / 'tests/judge.py').read_bytes() == (original / 'eval/judge.py').read_bytes()
        assert (root / 'tests/verify.py').read_bytes() == (original / 'harbor/runtime/verify.py').read_bytes()
        assert (root / 'tests/test.sh').read_bytes() == (original / 'harbor/templates/test.sh').read_bytes()
        assert (root / 'environment/Dockerfile').read_bytes() == (original / 'harbor/templates/environment.Dockerfile').read_bytes()
        assert not list((root / 'environment').rglob('RUBRICS.json'))
        assert not list((root / 'environment').rglob('task_card.md'))
        for item in task['source_files']:
            if item['path'].startswith('task_folder/'):
                assert hashlib.sha256((root / 'environment' / item['path']).read_bytes()).hexdigest() == item['sha256']
        provenance = json.loads((root / 'provenance.json').read_text())
        assert provenance['source_hashes'] == task['source_files']
        assert provenance['judge_token_usage'] is None
        assert not (root / 'solution/solve.sh').exists()


def test_pinned_native_task_parses_with_installed_harbor(monkeypatch, tmp_path):
    config_module = pytest.importorskip('harbor.models.task.config')
    _, tasks, _ = fixture_prepare(monkeypatch, tmp_path)
    config = config_module.TaskConfig.model_validate_toml((Path(tasks[0]['path']) / 'task.toml').read_text())
    assert config.verifier.environment_mode.value == 'separate'
    assert config.artifacts == ['/workspace/output']


def test_missing_judge_is_explicit_and_no_secret_in_checks(monkeypatch, tmp_path):
    monkeypatch.delenv('JUDGE_API_KEY', raising=False)
    spec = catalog()
    assert not jobbench.preflight(spec)['judge_credentials']['ok']
    key = 'unit-test-secret-never-used'
    path = tmp_path / 'judge.env'
    path.write_text('JUDGE_API_KEY=' + key + '\n')
    spec['runtime_options'] = {'judge_env_file': str(path)}
    checks = jobbench.preflight(spec)
    assert checks['judge_credentials']['ok']
    assert key not in json.dumps(checks)
    assert jobbench.runtime_environment(spec) == {'JUDGE_API_KEY': key}


@pytest.mark.parametrize('options', [
    {'api_key': 'never-inline'}, {'judge_api_base': 'http://example.invalid'},
    {'judge_api_base': 'https://user:secret@example.invalid'},
    {'judge_model': ''}, {'judge_max_workers': True}, {'agent_timeout_sec': 0},
    {'verifier_timeout_sec': 30}, {'judge_env_file': {'bad': 'path'}},
])
def test_bad_runtime_configuration_is_rejected(options):
    spec = catalog()
    spec['runtime_options'] = options
    with pytest.raises(ValueError):
        jobbench.runtime_options(spec)


def test_native_weighted_scoring_differs_from_rubric_pass_rate(tmp_path):
    root = source(tmp_path)
    # Execute these exact dependency-free upstream functions, not a rewritten scorer.
    module = ast.parse((root / 'eval/judge.py').read_text())
    definitions = [node for node in module.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('build_scorecard', 'build_reward')]
    scope = {}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), 'pinned-jobbench-scorecard', 'exec'), scope)
    results = [{'index': 0, 'weight': 2, 'result': {'score': 2, 'passed': True}},
               {'index': 1, 'weight': 8, 'result': {'score': 0, 'passed': False}}]
    score = scope['build_scorecard'](results)
    assert score['normalized_score'] == 0.2
    assert score['pass_rate'] == 0.5
    assert scope['build_reward'](score) == {'reward': 0.2}


def test_native_verifier_rejects_invalid_rewards_and_unsafe_outputs(tmp_path):
    native = load_module(source(tmp_path) / 'harbor/runtime/verify.py', '_test_jobbench_verify')
    output = tmp_path / 'outputs'
    output.mkdir()
    (output / 'link').symlink_to('/etc/passwd')
    with pytest.raises(native.VerificationError, match='symlink'):
        native._inspect_output_tree(output)
    reward = tmp_path / 'reward.json'
    for value in (True, '1', 2, -1, float('nan')):
        reward.write_text(json.dumps({'reward': value}))
        with pytest.raises(native.VerificationError):
            native._validated_reward(reward)


def test_api_failures_are_not_zero_scores(tmp_path):
    native = load_module(source(tmp_path) / 'harbor/runtime/verify.py', '_test_jobbench_logs')
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'jobbench_rubric_0.log').write_text('Parse Status: api_error\nAPI Exit Code: 1\nError: missing credentials\n')
    with pytest.raises(native.VerificationError, match='Judge failed'):
        native._validate_judge_logs(logs, 1)
