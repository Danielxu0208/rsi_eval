"""Materialize the complete pinned Frontier v1 matrix using native metadata."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import tarfile

from .common import download
from .frontier_eng import SOURCE_COMMIT, REPOSITORY, grader_platform, write

VERSION = 'v1-bafa7d5'
PROTOCOL = 'harbor-native-unified-100-v2'
ARCHIVE_SHA256 = '3ccf7ec9506aaf7491c790c9584175bc65c53fbc0c1b676762a8812e18a9b98b'
_ALLOWED = {'runtime_image', 'platform', 'gpu', 'assets_dir', 'env', 'agent_timeout_sec',
            'memory_mb', 'cpus', 'build_timeout_sec', 'task_overrides', 'candidate_max_bytes'}


def runtime_options(spec, task=None):
    options = dict(spec.get('runtime_options') or {})
    unknown = set(options) - _ALLOWED
    if unknown:
        raise ValueError('unknown Frontier benchmark_runtime options: ' + ', '.join(sorted(unknown)))
    overrides = options.pop('task_overrides', {})
    if not isinstance(overrides, dict):
        raise ValueError('task_overrides must be an object keyed by task ID')
    if task is not None:
        specific = overrides.get(task['id'], {})
        if not isinstance(specific, dict) or set(specific) - (_ALLOWED - {'task_overrides'}):
            raise ValueError('invalid Frontier task runtime override: ' + task['id'])
        options.update(specific)
    if 'platform' not in options:
        options['platform'] = grader_platform()
    selected_platform = options['platform']
    if selected_platform not in ('linux/amd64', 'linux/arm64'):
        raise ValueError('Frontier platform must be linux/amd64 or linux/arm64')
    image = options.get('runtime_image')
    if image is not None and (not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]*', image)):
        raise ValueError('runtime_image must be a Docker image reference')
    if 'gpu' in options and type(options['gpu']) is not bool:
        raise ValueError('gpu must be true or false')
    if 'candidate_max_bytes' in options and (type(options['candidate_max_bytes']) is not int or options['candidate_max_bytes'] <= 0):
        raise ValueError('candidate_max_bytes must be a positive integer')
    for key in ('agent_timeout_sec', 'memory_mb', 'cpus', 'build_timeout_sec'):
        if key in options and (type(options[key]) not in (int, float) or options[key] <= 0):
            raise ValueError(key + ' must be a positive number')
    env = options.get('env', {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', k) or not isinstance(v, str) for k, v in env.items()):
        raise ValueError('env must map uppercase environment names to strings')
    if any(re.search(r'(?:TOKEN|SECRET|PASSWORD|API_KEY|AUTH)', key) for key in env):
        raise ValueError('Frontier grader env must not contain authentication or credential variables')
    return options


def preflight(spec):
    checks = {}
    for task in spec['tasks']:
        options = runtime_options(spec, task)
        tid = task['id']
        recipe = task['runtime_recipe']
        supported = options['platform'] in recipe['native_architectures']
        checks[tid + ':native_platform'] = {'ok': supported, 'detail':
            'Native supported architectures: ' + ', '.join(recipe['native_architectures']) + '; selected ' + options['platform']}
        default = os.environ.get('DOCKER_DEFAULT_PLATFORM')
        checks[tid + ':compose_platform'] = {'ok': not default or default == options['platform'],
            'detail': 'Unset conflicting DOCKER_DEFAULT_PLATFORM before running task-specific Docker platforms.'}
        if recipe['gpu']:
            checks[tid + ':gpu_configuration'] = {'ok': options.get('gpu') is True,
                'detail': 'Set benchmark_runtime.gpu=true and use an NVIDIA-enabled Docker daemon. Availability is checked inside the native runtime.'}
        assets = options.get('assets_dir')
        if assets:
            checks[tid + ':assets_directory'] = {'ok': Path(assets).expanduser().is_dir(),
                'detail': 'Input asset directory must exist; files are copied, hashed and checked against frozen source conflicts.'}
        checks[tid + ':native_mapping'] = {'ok': True,
            'detail': 'Mapped to the original unified runner and task metadata; native evaluator timeout ' + str(task['native_evaluator_timeout_sec']) + ' seconds.',
            'dependencies': recipe}
    return checks


def extract_source(archive, destination, expected_files):
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as source:
        for member in source:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != 'Frontier-Engineering-' + SOURCE_COMMIT or '..' in parts or PurePosixPath(member.name).is_absolute():
                raise ValueError('unsafe Frontier archive entry')
            if member.isdir():
                continue
            relative = '/'.join(parts[1:])
            if not member.isfile() or relative not in expected_files:
                raise ValueError('unexpected Frontier archive file: ' + relative)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream, target.open('xb') as output:
                shutil.copyfileobj(stream, output)
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected_files[relative]:
                raise ValueError('Frontier archive checksum mismatch: ' + relative)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    missing = [name for name in expected_files if not (destination / name).is_file()]
    if missing:
        raise ValueError('incomplete Frontier source archive: ' + missing[0])


def metadata_lines(path):
    return [text for line in path.read_text().splitlines() if (text := line.strip()) and not text.startswith('#')] if path.is_file() else []


def validate_task(task, root):
    base = root / 'benchmarks' / task['id']
    native = task['native_metadata']
    for key in ('agent_files', 'copy_files', 'readonly_files', 'artifact_files'):
        if metadata_lines(base / 'frontier_eval' / (key + '.txt')) != native[key]:
            raise ValueError('native task metadata mismatch: ' + task['id'] + '/' + key)
    if metadata_lines(base / 'frontier_eval/initial_program.txt') != [native['initial_program']]:
        raise ValueError('native initial program identity changed: ' + task['id'])
    if not (base / native['initial_program']).is_file():
        raise ValueError('missing native initial program: ' + task['id'])
    if '\n'.join(metadata_lines(base / 'frontier_eval/eval_command.txt')) != native['eval_command']:
        raise ValueError('native evaluation command changed: ' + task['id'])


def _copy(root, relative, destination):
    source = root / relative
    target = destination / relative
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _asset_copy(asset_root, target, official_files):
    manifest = {}
    for path in sorted(asset_root.rglob('*')):
        if path.is_symlink():
            raise ValueError('asset snapshots must contain regular files, not symlinks: ' + str(path))
        if not path.is_file() or '.git' in path.parts:
            continue
        relative = path.relative_to(asset_root).as_posix()
        if not (relative.startswith('benchmarks/') or relative.startswith('third_party/')):
            raise ValueError('assets_dir must use upstream benchmarks/ or third_party/ layout: ' + relative)
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(block)
        digest = hasher.hexdigest()
        if relative in official_files and digest != official_files[relative]:
            raise ValueError('assets_dir cannot replace frozen upstream source: ' + relative)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        manifest[relative] = digest
    return manifest


def _task(spec, task, source, destination):
    options = runtime_options(spec, task)
    recipe = task['runtime_recipe']
    runtime_file = Path(__file__).with_name('frontier_eng_runtime.py')
    full_runtime = Path(__file__).with_name('frontier_eng_full_runtime.py')
    token = secrets.token_hex(32)
    context_digest = hashlib.sha256((token + json.dumps(task, sort_keys=True) + json.dumps(options, sort_keys=True)).encode()
                                     + runtime_file.read_bytes() + full_runtime.read_bytes()).hexdigest()
    environment = destination / 'environment'
    agent_dir = environment / ('agent-' + context_digest)
    grader_dir = environment / ('grader-' + context_digest)
    upstream = grader_dir / 'upstream'
    # Keep all cross-task family helpers and the entire native driver. No custom
    # per-task grader dispatch is needed, including external {repo_root} commands.
    for relative in ('frontier_eval', 'scripts', 'patches', 'benchmarks/_shared',
                     'benchmarks/' + task['id'].split('/')[0]):
        _copy(source, relative, upstream)
    # Shared source hashes bind every shipped grader/helper, not just metadata.
    frozen = {name: digest for name, digest in spec['source']['files'].items() if (upstream / name).is_file()}
    assets = _asset_copy(Path(options['assets_dir']).expanduser().resolve(), upstream, spec['source']['files']) if options.get('assets_dir') else {}
    public = agent_dir / 'public'
    public.mkdir(parents=True)
    base = source / 'benchmarks' / task['id']
    public_entries = list(dict.fromkeys([*task['native_metadata']['agent_files'], task['native_metadata']['initial_program']]))
    for relative in public_entries:
        if not (base / relative).exists():
            raise ValueError('native agent context is missing: ' + task['id'] + '/' + relative)
        _copy(base, relative, public)
    for folder in (agent_dir, grader_dir, destination / 'tests'):
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(runtime_file, folder / 'frontier_runtime.py')
        shutil.copyfile(full_runtime, folder / 'frontier_full_runtime.py')
    broker = {'engine': 'native-unified', 'task_id': task['id'], 'task': task,
              'source_root': '/opt/frontier/upstream', 'source_commit': SOURCE_COMMIT,
              'files': frozen, 'candidate_path': task['native_metadata']['initial_program'],
              'protocol': PROTOCOL, 'candidate_budget': 100, 'verifier_token': token,
              'candidate_max_bytes': options.get('candidate_max_bytes', 4 * 1024 * 1024),
              'state_dir': '/state', 'environment': options.get('env', {})}
    write(grader_dir / 'broker.json', json.dumps(broker, indent=2) + '\n')
    build_config = {'source_root': broker['source_root'], 'source_commit': SOURCE_COMMIT,
                    'task_id': task['id'].split('/')[0], 'task': {'runtime_recipe': recipe}}
    write(grader_dir / 'runtime-build.json', json.dumps(build_config, sort_keys=True, indent=2) + '\n')
    # Drop namespace capabilities in actual candidate processes. The outer
    # trusted driver explicitly adds only its needed SYS_ADMIN flag afterward.
    write(grader_dir / 'bwrap', '#!/bin/sh\nexec /usr/bin/bwrap --cap-drop ALL "$@"\n')
    (grader_dir / 'bwrap').chmod(0o755)
    custom_image = options.get('runtime_image')
    base_image = custom_image or 'python:' + recipe['python'] + '-slim-bookworm'
    setup = ''
    if not custom_image:
        packages = ' '.join(shlex.quote(name) for name in [*recipe['apt'], *recipe.get('apt_by_platform', {}).get(options['platform'], [])])
        setup = f'RUN apt-get update && apt-get install -y --no-install-recommends {packages} && rm -rf /var/lib/apt/lists/*\n'
    build = f'''FROM {base_image}
{setup}COPY upstream /opt/frontier/upstream
COPY frontier_runtime.py /opt/frontier_runtime.py
COPY frontier_full_runtime.py /opt/frontier_full_runtime.py
COPY bwrap /usr/local/bin/bwrap
COPY runtime-build.json /opt/frontier/runtime-build.json
WORKDIR /opt/frontier/upstream
ENV PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 FRONTIER_MALLOC_TOOLCHAIN=/opt/frontier/toolchain
RUN python -I /opt/frontier_full_runtime.py bootstrap /opt/frontier/runtime-build.json before
'''
    if not custom_image:
        for step in recipe.get('pip_preinstall', []):
            if step.get('platforms') and options['platform'] not in step['platforms']:
                continue
            command = ['python', '-m', 'pip', 'install', '--no-cache-dir']
            if step.get('index_url'):
                command += ['--index-url', step['index_url']]
            command += ['--config-settings=' + key + '=' + value for key, value in step.get('config_settings', {}).items()]
            command += step['packages']
            build += 'RUN ' + shlex.join(command) + '\n'
        reqs = ['-r ' + shlex.quote('/opt/frontier/upstream/' + name) for name in recipe['requirements']]
        build += 'RUN python -m pip install --no-cache-dir ' + ' '.join([*(shlex.quote(name) for name in recipe['packages']), *reqs]) + '\n'
    build += '''RUN python -I /opt/frontier_full_runtime.py bootstrap /opt/frontier/runtime-build.json after
RUN mkdir -m 700 /state
COPY broker.json /opt/frontier/broker.json
CMD ["python", "-I", "/opt/frontier_runtime.py", "serve", "/opt/frontier/broker.json"]
'''
    write(grader_dir / 'Dockerfile', build)
    # The agent sees exactly native prompt files and the seed, never the private
    # reference tree, build metadata, asset truth or verification token.
    write(agent_dir / 'Dockerfile', '''FROM python:3.12.9-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git procps build-essential && rm -rf /var/lib/apt/lists/*
COPY public /workspace
COPY frontier_runtime.py /opt/frontier_runtime.py
WORKDIR /workspace
''')
    shutil.copyfile(agent_dir / 'Dockerfile', environment / 'Dockerfile')
    gpu = '\n    gpus: all' if recipe['gpu'] else ''
    write(environment / 'docker-compose.yaml', f'''services:
  main:
    build:
      context: ${{CONTEXT_DIR}}/{agent_dir.name}
    networks: [default, frontier-private]
    depends_on:
      frontier-grader:
        condition: service_healthy
  frontier-grader:
    build:
      context: ${{CONTEXT_DIR}}/{grader_dir.name}
    platform: {options['platform']}
    networks: [frontier-private]
    cap_add: [SYS_ADMIN]
    read_only: true
    tmpfs:
      - /tmp:rw,nosuid,nodev
      - /state:rw,nosuid,nodev,mode=0700
    security_opt: [seccomp=unconfined, apparmor=unconfined, systempaths=unconfined]{gpu}
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]
      interval: 2s
      timeout: 5s
      retries: 30
    mem_limit: {int(options.get('memory_mb', 8192))}m
    cpus: {options.get('cpus', 2)}
networks:
  frontier-private:
    internal: true
''')
    initial = task['native_metadata']['initial_program']
    prompt = f'''# Frontier-Eng: {task['id']}

Read the original task context under `/workspace`. The files listed below are
exactly the native agent_files plus its initial program. Follow their constraints.

''' + '\n'.join('- `' + name + '`' for name in public_entries) + f'''

Modify `/workspace/{initial}`. The native task runner executes your source with
its original evaluator, inputs, file layout and {task['native_evaluator_timeout_sec']}-second evaluation timeout.

Inspect the initial baseline and remaining proposal budget:

```sh
python -I /opt/frontier_runtime.py feedback
```

Evaluate a candidate using the frozen native environment:

```sh
python -I /opt/frontier_runtime.py feedback /workspace/{initial}
```

There are at most 99 feedback proposals plus one final candidate, matching the
100-candidate allowance in the released matrix. The initial baseline is separate.
Every evaluated proposal consumes a slot, including invalid candidates. The
broker preserves the best feasible native combined_score observed within budget.
Higher combined_score is better. Do not edit the task's frozen evaluator files.
Use feedback after a source modification and leave the final source at its
original path. Final verification also records a diagnostic rerun of the selected
source. It does not provide extra optimization feedback.
'''
    write(destination / 'instruction.md', prompt)
    timeout = task['native_evaluator_timeout_sec']
    write(destination / 'task.toml', f'''version = "1.0"
[metadata]
benchmark = "frontier-eng"
record_id = "{task['id']}"
source_commit = "{SOURCE_COMMIT}"
protocol = "{PROTOCOL}"
grader_platform = "{options['platform']}"
collection = "{task['collection']}"
[agent]
timeout_sec = {float(options.get('agent_timeout_sec', 604800))}
[verifier]
timeout_sec = {float(3 * timeout + 300)}
[environment]
build_timeout_sec = {float(options.get('build_timeout_sec', 7200))}
cpus = {int(options.get('cpus', 2))}
memory_mb = {int(options.get('memory_mb', 8192))}
storage_mb = 32768
''')
    write(destination / 'tests/verifier.json', json.dumps({'verifier_token': token, 'request_timeout_sec': 3 * timeout + 250,
                                                         'candidate_max_bytes': broker['candidate_max_bytes']}) + '\n')
    write(destination / 'tests/test.sh', f'#!/bin/bash\nset -euo pipefail\npython -I /tests/frontier_runtime.py verify /tests/verifier.json /workspace/{initial} /logs/verifier\n')
    write(destination / 'solution/solve.sh', '#!/bin/bash\nset -euo pipefail\n# Leave the original native seed in place for final baseline verification.\ntrue\n')
    for name in ('tests/test.sh', 'solution/solve.sh'):
        (destination / name).chmod(0o755)
    write(destination / 'provenance.json', json.dumps({'repository': REPOSITORY, 'commit': SOURCE_COMMIT,
        'source_archive_sha256': ARCHIVE_SHA256, 'task': task, 'runtime_options': options,
        'copied_asset_sha256': assets, 'frozen_source_sha256': frozen,
        'protocol': spec['protocol'], 'private_execution_nonce': 'generated separately per trial'}, indent=2) + '\n')


def prepare(spec, output_dir):
    if spec.get('version') != VERSION or spec.get('protocol', {}).get('version') != PROTOCOL or spec.get('source', {}).get('archive_sha256') != ARCHIVE_SHA256 or spec.get('source', {}).get('commit') != SOURCE_COMMIT:
        raise ValueError('full Frontier requires the pinned v1 native runner profile')
    failures = [name + ': ' + row['detail'] for name, row in preflight(spec).items() if not row['ok']]
    if failures:
        raise ValueError('; '.join(failures))
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    archive = download(spec['source']['archive_url'], root / 'source.tar.gz', ARCHIVE_SHA256)
    source = root / 'source' / SOURCE_COMMIT
    extract_source(archive, source, spec['source']['files'])
    result = []
    for task in spec['tasks']:
        validate_task(task, source)
        destination = root / 'tasks' / ('frontier-v1-' + task['id'].lower().replace('/', '-').replace('_', '-'))
        destination.mkdir(parents=True, exist_ok=False)
        _task(spec, task, source, destination)
        result.append({'id': task['id'], 'path': str(destination)})
    return result
