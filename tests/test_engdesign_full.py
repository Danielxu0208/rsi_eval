"""Offline checks for complete EngDesign selection and native grader transport."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from rsi_eval import BenchmarkSelection, TargetSpec, preview
from rsi_eval.materializers import common, engdesign, engdesign_full, engdesign_runtime

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/engdesign' / engdesign.SOURCE_COMMIT


@pytest.fixture
def full():
    catalog = json.loads((ROOT / 'src/rsi_eval/data/catalog_engdesign.json').read_text())
    return next(item for item in catalog if item['version'] == engdesign_full.VERSION)


@pytest.fixture
def local_sources(monkeypatch):
    monkeypatch.delenv('DOCKER_DEFAULT_PLATFORM', raising=False)
    monkeypatch.delenv('RSI_EVAL_SOURCE_CACHE', raising=False)
    def source(url, timeout):
        prefix = 'https://raw.githubusercontent.com/AGI4Engineering/EngDesign/' + engdesign.SOURCE_COMMIT + '/'
        assert url.startswith(prefix)
        return io.BytesIO((FIXTURE / url.removeprefix(prefix)).read_bytes())
    monkeypatch.setattr(common.urllib.request, 'urlopen', source)


def prepare(full, output, ids=('XY_01',)):
    full['tasks'] = [task for task in full['tasks'] if task['id'] in ids]
    return [Path(t['path']) for t in engdesign.prepare(full, output)]


def grade(task, answer, output):
    grader = next((task / 'environment').glob('grader-*'))
    return subprocess.run([sys.executable, '-I', str(grader / 'runtime.py'), 'grade',
                           str(grader / 'native'), str(answer), str(output), str(grader / 'config.json')],
                          capture_output=True, text=True, timeout=30)


def test_all_released_tasks_are_selectable_independently_of_legacy_subset(full, tmp_path):
    catalog = json.loads((ROOT / 'src/rsi_eval/data/catalog_engdesign.json').read_text())
    legacy = next(item for item in catalog if item['version'].startswith('open-subset'))
    expected = set(legacy['coverage']['unsupported_task_ids']) | {'AB_01', 'XY_01'}
    assert len(expected) == len(full['tasks']) == 67
    assert {t['id'] for t in full['tasks']} == expected
    assert legacy['lifecycle'] == 'legacy-subset'
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    (candidate / 'wrapper.py').write_text('# no model\n')
    result = preview(target=TargetSpec(candidate, 'harbor.agents.installed.codex:Codex'),
                     benchmark=BenchmarkSelection(full['name'], full['version']), output_dir=tmp_path / 'run')
    assert len(result['benchmark']['task_ids']) == 67
    assert not (tmp_path / 'run').exists()


def test_manifest_has_complete_runtime_inputs_and_private_references(full):
    tasks = {t['id']: t for t in full['tasks']}
    assert sum(t['runtime_family'] == 'webots' for t in tasks.values()) == 2
    assert sum(t['runtime_family'] == 'octave' for t in tasks.values()) == 4
    assert sum(t['runtime_family'] == 'iverilog' for t in tasks.values()) == 9
    for task in tasks.values():
        assert task['record_id'] == task['id']
        assert 'output_structure.py' in task['public_files']
        assert 'evaluate.py' not in task['public_files']
        assert not any('ground_truth' in name or 'solution' in name for name in task['public_files'])
        assert {'evaluate.py', 'output_structure.py'} <= set(task['grader_files'])
        assert set(task['public_files']) | set(task['grader_files']) <= set(task['source_files'])
        assert 'LLM_prompt.txt' in task['source_files']
        for name in task['source_files']:
            assert len(full['source']['files'][f"EngDesign-Open/{task['id']}/{name}"]) == 64
    assert 'simulation_results/ground_truth.json' in tasks['DL_01']['grader_files']
    assert 'simulation_results/ground_truth_layout.json' in tasks['DL_02']['grader_files']
    assert 'solution.txt' in tasks['YX_01']['grader_files']
    assert 'sobel3.png' not in tasks['JY_02']['public_files']  # Generated output is not an input image.
    assert 'original_image.png' in tasks['WJ_01']['grader_files']
    assert 'images/noisy_image_s&p.png' in tasks['WJ_01']['public_files']
    assert 'controllers/robot_controller/robot_controller.py' in tasks['XZ_03']['grader_files']
    assert 'ffmpeg' in engdesign_full.dockerfile('python')
    assert 'octave-control' in engdesign_full.dockerfile('octave')
    assert 'iverilog' in engdesign_full.dockerfile('iverilog')


def test_full_materializer_keeps_all_scoring_code_and_token_out_of_agent(full, local_sources, tmp_path):
    task, = prepare(full, tmp_path / 'materialized')
    agent = next((task / 'environment').glob('agent-*'))
    grader = next((task / 'environment').glob('grader-*'))
    expected = FIXTURE / 'EngDesign-Open/XY_01'
    assert (grader / 'native/evaluate.py').read_bytes() == (expected / 'evaluate.py').read_bytes()
    assert (agent / 'public/font_rom.sv').read_bytes() == (expected / 'font_rom.sv').read_bytes()
    assert sorted(p.name for p in agent.iterdir()) == ['Dockerfile', 'public']
    assert not (agent / 'public/font_rom_data.py').exists()
    token = json.loads((grader / 'config.json').read_text())['verifier_token']
    assert token not in ''.join(p.read_text() for p in agent.rglob('*') if p.is_file())
    compose = (task / 'environment/docker-compose.yaml').read_text()
    assert 'internal: true' in compose and 'service_healthy' in compose
    assert 'privileged' not in compose and 'docker.sock' not in compose and 'codex' not in compose
    config = tomllib.loads((task / 'task.toml').read_text())
    assert config['metadata']['scope'] == 'full-upstream-open'
    assert config['verifier']['timeout_sec'] == 900
    assert (grader / 'LICENSE').read_bytes() == (FIXTURE / 'LICENSE').read_bytes()
    assert (task / 'tests/test.sh').stat().st_mode & 0o111
    assert (task / 'solution/solve.sh').stat().st_mode & 0o111


def test_full_native_runner_preserves_pass_and_partial_score(full, local_sources, tmp_path):
    pytest.importorskip('pydantic')
    task, = prepare(full, tmp_path / 'materialized')
    answer, output = tmp_path / 'answer.json', tmp_path / 'result.json'
    subprocess.run([sys.executable, '-I', str(task / 'solution/solve.py'), str(answer)], check=True)
    run = grade(task, answer, output)
    result = json.loads(output.read_text())
    assert run.returncode == 0, result
    assert result['rewards'] == {'reward': 1.0, 'native_score': 100.0, 'confidence': 100.0}
    assert result['schema_validated'] and result['native_called'] and result['native_completed']
    assert result['submission_sha256'] == hashlib.sha256(answer.read_bytes()).hexdigest()
    submission = json.loads(answer.read_text())
    submission['tetromino_pattern']['visual'] = ['wrong'] * 4
    answer.write_text(json.dumps(submission))
    run = grade(task, answer, output)
    assert run.returncode == 0
    result = json.loads(output.read_text())
    assert result['rewards'] == {'reward': 0.0, 'native_score': 70.0, 'confidence': 100.0}
    assert result['native_return'][0] is False


@pytest.mark.parametrize('text', ['{}', 'null', '{"reasoning": NaN}', '{"config": {"rotation": 1e999}}',
                                  '{"config": {"rotation": -1e999}}', 'not json'])
def test_invalid_submission_never_invokes_native_grader(full, local_sources, tmp_path, text):
    pytest.importorskip('pydantic')
    task, = prepare(full, tmp_path / 'materialized')
    answer, output = tmp_path / 'answer.json', tmp_path / 'result.json'
    answer.write_text(text)
    run = grade(task, answer, output)
    result = json.loads(output.read_text())
    assert run.returncode == 0
    assert result['status'] == 'invalid_submission'
    assert result['rewards'] == {'reward': 0.0}
    assert result['native_score'] is None
    assert not result['native_called']


def test_native_source_corruption_has_no_reward(full, local_sources, tmp_path):
    task, = prepare(full, tmp_path / 'materialized')
    grader = next((task / 'environment').glob('grader-*'))
    (grader / 'native/evaluate.py').write_text('raise RuntimeError("changed")')
    output = tmp_path / 'result.json'
    run = grade(task, tmp_path / 'answer.json', output)
    assert run.returncode == 1
    result = json.loads(output.read_text())
    assert result['status'] == 'verifier_error' and 'rewards' not in result
    assert 'checksum' in result['error']


def test_full_materializer_rejects_injected_task_before_network(full, tmp_path, monkeypatch):
    monkeypatch.setattr(common.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('network'))
    full['tasks'][0]['id'] = '../../arbitrary'
    with pytest.raises(ValueError, match='released catalog'):
        engdesign.prepare(full, tmp_path)
    assert not list(tmp_path.iterdir())


def test_runtime_options_are_explicit_and_do_not_accept_credentials(full, monkeypatch):
    monkeypatch.delenv('DOCKER_DEFAULT_PLATFORM', raising=False)
    full['runtime_options'] = {'webots_image': 'registry.example/robotics@sha256:1234',
                               'platform': 'linux/arm64', 'verifier_timeout_sec': 120}
    assert engdesign.preflight(full)['engdesign_runtime']['ok']
    full['runtime_options'] = {'platform': 'linux/arm64'}
    assert not engdesign.preflight(full)['engdesign_runtime']['ok']
    full['runtime_options'] = {'env': {'OPENAI_API_KEY': 'not-accepted'}}
    assert not engdesign.preflight(full)['engdesign_runtime']['ok']
    full['runtime_options'] = {}
    monkeypatch.setenv('DOCKER_DEFAULT_PLATFORM', 'linux/arm64')
    assert not engdesign.preflight(full)['engdesign_runtime']['ok']


def test_numeric_transport_preserves_nonfinite_detail_without_inventing_score():
    assert engdesign_runtime.plain(complex(1, 2)) == {'real': 1.0, 'imag': 2.0}
    assert engdesign_runtime.plain(float('inf')) == {'native_nonfinite': 'inf'}
    with pytest.raises(ValueError, match='finite numeric'):
        engdesign_runtime.finite_scalar(float('nan'), 'score')
    with pytest.raises(ValueError, match='finite numeric'):
        engdesign_runtime.finite_scalar(None, 'score')


def test_original_candidate_execution_error_keeps_null_native_score(full, local_sources, tmp_path):
    pytest.importorskip('pydantic')
    pytest.importorskip('cv2')
    task, = prepare(full, tmp_path / 'materialized', ('AB_02',))
    answer, output = tmp_path / 'answer.json', tmp_path / 'result.json'
    answer.write_text(json.dumps({'reasoning': 'Invalid generated Python',
                                 'config': {'reasoning': 'Exercise native execution failure',
                                            'solution_code': 'def broken syntax'}}))
    result = grade(task, answer, output)
    verdict = json.loads(output.read_text())
    assert result.returncode == 0, verdict
    assert verdict['status'] == 'graded'
    assert verdict['native_called'] and verdict['native_completed']
    assert verdict['native_return'][0] is False
    assert verdict['native_score'] is None and verdict['confidence'] is None
    assert verdict['rewards'] == {'reward': 0.0}


@pytest.mark.parametrize('task_id', ['XZ_03', 'XZ_04'])
def test_webots_compatibility_changes_only_remote_resource_uris(full, tmp_path, task_id):
    import shutil
    source = FIXTURE / 'EngDesign-Open' / task_id
    shutil.copytree(source, tmp_path / 'source')
    original = (source / 'worlds/construction_site.wbt').read_bytes()
    patch = engdesign_full._local_webots_world(tmp_path / 'source')
    runtime = (tmp_path / 'source/worlds/construction_site.wbt').read_bytes()
    old = b'https://raw.githubusercontent.com/cyberbotics/webots/R2025a/projects/objects/backgrounds/protos/'
    assert runtime == original.replace(old, b'webots://projects/objects/backgrounds/protos/')
    assert patch['original_sha256'] == full['source']['files'][f'EngDesign-Open/{task_id}/worlds/construction_site.wbt']
    assert patch['runtime_sha256'] == hashlib.sha256(runtime).hexdigest()
    assets = full['source']['simulator_assets']
    assert assets['release'] == 'R2025a'
    assert len(assets['files']) == 14


def test_startup_probe_checks_native_imports_without_submission(full, local_sources, tmp_path):
    pytest.importorskip('pydantic')
    task, = prepare(full, tmp_path / 'materialized')
    grader = next((task / 'environment').glob('grader-*'))
    output = tmp_path / 'probe.json'
    proc = subprocess.run([sys.executable, '-I', str(grader / 'runtime.py'), 'probe',
                           str(grader / 'native'), str(tmp_path / 'no-submission.json'),
                           str(output), str(grader / 'config.json')],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(output.read_text())
    assert result['status'] == 'ready'
    assert 'rewards' not in result
