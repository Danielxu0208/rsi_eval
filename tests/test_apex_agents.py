import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tomllib
from unittest.mock import patch

import pytest

from rsi_eval import BenchmarkSelection
from rsi_eval.benchmarks import resolve
from rsi_eval.materializers import apex_agents as adapter


def test_full_release_and_all_native_package_contracts():
    spec = resolve(BenchmarkSelection('apex-agents', '1.1.1'))
    assert len(spec['tasks']) == 240
    assert len({task['world_id'] for task in spec['tasks']}) == 31
    assert spec['coverage']['by_domain'] == {'Investment Banking': 80, 'Law': 80, 'Management Consulting': 80}
    assert sum(task['expected_output'] == 'message_in_console' for task in spec['tasks']) == 206
    for task in spec['tasks']:
        assert len(task['archive_sha256']) == 64
        assert {'tests/grade.py', 'tests/atif.py', 'tests/test.sh', 'tests/grading_config.json',
                'environment/docker-compose.yaml', 'solution/solve.sh'} <= task['files'].keys()
        if task['expected_output'] != 'message_in_console':
            assert 'tests/needs_snapshot' in task['files']


def test_preflight_requires_separate_judge_and_never_exposes_credential(tmp_path):
    spec = resolve(BenchmarkSelection('apex-agents', '1.1.1'))
    env_file = tmp_path / 'private.env'
    env_file.write_text('VERCEL_AI_GATEWAY_API_KEY=private-value\n')
    with patch.dict('os.environ', {}, clear=True):
        assert not adapter.preflight(spec)['judge_credentials']['ok']
        spec['runtime_options'] = {'judge_env_file': str(env_file)}
        assert adapter.preflight(spec)['judge_credentials']['ok']
        assert 'private-value' not in json.dumps(adapter.preflight(spec))
        assert adapter.runtime_environment(spec)['VERCEL_AI_GATEWAY_API_KEY'] == 'private-value'
        assert adapter.runtime_environment(spec)['GRADING_MODEL'] == adapter.JUDGE
    spec['runtime_options']['judge_model'] = 'replacement'
    with pytest.raises(ValueError, match='new declared profile'):
        adapter.preflight(spec)


def tar(path, values):
    with tarfile.open(path, 'w:gz') as out:
        for name, data in values.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name.endswith('.sh') else 0o644
            out.addfile(member, io.BytesIO(data))


def test_native_source_preserved_and_only_transport_is_changed(tmp_path):
    spec = resolve(BenchmarkSelection('apex-agents', '1.1.1', task_ids=('128-jr-1-f7f95d92',)))
    task = spec['tasks'][0]
    native = ('[task]\nname="mercor/128-jr-1-f7f95d92"\n[metadata]\ntask_id=' + json.dumps(task['task_id']) +
        '\n[verifier]\nenvironment_mode="separate"\n[verifier.environment.env]\nVERCEL_AI_GATEWAY_API_KEY="${VERCEL_AI_GATEWAY_API_KEY:-}"\n'
        '[environment]\ndocker_image="public.example/image@sha256:' + 'a' * 64 + '"\n'
        '[environment.env]\nWORLD_TASK_ID="task-id"\nVERCEL_AI_GATEWAY_API_KEY = "${VERCEL_AI_GATEWAY_API_KEY:-}"\n'
        '[[environment.mcp_servers]]\nname="world"\ntransport="streamable-http"\nurl="http://world:8000/mcp"\n')
    files = {'task.toml': native.encode(), 'tests/grading_config.json': json.dumps({'grading_settings': {'llm_judge_model': adapter.JUDGE}}).encode(),
             'tests/test.sh': b'#!/bin/sh\nexit 1\n', 'tests/grade.py': b'# native fixture\n',
             'environment/docker-compose.yaml': b'services: {}\n',
             'environment/task_files/.harbor/mcp_config.json': json.dumps({'mcpServers': {'test': {'command': 'setpriv', 'args': ['--reuid=svc_test', 'uv', 'run', 'python', 'main.py'], 'env': {'HOME': '/home/svc_test'}}}}).encode()}
    archive = tmp_path / 'task.tar.gz'
    tar(archive, files)
    task['files'] = {key: hashlib.sha256(value).hexdigest() for key, value in files.items()}
    task['archive_size'] = archive.stat().st_size
    with patch.object(adapter, 'download', return_value=archive):
        results = adapter.prepare(spec, tmp_path / 'prepared')
    directory = Path(results[0]['path'])
    parsed = tomllib.loads((directory / 'task.toml').read_text())
    assert 'VERCEL_AI_GATEWAY_API_KEY' not in parsed['environment']['env']
    assert 'VERCEL_AI_GATEWAY_API_KEY' in parsed['verifier']['environment']['env']
    assert parsed['environment']['mcp_servers'][0]['url'] == 'http://world:8000/mcp'
    assert (directory / 'upstream-task.toml').read_text() == native
    assert (directory / 'tests/grade.py').read_bytes() == files['tests/grade.py']
    assert (directory / 'tests/test.sh').stat().st_mode & 0o111
    assert 'private-value' not in (directory / 'provenance.json').read_text()
    mcp = json.loads((directory / 'environment/task_files/.harbor/mcp_config.json').read_text())
    assert mcp['mcpServers']['test']['env'] == {'HOME': '/home/svc_test', 'UV_NO_CACHE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    assert mcp['mcpServers']['test']['args'][0] == '--reuid=svc_test'
    repair = json.loads((directory / 'provenance.json').read_text())['service_cache_repair']
    assert repair['original_sha256'] == task['files']['environment/task_files/.harbor/mcp_config.json']


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'tests\\escape', 'unexpected'])
def test_archive_rejects_unsafe_or_unexpected_member(tmp_path, name):
    archive = tmp_path / 'bad.tar.gz'
    tar(archive, {name: b'x'})
    with pytest.raises(ValueError):
        adapter.extract_package(archive, tmp_path / 'out', {'tests/test.sh': 'a' * 64})


def test_archive_rejects_corruption_and_missing_files(tmp_path):
    archive = tmp_path / 'bad.tar.gz'
    tar(archive, {'tests/test.sh': b'x'})
    with pytest.raises(ValueError, match='checksum'):
        adapter.extract_package(archive, tmp_path / 'out', {'tests/test.sh': 'a' * 64})
    with pytest.raises(ValueError, match='Incomplete'):
        adapter.extract_package(archive, tmp_path / 'out2', {'tests/test.sh': hashlib.sha256(b'x').hexdigest(), 'missing': 'a' * 64})


@pytest.mark.parametrize('detail,status', [('runner exited 1', 'infra_error'), ('HTTP 401 Unauthorized', 'auth_error'), ('quota 429', 'quota_exceeded')])
def test_native_grading_failure_is_not_a_measured_zero(tmp_path, detail, status):
    (tmp_path / 'verifier').mkdir()
    (tmp_path / 'verifier/grading_error.json').write_text(json.dumps({'grading_error': True, 'detail': detail}))
    result = adapter.verifier_failure({}, tmp_path)
    assert result['status'] == status
    assert detail not in result['error']
