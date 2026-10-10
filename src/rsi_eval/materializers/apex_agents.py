"""Pinned native APEX-Agents packages, including world MCP and private grading."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tomllib

from .common import download
from .judge_env import load_judge_env

VERSION = '1.1.1'
DATASET_SHA256 = '3fe2900ebd603ea5a802049819ce6ead171575ee0166a61187065ad9606c955d'
JUDGE = 'vercel_ai_gateway/deepseek/deepseek-v4-flash-0731'
STORAGE_URL = 'https://ofhuhcpkvzjlejydnvyd.supabase.co/storage/v1/object/authenticated/packages/'
# Harbor's public application key permits anonymous reads of public packages.
# It is distributed in upstream harbor/auth/constants.py, not a user secret.
PUBLIC_APPLICATION_KEY = 'sb_publishable_Z-vuQbpvpG-PStjbh4yE0Q_e-d3MTIH'


def runtime_options(spec: dict) -> dict:
    options = dict(spec.get('runtime_options') or {})
    if set(options) - {'judge_env_file'}:
        raise ValueError('APEX supports only judge_env_file; changing the native judge requires a new declared profile')
    if 'judge_env_file' in options:
        if not isinstance(options['judge_env_file'], str) or not options['judge_env_file']:
            raise ValueError('judge_env_file must be a file path')
        options['judge_env_file'] = str(Path(options['judge_env_file']).expanduser().resolve())
    return options


def runtime_environment(spec: dict) -> dict[str, str]:
    values = load_judge_env(runtime_options(spec), {'VERCEL_AI_GATEWAY_API_KEY'})
    # The task pins its model; inherited GRADING_MODEL cannot change scoring.
    return {**values, 'GRADING_MODEL': JUDGE}


def preflight(spec: dict) -> dict:
    runtime_options(spec)
    try:
        ready = bool(runtime_environment(spec).get('VERCEL_AI_GATEWAY_API_KEY', '').strip())
    except (OSError, ValueError, UnicodeError):
        ready = False
    return {
        'judge_credentials': {'ok': ready, 'detail': 'The native APEX 1.1.1 judge requires VERCEL_AI_GATEWAY_API_KEY in the host environment or judge_env_file. A Codex subscription cannot authenticate this judge.', 'model': JUDGE},
        'native_protocol': {'ok': True, 'detail': 'All 240 native task packages are pinned. A shared agent image, shared verifier image and 31 world images provide the original linux/amd64 environment. The target must support the task world MCP server; official oracle scripts are available.'},
        'publisher_terms': {'ok': True, 'detail': spec.get('usage_terms', 'Evaluation-only publisher terms apply.')},
    }


def extract_package(archive: Path, destination: Path, expected: dict[str, str]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    seen = set()
    with tarfile.open(archive) as source:
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or '\\' in member.name:
                raise ValueError('Unsafe APEX package path')
            if member.isdir():
                continue
            name = path.as_posix()
            if not member.isfile() or name not in expected or name in seen:
                raise ValueError('Unexpected APEX package member')
            seen.add(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream, target.open('xb') as output:
                shutil.copyfileobj(stream, output)
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected[name]:
                raise ValueError('APEX source checksum mismatch: ' + name)
            target.chmod(member.mode & 0o777)
    if seen != expected.keys():
        raise ValueError('Incomplete APEX task package')


def configure_service_caches(path: Path) -> dict:
    """Avoid root-owned image caches without weakening service isolation."""
    original = path.read_bytes()
    config = json.loads(original)
    changed = []
    for name, server in config['mcpServers'].items():
        if 'uv' in server.get('args', []):
            server.setdefault('env', {}).update(UV_NO_CACHE='1', PYTHONDONTWRITEBYTECODE='1')
            changed.append(name)
    path.write_text(json.dumps(config, indent=2) + '\n')
    return {'original_sha256': hashlib.sha256(original).hexdigest(),
            'runtime_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'services': changed, 'changes': {'UV_NO_CACHE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}}


def prepare(spec: dict, output_dir: Path) -> list[dict]:
    if spec.get('version') != VERSION or spec['source'].get('sha256') != DATASET_SHA256:
        raise ValueError('APEX requires the pinned 1.1.1 dataset')
    runtime_options(spec)
    tasks = []
    for task in spec['tasks']:
        if not re.fullmatch(r'[a-z0-9_-]+', task['id']) or task['package_name'] != 'mercor/' + task['id']:
            raise ValueError('Invalid APEX task identity')
        ref = task['package_ref'].removeprefix('sha256:')
        if not re.fullmatch(r'[0-9a-f]{64}', ref):
            raise ValueError('APEX requires an immutable task ref')
        expected_path = f"packages/{task['package_name']}/{ref}/dist.tar.gz"
        if task['archive_path'] != expected_path:
            raise ValueError('APEX package path differs from its pinned identity')
        archive = download(STORAGE_URL + expected_path,
                           output_dir / '.sources' / (task['id'] + '.tar.gz'),
                           task['archive_sha256'], headers={'apikey': PUBLIC_APPLICATION_KEY,
                               'Authorization': 'Bearer ' + PUBLIC_APPLICATION_KEY})
        if archive.stat().st_size != task['archive_size']:
            raise ValueError('APEX archive size mismatch')
        destination = output_dir / task['id']
        extract_package(archive, destination, task['files'])
        task_file = destination / 'task.toml'
        original = task_file.read_text()
        config = tomllib.loads(original)
        if config['task']['name'] != task['package_name'] or config['metadata']['task_id'] != task['task_id']:
            raise ValueError('APEX task metadata identity mismatch')
        grader = json.loads((destination / 'tests/grading_config.json').read_text())
        if grader['grading_settings']['llm_judge_model'] != JUDGE:
            raise ValueError('APEX native grading model changed')
        # Keep the judge credential out of the agent container. The upstream
        # agent environment forwards this provider too; Codex uses its own login.
        before, after = original.split('[environment]', 1)
        after = '\n'.join(line for line in after.split('\n')
                          if not line.startswith(('VERCEL_AI_GATEWAY_API_KEY =', 'VERCEL_AI_GATEWAY_API_BASE =')))
        task_file.write_text(before + '[environment]' + after)
        (destination / 'upstream-task.toml').write_text(original)
        cache_repair = configure_service_caches(destination / 'environment/task_files/.harbor/mcp_config.json')
        # The package uses a prebuilt image. The SDK requires a Dockerfile as
        # an explicit fallback; Harbor retains docker_image and native Compose.
        (destination / 'environment/Dockerfile').write_text('FROM ' + config['environment']['docker_image'] + '\n')
        (destination / 'provenance.json').write_text(json.dumps({
            'dataset_ref': spec['dataset_ref'], 'dataset_sha256': DATASET_SHA256,
            'package_ref': task['package_ref'], 'archive_sha256': task['archive_sha256'],
            'source_files': task['files'], 'judge_model': JUDGE,
            'transport_changes': ['Do not forward judge gateway credentials to agent', 'Add Dockerfile fallback for the unchanged prebuilt image', 'Disable service caches in root-owned image cache directories; preserve users and permissions'],
            'service_cache_repair': cache_repair,
            'protocol': spec['protocol'], 'judge_token_usage': None, 'judge_dollar_cost': None,
        }, indent=2) + '\n')
        tasks.append({'id': task['id'], 'path': str(destination.resolve())})
    return tasks


def verifier_failure(spec: dict, trial_dir: Path) -> dict | None:
    """A native failed grader is infrastructure failure, never task reward zero."""
    report = Path(trial_dir) / 'verifier/grading_error.json'
    if not report.is_file():
        return None
    try:
        failure = json.loads(report.read_text())
        if failure.get('grading_error') is not True:
            return None
        message = str(failure.get('detail', '')).lower()
    except (ValueError, TypeError, OSError):
        return {'status': 'infra_error', 'error': 'Malformed private APEX grading failure report'}
    if re.search(r'\b429\b|rate[ _]?limit|quota|resource_exhausted', message):
        status = 'quota_exceeded'
    elif re.search(r'\b401\b|unauthori[sz]ed|authentication|invalid.api.key', message):
        status = 'auth_error'
    else:
        status = 'infra_error'
    return {'status': status, 'error': 'Native APEX grading failed; inspect verifier/grading_error.json and grade_details.json'}
