"""Whole-matrix routing checks; no models, Docker or network are required."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tomllib

import pytest

from rsi_eval.materializers import frontier_eng_full as full
from rsi_eval.materializers import frontier_eng_full_runtime as runtime
from rsi_eval.materializers import frontier_eng_runtime as broker_runtime

ROOT = Path(__file__).resolve().parents[1]


def test_native_transport_retains_setup_failure_artifacts():
    metrics = {'combined_score': -1e18, 'valid': 0.0}
    artifacts = {'error_message': 'runtime executable not found: missing', 'stderr': 'details'}
    result = runtime.preserve_native_result(metrics, artifacts)
    assert result['metrics'] is metrics
    assert result['artifacts'] is artifacts


def test_singlecell_truth_is_separate_from_native_candidate_inputs(tmp_path, catalog):
    assets = runtime.singlecell_assets(tmp_path)
    assert assets['test_mod2.h5ad'].parent != assets['test_mod1.h5ad'].parent
    assert assets['test_mod2.h5ad'].parent.name == 'openproblems_neurips2021__bmmc_cite__normal__log_cp10k'
    task = next(t for t in catalog['tasks'] if t['id'] == 'SingleCellAnalysis/predict_modality')
    assert set(task['runtime_recipe']['required_assets']) == {p.relative_to(tmp_path).as_posix() for p in assets.values()}


def test_engdesign_runner_gets_readonly_source_helper_alias(tmp_path):
    source = tmp_path/'source'; shared = source/'benchmarks/_shared'; shared.mkdir(parents=True)
    (shared/'candidate_sandbox.py').write_text('# frozen source')
    work = tmp_path/'native-work'; work.mkdir()
    renderer = runtime.engdesign_renderer(lambda **kwargs: 'original command', source)
    assert renderer(sandbox_root=work) == 'original command'
    alias = work/'benchmarks/_shared'
    assert alias.is_symlink()
    assert alias.resolve() == shared


@pytest.mark.parametrize('artifacts', [
    {'error_message': 'failed to load model: checkpoint missing'},
    {'error_message': 'scorer reference self-test failed, refusing to score: incorrect digest'},
    {'error_message': 'scorer produced no numeric combined_score'},
    {'user_artifact::failure_reason': "bwrap: Can't mount proc on /newroot/proc: Operation not permitted"},
    {'user_artifact::task_results': {'AM_02': {'error': 'ModuleNotFoundError: control'}}},
    {'collected_artifact::verification/outputs/summary.json': '{"cases": [{"candidate_error": "bwrap: Can\'t find source path /sys: No such file"}]}'},
])
def test_native_environment_failures_do_not_become_graded_zero(artifacts):
    metrics = {'combined_score': 0, 'valid': 0}
    assert runtime.native_infrastructure_error(metrics, artifacts, [metrics])


def test_missing_grader_metrics_is_infra_but_native_timeout_remains_invalid():
    assert runtime.native_infrastructure_error({'combined_score': 1, 'valid': 1}, {}, [])
    assert runtime.native_infrastructure_error({'combined_score': -1e18, 'valid': 0, 'timeout': 1}, {}, []) is None
    assert runtime.native_infrastructure_error({'combined_score': 1, 'valid': 1}, {}, [{'combined_score': None, 'valid': 'bad'}])


def test_pinned_external_assets_reject_changed_bytes(tmp_path, monkeypatch):
    contents = b'original stiffness matrix'
    expected = hashlib.sha256(contents).hexdigest()
    monkeypatch.setattr(runtime.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(contents))
    asset = {'destination':'benchmarks/EngDesign/example.csv', 'url':'https://example.test/pinned.csv', 'sha256':expected}
    runtime.fetch_pinned_assets(tmp_path, [asset])
    path = tmp_path/asset['destination']
    assert path.read_bytes() == contents
    path.write_text('changed')
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        runtime.fetch_pinned_assets(tmp_path, [asset])


def test_native_invalid_design_auxiliary_infinity_remains_a_graded_result():
    metrics = {'combined_score': 0.0, 'valid': 0.0, 'compliance': float('inf')}
    assert runtime.native_infrastructure_error(metrics, {}, [metrics]) is None
    result = runtime.json_safe_native({'metrics': metrics, 'artifacts': {'metrics_json': '{"compliance": Infinity}'}})
    assert result['metrics'] == {'combined_score': 0.0, 'valid': 0.0, 'compliance': None}
    assert result['native_nonfinite_fields']['/metrics/compliance'] == 'inf'
    assert 'Infinity' in result['artifacts']['metrics_json']
    json.dumps(result, allow_nan=False)


def test_auxiliary_null_stays_in_raw_result_but_not_harbor_rewards(tmp_path, monkeypatch):
    result = {'best_metrics': {'combined_score': 0, 'valid': 0, 'compliance': None},
              'candidate_calls': 1, 'feedback_calls': 0, 'candidate_budget': 100,
              'native_nonfinite_fields': {'/metrics/compliance': 'inf'}}
    monkeypatch.setattr(broker_runtime, 'request', lambda *args, **kwargs: result)
    candidate = tmp_path/'candidate.py'; candidate.write_text('pass')
    config = tmp_path/'config.json'; config.write_text('{"verifier_token":"test-only"}')
    output = tmp_path/'output'
    assert broker_runtime.final_verify(config, candidate, output) == 0
    assert json.loads((output/'frontier.json').read_text()) == result
    rewards = json.loads((output/'reward.json').read_text())
    assert 'compliance' not in rewards
    assert rewards['valid'] == rewards['reward'] == 0


@pytest.fixture
def catalog():
    return next(s for s in json.loads((ROOT/'src/rsi_eval/data/catalog_frontier_eng.json').read_text()) if s['version'] == full.VERSION)


def test_full_catalog_routes_every_native_matrix_entry(catalog):
    tasks = catalog['tasks']
    assert len(tasks) == 48
    assert sum(t['collection'] == 'v1' for t in tasks) == 47
    assert [t['id'] for t in tasks if t['collection'] != 'v1'] == ['EngDesign']
    assert catalog['coverage']['unsupported_task_ids'] == []
    assert len(catalog['source']['files']) == 2665
    for task in tasks:
        base = 'benchmarks/' + task['id'] + '/'
        assert base + task['native_metadata']['initial_program'] in catalog['source']['files']
        assert base + 'frontier_eval/eval_command.txt' in catalog['source']['files']
        assert task['native_task_config']['benchmark'] == task['id']
        assert task['native_evaluator_timeout_sec'] in (300, 600, 1200)
        assert task['objective'] == {'metric':'combined_score', 'direction':'maximize'}
        assert task['runtime_recipe']['packages']
        for req in task['runtime_recipe']['requirements']:
            assert req in catalog['source']['files']
    assert catalog['protocol']['candidate_budget'] == 100
    assert 'optional explicit override' in catalog['protocol']['agent_wall_time_sec'].lower()


def test_preflight_marks_hardware_per_task_without_removing_ids(catalog, monkeypatch):
    monkeypatch.delenv('DOCKER_DEFAULT_PLATFORM', raising=False)
    catalog['runtime_options'] = {'platform':'linux/arm64'}
    result = full.preflight(catalog)
    assert not result['ComputerSystems/MallocLab:native_platform']['ok']
    assert not result['KernelEngineering/MLA:gpu_configuration']['ok']
    assert result['EnergyStorage/BatteryFastChargingProfile:native_mapping']['ok']
    assert len(catalog['tasks']) == 48
    catalog['runtime_options'] = {'platform':'linux/amd64','gpu':True}
    assert all(value['ok'] for value in full.preflight(catalog).values())


def test_per_task_runtime_settings_can_resolve_mixed_hardware(catalog, monkeypatch):
    monkeypatch.delenv('DOCKER_DEFAULT_PLATFORM', raising=False)
    catalog['tasks'] = [t for t in catalog['tasks'] if t['id'] in ('Robotics/PIDTuning', 'KernelEngineering/TriMul')]
    catalog['runtime_options'] = {'platform':'linux/arm64','task_overrides':{'KernelEngineering/TriMul':{'platform':'linux/amd64','gpu':True}}}
    assert all(row['ok'] for row in full.preflight(catalog).values())


@pytest.mark.parametrize('options', [{'wat':1}, {'gpu':'yes'}, {'agent_timeout_sec':0}, {'candidate_max_bytes':0}, {'candidate_max_bytes':True},
    {'env':{'OPENAI_API_KEY':'never-copy'}}, {'runtime_image':'python:3.12\nRUN bad'}, {'task_overrides':[]}])
def test_invalid_runtime_configuration_fails(options, catalog):
    catalog['runtime_options'] = options
    with pytest.raises(ValueError):
        full.runtime_options(catalog, catalog['tasks'][0])


def test_asset_snapshot_cannot_replace_grader_or_contain_symlink(tmp_path):
    source = tmp_path/'assets'; target = tmp_path/'staged'; source.mkdir()
    path = source/'benchmarks/example/evaluator.py'; path.parent.mkdir(parents=True); path.write_text('changed')
    with pytest.raises(ValueError, match='cannot replace frozen'):
        full._asset_copy(source,target,{'benchmarks/example/evaluator.py':hashlib.sha256(b'original').hexdigest()})
    path.unlink(); path.symlink_to('/etc/hosts')
    with pytest.raises(ValueError, match='not symlinks'):
        full._asset_copy(source,target,{})


def test_source_archive_rejects_path_escape_before_extracting(tmp_path):
    archive = tmp_path/'source.tar'
    with tarfile.open(archive,'w') as tar:
        member = tarfile.TarInfo('Frontier-Engineering-'+full.SOURCE_COMMIT+'/../escape')
        member.size = 1; tar.addfile(member,io.BytesIO(b'x'))
    with pytest.raises(ValueError, match='unsafe'):
        full.extract_source(archive,tmp_path/'out',{})
    assert not (tmp_path/'escape').exists()


def synthetic_source(catalog, root):
    # Small synthetic source content exercises the entire materialization path;
    # the catalog binds the real source separately by archive and file hashes.
    for task in catalog['tasks']:
        base = root/'benchmarks'/task['id']
        for key in ('agent_files','copy_files','readonly_files','artifact_files'):
            p=base/'frontier_eval'/(key+'.txt');p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text('\n'.join(task['native_metadata'][key])+'\n')
        for key, value in [('initial_program',task['native_metadata']['initial_program']),('eval_command',task['native_metadata']['eval_command'])]:
            (base/'frontier_eval'/(key+'.txt')).write_text(value+'\n')
        for name in [*task['native_metadata']['agent_files'],task['native_metadata']['initial_program']]:
            path=base/name
            if not path.exists():
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('# synthetic source for '+task['id']+'\n')
    for name in ('frontier_eval/tasks/unified/evaluator/python.py','benchmarks/_shared/candidate_sandbox.py','scripts/bootstrap/fetch_task_assets.py'):
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# synthetic fixture\n')


def test_all_48_entries_generate_executable_native_task_packages(catalog,tmp_path,monkeypatch):
    monkeypatch.delenv('DOCKER_DEFAULT_PLATFORM',raising=False)
    source=tmp_path/'source';synthetic_source(catalog,source)
    catalog['runtime_options']={'platform':'linux/amd64','gpu':True}
    for index, task in enumerate(catalog['tasks']):
        full.validate_task(task,source)
        target=tmp_path/'tasks'/str(index);target.mkdir(parents=True)
        full._task(catalog,task,source,target)
        config=tomllib.loads((target/'task.toml').read_text())
        assert config['metadata']['record_id']==task['id']
        assert config['agent']['timeout_sec']==604800
        grader,= (target/'environment').glob('grader-*')
        agent,= (target/'environment').glob('agent-*')
        broker=json.loads((grader/'broker.json').read_text())
        assert broker['engine']=='native-unified'
        assert broker['task']['native_metadata']['eval_command']==task['native_metadata']['eval_command']
        assert broker['candidate_budget']==100
        assert broker['candidate_max_bytes']==4*1024*1024
        assert (agent/'public'/task['native_metadata']['initial_program']).is_file()
        assert broker['verifier_token'] not in ''.join(p.read_text() for p in agent.rglob('*') if p.is_file())
        assert 'frontier_full_runtime.py' in (grader/'Dockerfile').read_text()
        assert (grader/'Dockerfile').read_text().rfind('COPY broker.json') > (grader/'Dockerfile').read_text().rfind('RUN ')
        assert '--cap-drop ALL' in (grader/'bwrap').read_text()
        assert 'read_only: true' in (target/'environment/docker-compose.yaml').read_text()
        if task['runtime_recipe']['gpu']:
            assert 'gpus: all' in (target/'environment/docker-compose.yaml').read_text()


def test_optics_arm_recipe_keeps_original_requirements_and_builds_cpu_dependencies(catalog, tmp_path, monkeypatch):
    monkeypatch.delenv('DOCKER_DEFAULT_PLATFORM', raising=False)
    task = next(t for t in catalog['tasks'] if t['id'].startswith('Optics/'))
    catalog['tasks'] = [task]
    catalog['runtime_options'] = {'platform':'linux/arm64', 'candidate_max_bytes':8*1024*1024}
    source = tmp_path/'source'; synthetic_source(catalog, source)
    target = tmp_path/'task'; target.mkdir()
    full._task(catalog, task, source, target)
    grader, = (target/'environment').glob('grader-*')
    dockerfile = (grader/'Dockerfile').read_text()
    assert 'qtbase5-dev' in dockerfile
    assert '--confirm-license=' in dockerfile
    assert 'https://download.pytorch.org/whl/cpu' in dockerfile
    assert all(path in dockerfile for path in task['runtime_recipe']['requirements'])
    assert json.loads((grader/'broker.json').read_text())['candidate_max_bytes'] == 8*1024*1024


def test_real_pinned_checkout_matches_every_native_metadata_and_source(catalog):
    root=ROOT/'.runtime/frontier-upstream'
    if not root.is_dir():
        pytest.skip('optional source audit requires the pinned untracked upstream checkout')
    for task in catalog['tasks']:
        full.validate_task(task,root)
    for name, digest in catalog['source']['files'].items():
        assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest
