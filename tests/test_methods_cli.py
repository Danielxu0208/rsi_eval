"""Optional command wrappers prepare frozen inputs without owning search state."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from rsi_eval import list_benchmarks
from rsi_eval.cli import main
from rsi_eval import methods


def test_retired_integrations_are_not_advertised(capsys):
    assert all(s["name"] != "gdpval" for s in list_benchmarks())
    assert main(["method", "list"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"meta-harness", "rrsi", "ahe", "harnessx"}


def test_shared_profile_probes_cli_without_model_and_does_not_read_credentials(tmp_path, monkeypatch):
    from rsi_eval import codex
    calls = []
    monkeypatch.setattr(codex, "codex_version", lambda settings: calls.append(settings.model) or "test-version")
    output = tmp_path / "profile.json"
    result = methods.configure(model="explicit-model", auth_dir=tmp_path / "missing-auth", output=output)
    assert calls == ["explicit-model"]
    assert result["model_calls"] == 0
    assert json.loads(output.read_text())["codex"]["expected_version"] == "test-version"
    assert not (tmp_path / "missing-auth").exists()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    env = tmp_path / "environment"
    (env / "bin").mkdir(parents=True)
    (env / "bin/python").symlink_to(sys.executable)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"codex": {"model": "shared-model", "auth_dir": "private-auth",
                                            "expected_version": "fixed-version"}}))
    invocations = []
    def invoke(command, *, cwd, log, env=None):
        invocations.append(command)
        if command[:2] == ["git", "clone"]:
            Path(command[-1]).mkdir()
    monkeypatch.setattr(methods, "_invoke", invoke)
    monkeypatch.setattr(methods, "_git_output", lambda path, *args: "" if args[0] == "status" else methods.METHODS["meta-harness"]["commit"])
    monkeypatch.setattr(methods, "environment_identity", lambda path: {"python": "3.12.15", "packages": {"harbor": "0.18.0"}})
    monkeypatch.setattr(methods, "_has_pip", lambda path: False)
    out = tmp_path / "experiment"
    record = methods.prepare_method(method="meta-harness", experiment=out, profile_file=profile,
                                    environment=env, install=False)
    return record, invocations, profile


def test_preparation_snapshots_installed_resources_and_generates_portable_configuration(prepared):
    record, calls, profile = prepared
    root = Path(record["experiment"])
    config = json.loads((root / "configuration.json").read_text())
    assert config["codex"]["model"] == "shared-model"
    assert config["codex"]["auth_dir"] == str(profile.parent / "private-auth")
    assert config["seed"] == str(root / "seed")
    assert config["run_options"]["harbor_executable"] == str(Path(record["environment"]) / "bin/harbor")
    assert (root / "runtime/integrations/meta_harness/launcher.py").is_file()
    assert (root / "runtime/rsi_eval_templates/codex_agent/agent.py").is_file()
    assert not (root / "runtime/integrations/tthe").exists()
    assert not list((root / "runtime").rglob("auth.json"))
    assert record["model_calls"] == 0
    assert not any("pip" in command for command in calls)
    again = methods.prepare_method(method="meta-harness", experiment=root, profile_file=profile,
                                    environment=Path(record["environment"]), install=False)
    assert again == record


def test_dispatch_selects_each_native_resume_without_a_method_base_class(prepared):
    record, _, _ = prepared
    for name in methods.METHODS:
        row = {**record, "method": name}
        command = methods.method_command(row, "resume")
        if name == "rrsi":
            assert "resume" in command and "integrations.rrsi" in command
        else:
            assert "--resume" in command
        assert command[0] == str(Path(record["environment"]) / "bin/python")


def test_runtime_and_configuration_tampering_prevents_launch(prepared, monkeypatch):
    record, _, _ = prepared
    root = Path(record["experiment"])
    launches = []
    monkeypatch.setattr(methods.subprocess, "run", lambda *a, **kw: launches.append(a))
    config = json.loads((root / "configuration.json").read_text())
    config["codex"]["model"] = "changed-model"
    (root / "configuration.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="configuration changed"):
        methods.launch_method(root, "run")
    assert launches == []


def test_preparation_cannot_overwrite_an_unrelated_directory(tmp_path):
    (tmp_path / "my-file").write_text("keep")
    with pytest.raises(FileExistsError):
        methods.prepare_method(method="rrsi", experiment=tmp_path, install=False)
    assert (tmp_path / "my-file").read_text() == "keep"


@pytest.fixture
def setup_probe(tmp_path, monkeypatch):
    """Deterministic setup boundaries, with real runtime/template snapshots."""
    env = tmp_path / 'venv'
    (env / 'bin').mkdir(parents=True)
    (env / 'bin/python').symlink_to(sys.executable)
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'codex': {'model': 'fixture', 'auth_dir': str(tmp_path / 'auth'),
                                           'expected_version': 'fixture-version'}}))
    calls = []
    failures = {'stage': None, 'raised': False}
    pin = methods.METHODS['meta-harness']['commit']

    def invoke(command, *, cwd, log, env=None):
        calls.append(command)
        if command[:2] == ['git', 'clone']:
            directory = Path(command[-1])
            directory.mkdir()
            if failures['stage'] == 'clone' and not failures['raised']:
                (directory / 'partial-pack').write_text('retain this partial clone')
                failures['raised'] = True
                raise KeyboardInterrupt('clone interrupted')
            (directory / 'HEAD.fixture').write_text('unrelated-main')
        elif command[0] == 'git' and 'checkout' in command:
            if failures['stage'] == 'detach' and not failures['raised']:
                failures['raised'] = True
                raise KeyboardInterrupt('interrupted before detach')
            (Path(command[2]) / 'HEAD.fixture').write_text(command[-1])
        elif 'integrations.ahe' in command and 'prepare' in command:
            directory = Path(command[command.index('--checkout') + 1])
            directory.mkdir()
            if failures['stage'] == 'checkout' and not failures['raised']:
                (directory / 'partial-export.py').write_text('preserve the failed export')
                failures['raised'] = True
                raise KeyboardInterrupt('AHE export interrupted')
            (directory / 'prepared.json').write_text('{}')

    def git(directory, *args):
        if not (directory / 'HEAD.fixture').exists():
            return None
        if args[0] == 'status':
            return ''
        if args[-1].endswith('^{commit}'):
            return args[-1].removesuffix('^{commit}')
        return (directory / 'HEAD.fixture').read_text()

    monkeypatch.setattr(methods, '_invoke', invoke)
    monkeypatch.setattr(methods, '_git_output', git)
    monkeypatch.setattr(methods, 'environment_identity', lambda path: {'python': '3.12.15', 'packages': {'harbor': '0.18.0'}})
    monkeypatch.setattr(methods, '_has_pip', lambda path: False)
    def record_lock(python, *, method, destination, requirements):
        value = {'sha256': 'fixture-lock-sha'}
        destination.write_text(json.dumps(value))
        return value
    monkeypatch.setattr('rsi_eval.dependency_locks.create_lock', record_lock)
    arguments = {'method': 'meta-harness', 'experiment': tmp_path / 'experiment',
                 'profile_file': profile, 'environment': env, 'install': False}
    return arguments, calls, failures


@pytest.mark.parametrize('stage', ['clone', 'detach'])
def test_prepare_recovers_interrupted_clone_and_detaches_pinned_revision(setup_probe, stage):
    args, calls, failures = setup_probe
    failures['stage'] = stage
    with pytest.raises(KeyboardInterrupt):
        methods.prepare_method(**args)
    record = methods.prepare_method(**args)
    assert record['status'] == 'prepared'
    root = args['experiment']
    assert (root / 'upstream-source/HEAD.fixture').read_text() == methods.METHODS['meta-harness']['commit']
    clones = [call for call in calls if call[:2] == ['git', 'clone']]
    assert len(clones) == (2 if stage == 'clone' else 1)
    if stage == 'clone':
        archived = list((root / 'prepare-attempts').glob('*/partial-pack'))
        assert len(archived) == 1 and archived[0].read_text() == 'retain this partial clone'
    else:
        assert len([call for call in calls if call[0] == 'git' and 'checkout' in call]) == 2


def test_prepare_recovers_partial_runtime_snapshot_without_losing_bytes(setup_probe, monkeypatch):
    args, _, _ = setup_probe
    original = methods._runtime_copy
    def interrupted(destination):
        destination.mkdir()
        (destination / 'partial-module.py').write_text('retain unfinished runtime')
        raise KeyboardInterrupt('snapshot interrupted')
    monkeypatch.setattr(methods, '_runtime_copy', interrupted)
    with pytest.raises(KeyboardInterrupt):
        methods.prepare_method(**args)
    monkeypatch.setattr(methods, '_runtime_copy', original)
    record = methods.prepare_method(**args)
    assert record['status'] == 'prepared'
    archived = list((args['experiment'] / 'prepare-attempts').glob('*/partial-module.py'))
    assert len(archived) == 1 and archived[0].read_text() == 'retain unfinished runtime'


def test_prepare_recovers_known_partial_native_checkout(setup_probe):
    args, _, failures = setup_probe
    args['method'] = 'ahe'
    failures['stage'] = 'checkout'
    with pytest.raises(KeyboardInterrupt):
        methods.prepare_method(**args)
    record = methods.prepare_method(**args)
    assert record['status'] == 'prepared'
    archived = list((args['experiment'] / 'prepare-attempts').glob('*/partial-export.py'))
    assert len(archived) == 1 and archived[0].read_text() == 'preserve the failed export'


def test_prepared_upstream_pin_is_checked_without_resetting_user_changes(setup_probe):
    args, calls, _ = setup_probe
    methods.prepare_method(**args)
    head = args['experiment'] / 'upstream-source/HEAD.fixture'
    head.write_text('changed-after-preparation')
    before = len(calls)
    with pytest.raises(ValueError, match='source pin changed'):
        methods.prepare_method(**args)
    assert len(calls) == before
    assert head.read_text() == 'changed-after-preparation'


def test_recovery_never_archives_an_unrecorded_directory(setup_probe):
    args, _, failures = setup_probe
    failures['stage'] = 'clone'
    with pytest.raises(KeyboardInterrupt):
        methods.prepare_method(**args)
    marker = args['experiment'] / 'method.json'
    record = json.loads(marker.read_text())
    record['managed_directories'].pop('upstream-source')
    marker.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='Cannot recreate unrecorded'):
        methods.prepare_method(**args)
    assert (args['experiment'] / 'upstream-source/partial-pack').exists()


def _check_fixture(tmp_path, monkeypatch, method, config):
    from rsi_eval import method_check
    from types import SimpleNamespace
    root = tmp_path / method
    root.mkdir()
    record = {'method': method, 'environment': str(tmp_path / 'env')}
    (root / 'method.json').write_text(json.dumps(record))
    (root / 'configuration.json').write_text(json.dumps(config))
    monkeypatch.setattr(method_check, 'validate_auth', lambda directory: None)
    monkeypatch.setattr(method_check, 'codex_version', lambda settings: settings.expected_version)
    monkeypatch.setattr(method_check.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=methods.METHODS[method]['commit']))
    calls = []
    monkeypatch.setattr(method_check, 'check', lambda **kwargs: calls.append(kwargs) or {'ok': True})
    return root, calls


def _check_config(tmp_path):
    return {'codex': {'model': 'fixture', 'auth_dir': str(tmp_path / 'auth'), 'expected_version': 'fixture-cli',
                       'executable': str(tmp_path / 'custom-codex')},
            'evolve': {'name': 'terminal-bench', 'version': '2.0', 'task_ids': ['extract-elf', 'log-summary-date-ranges']},
            'test': {'name': 'terminal-bench', 'version': '2.1', 'task_ids': ['extract-elf']}}


def test_meta_check_uses_native_budgets_legacy_options_and_implicit_smoke(tmp_path, monkeypatch):
    from rsi_eval.method_check import inspect_experiment
    config = _check_config(tmp_path)
    config.update(environment='daytona', evaluation_timeout=73)
    config['run_options'] = {'repetitions': 9, 'concurrency': 9}
    root, calls = _check_fixture(tmp_path, monkeypatch, 'meta-harness', config)
    report = inspect_experiment(root)
    assert report['ok'] and report['model_calls'] == 0
    assert [row['role'] for row in report['checks']] == ['evolve', 'smoke', 'test']
    assert calls[1]['benchmark'].task_ids == ['extract-elf']
    for call in calls:
        assert call['options'].repetitions == call['options'].concurrency == 1
        assert call['options'].environment == 'daytona' and call['options'].timeout == 73
        assert call['codex_executable'] == str(tmp_path / 'custom-codex')


def test_meta_check_skips_disabled_smoke_and_requires_explicit_full_set_smoke(tmp_path, monkeypatch):
    from rsi_eval.method_check import inspect_experiment
    config = _check_config(tmp_path)
    config['evolve'].pop('task_ids')
    root, calls = _check_fixture(tmp_path, monkeypatch, 'meta-harness', config)
    with pytest.raises(ValueError, match='Specify smoke selection'):
        inspect_experiment(root)
    config['skip_smoke'] = True
    (root / 'configuration.json').write_text(json.dumps(config))
    report = inspect_experiment(root)
    assert [row['role'] for row in report['checks']] == ['evolve', 'test']


def test_rrsi_check_uses_native_k_smoke_budget_and_policy_target(tmp_path, monkeypatch):
    from rsi_eval.method_check import inspect_experiment
    from integrations.rrsi import __main__ as rrsi_cli
    config = _check_config(tmp_path)
    config['smoke'] = deepcopy(config['evolve'])
    config['rrsi'] = {'k': 3}
    config['run_options'] = {'repetitions': 9, 'concurrency': 2}
    config['policy_model'] = 'distinct-policy-model'
    config['target_config'] = {'reasoning_effort': 'high'}
    monkeypatch.setattr(rrsi_cli, 'checked_config', lambda checkout: config)
    root, calls = _check_fixture(tmp_path, monkeypatch, 'rrsi', config)
    assert inspect_experiment(root)['ok']
    assert [call['options'].repetitions for call in calls] == [3, 1, 3]
    assert [call['options'].concurrency for call in calls] == [2, 1, 2]
    assert all(call['target'].model == 'distinct-policy-model' for call in calls)
    assert all(call['target'].config['reasoning_effort'] == 'high' for call in calls)


def test_external_environment_without_pip_gets_actionable_error(setup_probe, monkeypatch):
    args, calls, _ = setup_probe
    args['install'] = True
    monkeypatch.setattr(methods, '_has_pip', lambda python: False)
    with pytest.raises(ValueError, match='ensurepip --upgrade'):
        methods.prepare_method(**args)
    assert not any('venv' in command for command in calls)
    assert not any('pip' in command for command in calls)


def test_interrupted_owned_venv_bootstrap_is_repaired_before_install(setup_probe, monkeypatch):
    args, calls, _ = setup_probe
    args['environment'] = None
    args['install'] = True
    invoke = methods._invoke
    state = {'venv_calls': 0, 'pip': False}
    def interrupted(command, **kwargs):
        if command[1:3] == ['-m', 'venv']:
            calls.append(command)
            directory = Path(command[-1])
            (directory / 'bin').mkdir(parents=True, exist_ok=True)
            python = directory / 'bin/python'
            if not python.exists():
                python.symlink_to(sys.executable)
            state['venv_calls'] += 1
            if state['venv_calls'] == 1:
                raise KeyboardInterrupt('venv stopped before pip installation')
            state['pip'] = True
        else:
            invoke(command, **kwargs)
    monkeypatch.setattr(methods, '_invoke', interrupted)
    monkeypatch.setattr(methods, '_has_pip', lambda python: state['pip'])
    with pytest.raises(KeyboardInterrupt):
        methods.prepare_method(**args)
    record = methods.prepare_method(**args)
    assert record['status'] == 'prepared'
    assert state['venv_calls'] == 2
    assert any(command[1:4] == ['-m', 'pip', 'install'] for command in calls)
