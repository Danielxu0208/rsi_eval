import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, check, evaluate, preview
from rsi_eval import materializers
from rsi_eval.benchmarks import resolve
from rsi_eval.config import load_config
from rsi_eval.materializers import jobbench
from rsi_eval.materializers.judge_env import load_judge_env


def test_private_file_allowlist_precedence_quotes_and_literal_values(tmp_path, monkeypatch):
    monkeypatch.setenv('JUDGE_API_KEY', 'host-private')
    monkeypatch.setenv('UNRELATED_SECRET', 'not-for-grader')
    envfile = tmp_path / 'private.env'
    envfile.write_text('# ignored\nexport JUDGE_API_KEY="$(never execute)${UNEXPANDED}"\n')
    assert load_judge_env({'judge_env_file': str(envfile)}, {'JUDGE_API_KEY'}) == {
        'JUDGE_API_KEY': '$(never execute)${UNEXPANDED}'}
    envfile.write_text('JUDGE_API_KEY=\n')
    assert load_judge_env({'judge_env_file': str(envfile)}, {'JUDGE_API_KEY'}) == {}
    assert load_judge_env({}, {'JUDGE_API_KEY'}) == {'JUDGE_API_KEY': 'host-private'}


@pytest.mark.parametrize('text', ['PATH=private-secret', 'BAD ASSIGNMENT', 'JUDGE_API_KEY="unmatched',
                                  'JUDGE_API_KEY=bad\x00secret', 'JUDGE_API_KEY="multi\nline"'])
def test_malformed_private_files_fail_without_echoing_contents(tmp_path, text):
    envfile = tmp_path / 'private.env'
    envfile.write_text(text)
    with pytest.raises(ValueError) as caught:
        load_judge_env({'judge_env_file': str(envfile)}, {'JUDGE_API_KEY'})
    assert text not in str(caught.value)


def test_private_file_must_exist_and_be_small(tmp_path):
    path = tmp_path / 'private.env'
    with pytest.raises(ValueError):
        load_judge_env({'judge_env_file': str(path)}, {'JUDGE_API_KEY'})
    path.write_bytes(b'x' * (1024 * 1024 + 1))
    with pytest.raises(ValueError):
        load_judge_env({'judge_env_file': str(path)}, {'JUDGE_API_KEY'})


@pytest.mark.parametrize('values', [None, [], {'JUDGE_API_KEY': 5}, {'PATH': 'bad'}, {'CODEX_HOME': 'bad'},
                                  {'DOCKER_HOST': 'bad'}])
def test_runtime_hook_rejects_non_string_or_runner_overrides(monkeypatch, values):
    monkeypatch.setattr(jobbench, 'runtime_environment', lambda spec: values)
    spec = resolve(BenchmarkSelection('jobbench', jobbench.VERSION, ('main--online_merchants--task1',)))
    with pytest.raises(ValueError):
        materializers.runtime_environment(spec)


def test_runtime_hook_receives_copies_and_defaults_to_empty(monkeypatch):
    spec = resolve(BenchmarkSelection('jobbench', jobbench.VERSION, ('main--online_merchants--task1',)))
    original = copy.deepcopy(spec)
    runtime = {'judge_model': 'custom'}
    def hook(configured):
        configured['tasks'].clear()
        configured['runtime_options']['judge_model'] = 'changed'
        return {'JUDGE_API_KEY': 'private-value'}
    monkeypatch.setattr(jobbench, 'runtime_environment', hook)
    assert materializers.runtime_environment(spec, runtime) == {'JUDGE_API_KEY': 'private-value'}
    assert spec == original and runtime == {'judge_model': 'custom'}
    unrelated = resolve(BenchmarkSelection('mgsm', '1.0-agent', ('en-001',)))
    assert materializers.runtime_environment(unrelated) == {}


def test_credentials_reach_only_child_environment_and_never_reports(tmp_path, monkeypatch):
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    (candidate / 'agent.py').write_text('VALUE = 1\n')
    target = TargetSpec(candidate, 'fixture:Agent')
    selection = BenchmarkSelection('jobbench', jobbench.VERSION, ('main--online_merchants--task1',))
    secret = 'private-test-value-never-a-real-key'
    private = tmp_path / 'private.env'
    private.write_text('JUDGE_API_KEY=' + secret + '\n')
    options = RunOptions(benchmark_runtime={'judge_env_file': str(private)})
    monkeypatch.delenv('JUDGE_API_KEY', raising=False)
    monkeypatch.setattr('rsi_eval.evaluation._probe', lambda *args: {'ok': True, 'detail': 'harbor 0.18.0'})
    diagnostic = check(target=target, benchmark=selection, options=options)
    planned = preview(target=target, benchmark=selection, options=options, output_dir=tmp_path / 'run')
    assert secret not in json.dumps(diagnostic) + json.dumps(planned)
    assert diagnostic['checks']['benchmark.judge_credentials']['ok']
    def prepare(spec, output):
        task = output / spec['tasks'][0]['id']
        for name in ('task.toml', 'instruction.md', 'environment/Dockerfile', 'tests/test.sh'):
            path = task / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture\n')
        return [{'id': spec['tasks'][0]['id'], 'path': str(task)}]
    monkeypatch.setattr(jobbench, 'prepare', prepare)
    captured = {}
    def popen(command, **kwargs):
        captured.update(kwargs['env'])
        assert secret not in json.dumps(command)
        return SimpleNamespace(wait=lambda timeout=None: 0)
    monkeypatch.setattr('rsi_eval.evaluation.subprocess.Popen', popen)
    result = evaluate(target=target, benchmark=selection, options=options, output_dir=tmp_path / 'run')
    assert captured['JUDGE_API_KEY'] == secret
    assert secret not in json.dumps(result.to_dict())
    for path in (tmp_path / 'run').rglob('*'):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_dataset_interpreter_preserves_virtualenv_symlink(tmp_path):
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    executable = tmp_path / 'base-python'
    executable.write_text('placeholder')
    interpreter = tmp_path / 'venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(executable)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'target': {'artifact_dir': 'candidate', 'entrypoint': 'fixture:Agent'},
        'benchmark': {'name': 'mgsm', 'version': '1.0-agent', 'task_ids': ['en-001']},
        'options': {'benchmark_runtime': {'dataset_python': 'venv/bin/python',
                    'task_overrides': {'x': {'dataset_python': 'venv/bin/python'}}}}}))
    options = load_config(config)[2]
    assert options.benchmark_runtime['dataset_python'] == str(interpreter)
    assert options.benchmark_runtime['task_overrides']['x']['dataset_python'] == str(interpreter)
