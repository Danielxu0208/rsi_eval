"""Complete pinned JobBench inputs with the original, private LLM judge."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
from typing import Any
from urllib.parse import quote, urlsplit

from .common import download

COMMIT = '660b33a86ed032559df0b2fdc1ad38cf7ceb83f3'
REVISION = 'd43a66575635da0d3f6cb9a20976dc1a60f3ffc9'
VERSION = 'd43a665-660b33a-v1'
ARCHIVE_SHA256 = '804052f247023befe69b831b4552d8de0c05c43a75db0e7b95e515d737ec1361'
ALLOWED = {'judge_env_file', 'judge_model', 'judge_api_base', 'judge_max_workers',
           'judge_max_retries', 'judge_timeout_per_rubric', 'agent_timeout_sec',
           'verifier_timeout_sec', 'build_timeout_sec'}


def runtime_options(spec: dict) -> dict:
    options = dict(spec.get('runtime_options') or {})
    if set(options) - ALLOWED:
        raise ValueError('Unknown JobBench benchmark_runtime options: ' + ', '.join(sorted(set(options) - ALLOWED)))
    defaults = {'judge_model': 'grok-4.3', 'judge_api_base': 'https://api.x.ai/v1',
                'judge_max_workers': 10, 'judge_max_retries': 3, 'judge_timeout_per_rubric': 300,
                'agent_timeout_sec': 7200, 'verifier_timeout_sec': 3600,
                'build_timeout_sec': 1800}
    defaults.update(options)
    for name in ('judge_max_workers', 'judge_max_retries', 'judge_timeout_per_rubric',
                 'agent_timeout_sec', 'verifier_timeout_sec', 'build_timeout_sec'):
        if type(defaults[name]) is not int or defaults[name] <= 0:
            raise ValueError(name + ' must be a positive integer')
    if defaults['verifier_timeout_sec'] <= 30:
        raise ValueError('verifier_timeout_sec must exceed 30 seconds')
    if not isinstance(defaults['judge_model'], str) or not defaults['judge_model'].strip():
        raise ValueError('judge_model must be a nonempty string')
    base = defaults['judge_api_base']
    parsed = urlsplit(base) if isinstance(base, str) else None
    if not parsed or parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('judge_api_base must be an HTTPS endpoint without credentials, query or fragment')
    if 'judge_env_file' in options:
        if not isinstance(options['judge_env_file'], str) or not options['judge_env_file']:
            raise ValueError('judge_env_file must be a path, never credential contents')
        defaults['judge_env_file'] = str(Path(options['judge_env_file']).expanduser().resolve())
    return defaults


def runtime_environment(spec: dict) -> dict[str, str]:
    from .judge_env import load_judge_env
    return load_judge_env(runtime_options(spec), {'JUDGE_API_KEY'})


def preflight(spec: dict) -> dict:
    options = runtime_options(spec)
    try:
        ready = bool(runtime_environment(spec).get('JUDGE_API_KEY', '').strip())
    except (OSError, ValueError, UnicodeError):
        ready = False
    return {
        'judge_credentials': {'ok': ready, 'detail':
            'A separate judge API credential is required. Set JUDGE_API_KEY in the host environment or '
            'provide benchmark_runtime.judge_env_file containing JUDGE_API_KEY. Codex subscription authentication does not supply the xAI judge.'},
        'judge_protocol': {'ok': True, 'detail':
            'Original JobBench judge, rubric scoring, evidence extraction and pinned LibreOffice runtime are retained.',
            'model': options['judge_model'], 'api_base': options['judge_api_base'],
            'official_default': options['judge_model'] == 'grok-4.3' and options['judge_api_base'] == 'https://api.x.ai/v1'},
        'public_network': {'ok': True, 'detail':
            'Tasks retain public network access. Live web search and browser capabilities must be supplied by the chosen agent.'}}


def extract_source(archive: Path, destination: Path, expected: dict[str, str]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    seen = set()
    with tarfile.open(archive) as source:
        for member in source:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != 'job-bench-eval-' + COMMIT or '..' in parts or PurePosixPath(member.name).is_absolute():
                raise ValueError('Unsafe JobBench source archive path')
            if member.isdir():
                continue
            relative = '/'.join(parts[1:])
            if not member.isfile() or relative not in expected or relative in seen:
                raise ValueError('Unexpected JobBench source file: ' + relative)
            seen.add(relative)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream, target.open('xb') as output:
                shutil.copyfileobj(stream, output)
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected[relative]:
                raise ValueError('JobBench source checksum mismatch: ' + relative)
    if seen != set(expected):
        raise ValueError('Incomplete JobBench evaluator archive')


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _toml(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render(spec: dict, task: dict, source: Path, raw: Path, destination: Path, renderer: Any) -> None:
    options = runtime_options(spec)
    provenance = renderer.render_task(raw, destination, task_id=task['id'], split=task['split'],
        occupation=task['occupation'], task_number='task' + str(task['task_number']),
        repo_id=spec['source']['repo_id'], revision=REVISION, judge_path=source / 'eval/judge.py',
        search_files=task['search_files'])
    original_toml = (destination / 'task.toml').read_text()
    configured = original_toml
    replacements = {
        'timeout_sec = 7200.0': 'timeout_sec = ' + str(float(options['agent_timeout_sec'])),
        'timeout_sec = 3600.0': 'timeout_sec = ' + str(float(options['verifier_timeout_sec'])),
        'build_timeout_sec = 1800.0': 'build_timeout_sec = ' + str(float(options['build_timeout_sec'])),
        '"${JUDGE_MODEL:-grok-4.3}"': _toml(options['judge_model']),
        '"${JUDGE_API_BASE:-https://api.x.ai/v1}"': _toml(options['judge_api_base']),
        '"${JUDGE_MAX_WORKERS:-10}"': _toml(str(options['judge_max_workers'])),
        '"${JUDGE_MAX_RETRIES:-3}"': _toml(str(options['judge_max_retries'])),
        '"${JUDGE_TIMEOUT_PER_RUBRIC:-300}"': _toml(str(options['judge_timeout_per_rubric'])),
    }
    for before, after in replacements.items():
        if before not in configured:
            raise ValueError('Pinned JobBench task configuration changed: ' + before)
        configured = configured.replace(before, after)
    _write(destination / 'task.toml', configured)
    _write(destination / 'upstream-task.toml', original_toml)
    provenance.update({'evaluator_commit': COMMIT, 'protocol': spec['protocol'],
                       'runtime_options': options, 'source_hashes': task['source_files'],
                       'judge_sha256': hashlib.sha256((destination / 'tests/judge.py').read_bytes()).hexdigest(),
                       'verify_sha256': hashlib.sha256((destination / 'tests/verify.py').read_bytes()).hexdigest(),
                       'judge_token_usage': None, 'judge_dollar_cost': None,
                       'usage_note': 'The original judge does not report API token usage or cost.'})
    _write(destination / 'provenance.json', json.dumps(provenance, indent=2) + '\n')


def prepare(spec: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    if spec.get('version') != VERSION or spec['source'].get('commit') != COMMIT or spec['source'].get('revision') != REVISION:
        raise ValueError('JobBench requires the pinned evaluator and complete dataset revision')
    runtime_options(spec)
    source_spec = spec['source']
    archive = download(source_spec['archive_url'], output_dir / '.sources/evaluator.tar.gz', ARCHIVE_SHA256)
    source = output_dir / '.sources/evaluator'
    extract_source(archive, source, source_spec['files'])
    module_spec = importlib.util.spec_from_file_location('_rsi_jobbench_native_render', source / 'harbor/jobbench_harbor/render.py')
    renderer = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(renderer)
    result = []
    for task in spec['tasks']:
        expected_id = f"{task['split']}--{task['occupation']}--task{task['task_number']}"
        if task['id'] != expected_id or not re.fullmatch(r'(main|easy)--[a-z0-9_-]+--task[1-9][0-9]*', expected_id):
            raise ValueError('JobBench task identity does not match its source directory')
        raw = output_dir / '.sources/dataset' / task['id']
        prefix = ('dataset' if task['split'] == 'main' else 'dataset_easy') + '/' + task['occupation'] + '/task' + str(task['task_number']) + '/'
        for item in task['source_files']:
            relative = item['path']
            if PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts or '\\' in relative:
                raise ValueError('Unsafe JobBench raw source path')
            url = 'https://huggingface.co/datasets/' + source_spec['repo_id'] + '/resolve/' + REVISION + '/' + quote(prefix + relative)
            downloaded = download(url, raw / relative, item['sha256'])
            if downloaded.stat().st_size != item['size']:
                raise ValueError('JobBench source size mismatch')
        # Preserve native validation before task generation, including every
        # rubric, reference input and instructions file. No oracle is fabricated.
        rubric = json.loads((raw / 'RUBRICS.json').read_text())
        if not rubric.get('rubrics'):
            raise ValueError('JobBench source has no rubric blocks')
        destination = output_dir / task['id']
        _render(spec, task, source, raw, destination, renderer)
        result.append({'id': task['id'], 'path': str(destination.resolve())})
    return result


def verifier_failure(spec: dict, trial_dir: Path) -> dict | None:
    """Use only the native private verifier report after a missing reward."""
    if spec.get('source', {}).get('commit') != COMMIT:
        return None
    path = Path(trial_dir) / 'verifier/verification.json'
    if not path.is_file() or path.is_symlink():
        return None
    if path.stat().st_size > 1024 * 1024:
        return {'status': 'infra_error', 'error': 'JobBench private verifier failure report is too large'}
    try:
        report = json.loads(path.read_text())
        if not isinstance(report, dict) or report.get('status') not in {'ok', 'error'}:
            raise ValueError('Invalid report')
        if report['status'] == 'ok':
            return {'status': 'infra_error', 'error': 'JobBench verifier reported success without a native reward'}
        detail = report['error']
        if not isinstance(detail, str) or not detail:
            raise ValueError('Missing error')
    except (OSError, ValueError, KeyError):
        return {'status': 'infra_error', 'error': 'Malformed private JobBench verifier failure report'}
    lower = detail.lower()
    if re.search(r'\b429\b|rate.?limit|quota|usage limit|resource_exhausted', lower):
        status = 'quota_exceeded'
    elif re.search(r'\b(?:401|403)\b|unauthorized|authentication|invalid.api.key|missing.api.key', lower):
        status = 'auth_error'
    else:
        status = 'infra_error'
    return {'status': status, 'error': 'JobBench judge: ' + detail[:6000]}
