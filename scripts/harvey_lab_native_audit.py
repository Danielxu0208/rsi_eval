#!/usr/bin/env python3
"""Audit every pinned LAB task and exercise the native scorer without model calls."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import tempfile

from rsi_eval.materializers import harvey_lab as adapter
from rsi_eval.materializers.harvey_lab_verifier import validate_scores


def audit(source: Path) -> dict:
    from importlib.resources import files
    from lab_core.evaluation import run_eval
    from lab_core.harness.run import load_task
    spec = json.loads(files('rsi_eval').joinpath('data/catalog_harvey_lab.json').read_text())[0]
    actual_commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual_commit != adapter.COMMIT:
        raise ValueError('source checkout must match ' + adapter.COMMIT)
    tracked = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z', 'tasks'], text=True).split('\0')
    native_ids = sorted(str(Path(name).parent.relative_to('tasks')) for name in tracked if name.endswith('/task.json'))
    if native_ids != sorted(task['id'] for task in spec['tasks']):
        raise ValueError('catalog does not cover all native tasks')
    for relative, digest in spec['source']['files'].items():
        if adapter.sha256(source / relative) != digest:
            raise ValueError('native source modified: ' + relative)
    os.environ['LAB_ROOT'] = str(source.resolve())
    areas = Counter()
    for task in spec['tasks']:
        config_file = source / 'tasks' / task['id'] / 'task.json'
        if adapter.sha256(config_file) != task['task_sha256']:
            raise ValueError('task modified: ' + task['id'])
        native = load_task(task['id'])
        run_eval.validate_task_config(native['config'], config_file)
        if Path(native['docs_dir']).resolve() != (source / task['documents_path']).resolve():
            raise ValueError('document mapping differs: ' + task['id'])
        areas[task['practice_area']] += 1
    for relative, expected in spec['source']['document_trees'].items():
        manifest = adapter.tree_manifest(source / relative)
        if adapter.tree_hash(manifest) != expected['sha256'] or len(manifest) != expected['file_count']:
            raise ValueError('document snapshot differs: ' + relative)
    # Synthetic transport fixture only. No benchmark answer or benchmark score is fabricated.
    with tempfile.TemporaryDirectory(prefix='harvey-native-fixture-') as temp:
        root = Path(temp)
        task_path = root / 'tasks/fixture/scoring/task.json'
        task_path.parent.mkdir(parents=True)
        task_path.write_text(json.dumps({'title': 'Synthetic adapter fixture', 'instructions': 'Synthetic only',
            'criteria': [{'id': name, 'title': name, 'match_criteria': 'Fixture', 'deliverables': ['report.md']} for name in ('A', 'B')]}))
        output = root / 'results/fixture/output'
        output.mkdir(parents=True)
        (output / 'report.md').write_text('Synthetic adapter output. Not a legal benchmark result.')
        class FixtureJudge:
            def __init__(self, model): self.model = model
            def evaluate_from_file(self, prompt_name, variables):
                assert prompt_name == 'rubric_criterion'
                assert 'Synthetic adapter output' in variables['agent_output']
                return {'verdict': 'fail' if self.model == 'fixture-b' and variables['criterion_title'] == 'B' else 'pass',
                        'reasoning': 'Deterministic transport fixture; no model call.'}
        original_judge = run_eval.Judge
        os.environ['LAB_ROOT'] = str(root)
        try:
            run_eval.Judge = FixtureJudge
            dual = run_eval.evaluate_run_dual('fixture', 'fixture/scoring', parallel=1, judge_models=('fixture-a', 'fixture-b'))
            metrics = validate_scores(dual, ['fixture-a', 'fixture-b'], 2)
            assert metrics['reward'] == .5 and metrics['criterion_pass_rate'] == .75
            assert (root / 'results/fixture/scores_fixture-a.json').is_file()
            assert (root / 'results/fixture/scores_fixture-b.json').is_file()
        finally:
            run_eval.Judge = original_judge
            os.environ['LAB_ROOT'] = str(source.resolve())
    return {'source_commit': actual_commit, 'profile': spec['version'], 'coverage': spec['coverage'],
            'native_loader_and_schema_tasks': len(native_ids), 'native_document_roots_hashed': len(spec['source']['document_trees']),
            'native_document_files_hashed': sum(d['file_count'] for d in spec['source']['document_trees'].values()),
            'source_file_count': len(spec['source']['files']), 'practice_areas': dict(sorted(areas.items())),
            'native_dual_scorer_synthetic_fixture': {'passed': True, 'fixture_only': True, 'model_calls': 0,
                'reward': metrics['reward'], 'criterion_pass_rate': metrics['criterion_pass_rate']},
            'live_judge_scoring': {'status': 'blocked_missing_credentials', 'required_default': ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY'],
                'note': 'No provider request was made. Codex subscription credentials do not authenticate either native judge API.'},
            'published_gold_oracle': False, 'benchmark_model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.source.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('native_loader_and_schema_tasks', 'native_document_files_hashed', 'benchmark_model_calls')}))


if __name__ == '__main__':
    main()
