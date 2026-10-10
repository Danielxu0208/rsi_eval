#!/usr/bin/env python3
"""Replay published EngDesign samples without model calls; run code only in Docker."""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def literal_response(node):
    # The upstream .jsonl logs contain Python reprs, including Pydantic constructors.
    # Read keyword fields as data; never eval or import the submitted source here.
    if isinstance(node, ast.Call) and not node.args and all(k.arg for k in node.keywords):
        return {k.arg: literal_response(k.value) for k in node.keywords}
    if isinstance(node, ast.Dict):
        return {literal_response(k): literal_response(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [literal_response(item) for item in node.elts]
    return ast.literal_eval(node)


def prepare(args):
    from rsi_eval import BenchmarkSelection
    from rsi_eval.benchmarks import resolve
    from rsi_eval.materializers import engdesign_full
    selection = None if args.tasks is None else tuple(args.tasks)
    spec = resolve(BenchmarkSelection('engdesign-open', engdesign_full.VERSION, selection))
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    cache = root / 'cache'
    cache.mkdir()
    existing_cache = os.environ.get('RSI_EVAL_SOURCE_CACHE')
    if existing_cache:
        for digest in spec['source'].get('simulator_assets', {}).get('files', {}).values():
            cached = Path(existing_cache).expanduser() / digest
            if cached.is_file() and sha(cached) == digest:
                shutil.copyfile(cached, cache / digest)
    for name, expected in spec['source']['files'].items():
        source = args.upstream / name
        if sha(source) != expected:
            raise ValueError('upstream checkout does not match the pinned source: ' + name)
        shutil.copyfile(source, cache / expected)
    samples = root / 'samples'
    samples.mkdir()
    manifest = []
    for task in spec['tasks']:
        choices = []
        directory = args.upstream / 'EngDesign-Open' / task['id']
        for path in sorted((directory / 'logs').glob('*.jsonl')):
            try:
                node = ast.parse(path.read_text().strip(), mode='eval').body
                fields = {ast.literal_eval(k): v for k, v in zip(node.keys, node.values)}
                response = literal_response(fields['response'])
                passed = bool(ast.literal_eval(fields.get('passed', ast.Constant(False))))
                if isinstance(response, dict):
                    choices.append((passed, response, path))
            except (ValueError, SyntaxError, KeyError, AttributeError):
                continue
        if not choices:
            raise ValueError('no parseable published response for ' + task['id'])
        passed, response, path = next((choice for choice in choices if choice[0]), choices[0])
        answer = samples / (task['id'] + '.json')
        answer.write_text(json.dumps(response, allow_nan=False) + '\n')
        manifest.append({'task_id': task['id'], 'upstream_log': str(path.relative_to(args.upstream)),
                         'log_sha256': sha(path), 'submission_sha256': sha(answer), 'published_passed': passed})
    (samples / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    # This audit-local cache contains only public source files and is hash checked.
    old_cache = os.environ.get('RSI_EVAL_SOURCE_CACHE')
    try:
        os.environ['RSI_EVAL_SOURCE_CACHE'] = str(cache)
        engdesign_full.prepare(spec, root / 'prepared')
    finally:
        if old_cache is None:
            os.environ.pop('RSI_EVAL_SOURCE_CACHE', None)
        else:
            os.environ['RSI_EVAL_SOURCE_CACHE'] = old_cache
    shutil.copyfile(Path(engdesign_full.__file__).with_name('engdesign_runtime.py'), root / 'runtime.py')
    shutil.copyfile(__file__, root / 'audit.py')
    scientific = root / 'scientific-image'
    scientific.mkdir()
    (scientific / 'Dockerfile').write_text(engdesign_full.dockerfile('python'))
    webots = root / 'webots-image'
    for task in spec['tasks']:
        if task['runtime_family'] == 'webots':
            grader = next((root / 'prepared/tasks' / ('engdesign-' + task['id'].lower().replace('_', '-')) / 'environment').glob('grader-*'))
            webots.mkdir()
            shutil.copytree(grader / 'webots', webots / 'assets')
            (webots / 'Dockerfile').write_text(engdesign_full.dockerfile('webots') + 'COPY assets /usr/local/webots\n')
            break
    print(json.dumps({'prepared_tasks': len(spec['tasks']), 'output_dir': str(root), 'model_calls': 0}))


def replay(args):
    if not Path('/.dockerenv').exists() or os.geteuid() != 0:
        raise RuntimeError('run replay in the documented credential-free Docker container')
    runtime_file = args.prepared.parent / 'runtime.py'
    module_spec = importlib.util.spec_from_file_location('engdesign_audit_runtime', runtime_file)
    runtime = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = runtime
    module_spec.loader.exec_module(runtime)
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for task in sorted((args.prepared / 'tasks').iterdir()):
        grader = next((task / 'environment').glob('grader-*'))
        config = json.loads((grader / 'config.json').read_text())
        if (config['runtime_family'] == 'webots') != (args.family == 'webots'):
            continue
        name = config['record_id']
        config.update(source_dir=str(grader / 'native'), timeout_sec=args.timeout)
        started = time.monotonic()
        probe = runtime.worker(config, None, probe=True)
        if probe['status'] != 'ready':
            result = {'status': 'verifier_error', 'error': 'startup probe failed'}
        else:
            response = json.loads((args.samples / (name + '.json')).read_text())
            result = runtime.worker(config, response)
        result.update(record_id=name, preflight=probe, elapsed_sec=time.monotonic() - started)
        (args.output / (name + '.json')).write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
        results.append(result)
        print(name, result['status'], result.get('passed'), result.get('native_score'), flush=True)
    (args.output / 'summary.json').write_text(json.dumps(results, indent=2, allow_nan=False) + '\n')
    return 0 if results and all(r['status'] == 'graded' and r.get('native_completed') for r in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--upstream', type=Path, required=True, help='official checkout at the catalog commit')
    p.add_argument('--output', type=Path, required=True, help='new audit staging directory')
    p.add_argument('--tasks', nargs='+')
    p = sub.add_parser('replay')
    p.add_argument('--prepared', type=Path, default=Path('/audit/prepared'))
    p.add_argument('--samples', type=Path, default=Path('/audit/samples'))
    p.add_argument('--output', type=Path, default=Path('/results'))
    p.add_argument('--family', choices=['scientific', 'webots'], required=True)
    p.add_argument('--timeout', type=int, default=90)
    args = parser.parse_args()
    return prepare(args) if args.command == 'prepare' else replay(args)


if __name__ == '__main__':
    raise SystemExit(main())
