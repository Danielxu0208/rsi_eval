"""Run every released Frontier task through its unmodified unified runner."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def fetch_pinned_assets(root, assets):
    for asset in assets:
        destination = Path(root) / asset['destination']
        if not destination.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(asset['url'], timeout=120) as response, destination.open('wb') as output:
                shutil.copyfileobj(response, output, 1024 * 1024)
        if digest(destination) != asset['sha256']:
            raise RuntimeError('pinned external asset checksum mismatch: ' + asset['destination'])


def singlecell_assets(root):
    base = Path(root) / 'benchmarks/SingleCellAnalysis/predict_modality'
    dataset = 'openproblems_neurips2021__bmmc_cite__normal__log_cp10k'
    return {name: base / ('resources_truth' if name == 'test_mod2.h5ad' else 'resources_cache') / dataset / name
            for name in ('train_mod1.h5ad', 'train_mod2.h5ad', 'test_mod1.h5ad', 'test_mod2.h5ad')}


def engdesign_renderer(renderer, root):
    """Expose the original read-only helper in the native temporary layout."""
    def render(**kwargs):
        # EngDesign locates _shared by walking ancestors of __file__, unlike
        # other tasks that honor FRONTIER_ENGINEERING_ROOT. Its unified copy
        # has no repository ancestor unless this read-only alias is supplied.
        parent = Path(kwargs['sandbox_root']) / 'benchmarks'
        parent.mkdir(exist_ok=True)
        (parent / '_shared').symlink_to(Path(root) / 'benchmarks/_shared', target_is_directory=True)
        return renderer(**kwargs)
    return render


def drop_candidate_capabilities():
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
    header = Header(0x20080522, 0)
    if libc.prctl(38, 1, 0, 0, 0) or libc.capset(ctypes.byref(header), (Data * 2)()):
        raise OSError(ctypes.get_errno(), 'cannot drop direct candidate capabilities')


def pymoto_renderer(renderer):
    def render(**kwargs):
        original = renderer(**kwargs)
        python = shlex.quote(kwargs['runtime_python_path'])
        if not original.startswith(python + ' '):
            raise RuntimeError('unexpected native PyMOTO command prefix')
        launcher = [kwargs['runtime_python_path'], '-I', str(Path(__file__).resolve()),
                    'pymoto', str(kwargs['candidate_dst'])]
        return shlex.join(launcher) + ' ' + original[len(python) + 1:]
    return render


def pymoto_main(candidate, script, arguments):
    original_run = subprocess.run
    candidate = Path(candidate).resolve()
    def run(command, *args, **kwargs):
        if isinstance(command, (tuple, list)) and len(command) >= 2 and command[0] == sys.executable and Path(command[1]).resolve() == candidate:
            kwargs['preexec_fn'] = drop_candidate_capabilities
        return original_run(command, *args, **kwargs)
    subprocess.run = run
    sys.argv = [script, *arguments]
    runpy.run_path(script, run_name='__main__')


def prepare_outer_namespace():
    """Allow native nested proc mounts inside the private outer namespace."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    # bubblewrap adds these mounts to its fresh /proc. They make the kernel
    # reject proc in a child user namespace as a potentially revealing mount.
    # This is the outer namespace only; native candidate namespaces retain
    # their original masks. No host/container mount is modified.
    for name in ('/proc/sysrq-trigger', '/proc/irq', '/proc/bus'):
        if libc.umount2(name.encode(), 0) != 0 and ctypes.get_errno() not in (2, 22):
            raise OSError(ctypes.get_errno(), 'cannot prepare nested namespace: ' + name)
    # The trusted outer driver retains mount and user-map capabilities in its own user namespace:
    # removing it prevents the native nested bubblewrap from configuring its
    # loopback device on supported runtimes. The grader's parent container root
    # is read-only, and broker state/PIDs are absent here. Native candidate
    # namespaces retain their original capability dropping and proc masks.


def temporary_runtime_environment(directory, environment=None):
    home = Path(directory) / 'runtime-home'
    home.mkdir(exist_ok=True)
    os.environ['HOME'] = str(home)
    os.environ['MPLCONFIGDIR'] = str(home / 'matplotlib')
    os.environ.update(environment or {})


def namespace_probe(root, config_path):
    import ctypes
    config = json.loads(Path(config_path).read_text())
    temporary_runtime_environment(Path(config_path).parent, config.get('environment'))
    for name in config['imports']:
        __import__(name)
    if config.get('gpu'):
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError('native task requires an available NVIDIA CUDA device')
    if Path('/state').exists() or Path('/opt/frontier/broker.json').exists():
        raise RuntimeError('broker state is visible in the evaluator namespace')
    frozen = Path(root) / 'benchmarks/_shared/candidate_sandbox.py'
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.mount(None, str(root).encode(), None, 4096 | 32, None) == 0:
        raise RuntimeError('frozen source can be remounted writable')
    try:
        descriptor = os.open(frozen, os.O_WRONLY | os.O_APPEND)
    except OSError:
        pass
    else:
        os.close(descriptor)
        raise RuntimeError('frozen source is writable in the evaluator namespace')
    sandbox = module('frontier_probe_sandbox', Path(root) / 'benchmarks/_shared/candidate_sandbox.py')
    with tempfile.TemporaryDirectory(prefix='frontier-probe-') as directory:
        candidate = Path(directory) / 'candidate.py'
        candidate.write_text("from pathlib import Path\nassert not Path('/state').exists()\nassert not Path('/opt/frontier/broker.json').exists()\nstatus=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)\nassert int(status['CapEff'],16)==0\nprint('frontier-native-namespace-ok')\n")
        result = sandbox.run_candidate_isolated(candidate, timeout_s=15, expected_outputs=(), readonly_paths=())
        if not result.ok or 'frontier-native-namespace-ok' not in result.stdout_tail:
            raise RuntimeError('nested native namespace preflight failed: ' + result.stderr_tail)
    print('frontier-native-namespace-ok')


def json_safe_native(value):
    """Keep native invalid-result auxiliary infinity without forging a score."""
    nonfinite = {}
    def convert(item, path):
        if isinstance(item, float) and not math.isfinite(item):
            nonfinite[path] = repr(item)
            return None
        if isinstance(item, dict):
            return {str(k): convert(v, path + '/' + str(k)) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(v, path + '/' + str(i)) for i, v in enumerate(item)]
        return item
    result = convert(value, '')
    if nonfinite:
        result['native_nonfinite_fields'] = nonfinite
    return result


def bootstrap(config_path, phase):
    """Install declared official assets at image build time, outside model runs."""
    config = json.loads(Path(config_path).read_text())
    root = Path(config['source_root'])
    recipe = config['task']['runtime_recipe']
    actions = recipe['bootstrap']
    os.chdir(root)
    # Upstream bootstrap resolves these documented interpreter names.
    for name in ('frontier-eval-driver', 'frontier-v1-main', 'frontier-v1-kernel', 'frontier-v1-summit', 'frontier-v1-sustaindc'):
        link = root / '.venvs' / name / 'bin/python'
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(sys.executable)
    if phase == 'before' and 'sustaindc' in actions:
        subprocess.run([sys.executable, 'scripts/bootstrap/fetch_task_assets.py', '--target', 'sustaindc'], check=True)
    if phase != 'after':
        return
    fetch_pinned_assets(root, recipe.get('pinned_assets', []))
    for action in actions:
        if action == 'predict_modality_data':
            dataset = 'openproblems_neurips2021/bmmc_cite/normal/log_cp10k/'
            base = 'https://openproblems-data.s3.amazonaws.com/resources/task_predict_modality/datasets/'
            for name, output in singlecell_assets(root).items():
                output.parent.mkdir(parents=True, exist_ok=True)
                if not output.is_file():
                    with urllib.request.urlopen(base + dataset + name, timeout=120) as response, output.open('wb') as stream:
                        shutil.copyfileobj(response, stream, 1024 * 1024)
        elif action == 'car_aerodynamics_sensing':
            subprocess.run([sys.executable, 'scripts/bootstrap/fetch_task_assets.py', '--target', action], check=True)
        elif action == 'malloc_wasm':
            subprocess.run([sys.executable, 'benchmarks/_shared/malloc_wasm/setup.py', '--install', '--root', '/opt/frontier/toolchain'], check=True)
    if config['task_id'].split('/')[0] == 'StructuralOptimization':
        # The original scorer uses its source-relative temp directory. The
        # runtime binds an empty per-evaluation directory here, never source.
        (root / 'benchmarks/StructuralOptimization/PyMOTOSIMPCompliance/temp').mkdir(exist_ok=True)
    for relative in recipe['required_assets']:
        if not (root / relative).exists():
            raise RuntimeError('required native asset missing after bootstrap: ' + relative)
    assets = {}
    for relative in recipe['required_assets']:
        path = root / relative
        for item in ([path] if path.is_file() else sorted(path.rglob('*'))):
            if item.is_file() and not item.is_symlink() and '.git' not in item.parts:
                assets[str(item.relative_to(root))] = digest(item)
    record = {'runtime_family': config['task_id'].split('/')[0], 'source_commit': config['source_commit'],
              'runtime_recipe': recipe, 'asset_sha256': assets,
              'python_version': sys.version, 'python_executable': sys.executable,
              'packages': subprocess.run([sys.executable, '-m', 'pip', 'freeze'], capture_output=True, text=True, check=True).stdout.splitlines()}
    Path('/opt/frontier/build-manifest.json').write_text(json.dumps(record, indent=2) + '\n')


def native_evaluate(config_path, candidate, output):
    config = json.loads(Path(config_path).read_text())
    root = Path(config['source_root'])
    temporary_runtime_environment(Path(config_path).parent, config.get('environment'))
    sys.path.insert(0, str(root))
    from frontier_eval.tasks.unified.spec import load_unified_task_spec
    from frontier_eval.tasks.unified.evaluator import python as evaluator
    # Upstream's optional OpenEvolve transport drops artifacts when that search
    # package is absent. Preserve the exact numerical metrics and artifacts
    # without installing its optimizer, model clients or changing grader code.
    evaluator._wrap = preserve_native_result
    if config['task_id'] == 'EngDesign':
        evaluator._render_eval_command = engdesign_renderer(evaluator._render_eval_command, root)
    elif config['task_id'] == 'StructuralOptimization/PyMOTOSIMPCompliance':
        evaluator._render_eval_command = pymoto_renderer(evaluator._render_eval_command)
    task_config = dict(config['task']['native_task_config'])
    task_config['runtime'] = {'env_name': 'rsi-per-task-runtime', 'python_path': sys.executable,
                              'shell': 'bash', 'env': dict(config.get('environment', {}))}
    if config['task_id'] == 'EngDesign':
        task_config['runtime']['env']['ENGDESIGN_EVAL_MODE'] = 'local'
    task_config['runtime']['env']['FRONTIER_MALLOC_TOOLCHAIN'] = '/opt/frontier/toolchain'
    os.environ['FRONTIER_ENGINEERING_ROOT'] = str(root)
    os.environ['FRONTIER_EVAL_EVALUATOR_TIMEOUT_S'] = str(config['task']['native_evaluator_timeout_sec'])
    spec = load_unified_task_spec(task_cfg=task_config, repo_root=root)
    original_read = evaluator._read_json
    metric_payloads = []
    def read_json(path):
        value = original_read(path)
        if str(path).endswith('/' + spec.metrics_json_rel):
            metric_payloads.append(value)
        return value
    evaluator._read_json = read_json
    result = evaluator.evaluate(str(candidate), spec=spec)
    metrics = result['metrics'] if isinstance(result, dict) else result.metrics
    artifacts = result['artifacts'] if isinstance(result, dict) else result.artifacts
    if config['task_id'] == 'StructuralOptimization/PyMOTOSIMPCompliance':
        submission = root / 'benchmarks' / config['task_id'] / 'temp/submission.json'
        if submission.is_file():
            artifacts['native_source_temp::submission.json'] = submission.read_text()
    infrastructure_error = native_infrastructure_error(metrics, artifacts, metric_payloads)
    value = {'metrics': metrics, 'artifacts': artifacts}
    if infrastructure_error:
        value['infrastructure_error'] = infrastructure_error
    Path(output).write_text(json.dumps(json_safe_native(value), allow_nan=False) + '\n')


def native_infrastructure_error(metrics, artifacts, metric_payloads):
    if artifacts.get('metrics_json_error') or artifacts.get('artifacts_json_error'):
        return 'native grader output is malformed'
    if not metrics.get('timeout'):
        explicit = [p for p in metric_payloads if isinstance(p, dict) and all(k in p for k in ('combined_score', 'valid'))]
        if not explicit:
            return 'native grader did not produce explicit combined_score and valid metrics'
        try:
            if any(not math.isfinite(float(p[k])) for p in explicit for k in ('combined_score', 'valid')):
                return 'native primary metric payload is nonfinite'
        except (TypeError, ValueError):
            return 'native primary metric payload is not numeric'
    if any(type(metrics.get(k)) not in (int, float) or not math.isfinite(metrics[k]) for k in ('combined_score', 'valid')):
        return 'native combined_score or valid is missing or nonfinite'
    # These errors arise before a candidate result exists. Preserve invalid
    # candidates, compiler failures and task timeouts as native graded outcomes.
    message = str(artifacts.get('error_message', ''))
    if message.startswith(('runtime executable not found:', 'runtime env ', 'benchmark dir not found:',
                           'eval cwd does not exist:', 'scorer not found:', 'compiler unavailable:',
                           'failed to prepare trusted scoring context:', 'octave executable not found',
                           'failed to load reference points:', 'torch import failed:', 'CUDA is required',
                           'failed to load model:', 'could not prepare candidate inputs:',
                           'scorer reference self-test failed, refusing to score:',
                           'scorer produced no numeric combined_score',
                           'JobShop unified evaluation failed before completion.',
                           'EngDesign evaluation failed before artifacts were produced.')):
        return message
    diagnostics = [artifacts.get(k, '') for k in ('error_message', 'user_artifact::error_message', 'user_artifact::failure_reason')]
    def collect_diagnostics(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('error', 'error_message', 'candidate_error', 'failure_reason', 'stderr') and isinstance(item, str):
                    diagnostics.append(item)
                elif isinstance(item, (dict, list)):
                    collect_diagnostics(item)
        elif isinstance(value, list):
            for item in value:
                collect_diagnostics(item)
    for name, value in artifacts.items():
        if name.startswith('collected_artifact::') and isinstance(value, str):
            if name.endswith(('.stderr.txt', '/stderr.txt')):
                diagnostics.append(value)
            elif name.endswith('.json'):
                try:
                    collect_diagnostics(json.loads(value))
                except (TypeError, ValueError):
                    pass
    for diagnostic in diagnostics:
        if isinstance(diagnostic, str) and any(text in diagnostic for text in (
                "bwrap: Can't mount proc", "bwrap: Can't find source path /sys", 'bwrap: Creating new namespace failed',
                'candidate isolation helper not found', 'candidate isolation requires bubblewrap')):
            return 'native environment failure: ' + diagnostic
    task_results = artifacts.get('user_artifact::task_results', {})
    for record in task_results.values() if isinstance(task_results, dict) else ():
        message = str(record.get('error', ''))
        if message.startswith(('ModuleNotFoundError:', 'ImportError:', 'FileNotFoundError:',
                               'RuntimeError: Could not determine home directory')):
            return 'native EngDesign environment failure: ' + message
    return None


def preserve_native_result(metrics, artifacts):
    return {'metrics': metrics, 'artifacts': artifacts}


class NativeEvaluator:
    """Broker-facing callable. An outer namespace hides all broker state."""
    def __init__(self, config):
        self.config = config
        self.root = Path(config['source_root'])
        self.task = config['task']
        self.files = config['files']
        self.baseline = (self.root / 'benchmarks' / config['task_id'] / self.task['native_metadata']['initial_program']).read_text()
        self.evidence = {}
        self.preflight_done = False

    def command(self, work, arguments):
        sandbox = module('frontier_outer_sandbox', self.root / 'benchmarks/_shared/candidate_sandbox.py')
        driver = Path(__file__).resolve()
        # Some CPU-capable native runners request their optional GPU boundary
        # even without a GPU. Its original helper always reads /sys in that
        # mode, so retain that read-only system view for the nested helper.
        readable = [self.root, driver, Path('/sys')]
        if Path('/opt/frontier/toolchain').exists():
            readable.append(Path('/opt/frontier/toolchain'))
        command = sandbox.namespace_command(
            [sys.executable, '-I', str(driver), 'outer', *arguments],
            work, readonly_paths=readable, gpu=bool(self.task['runtime_recipe']['gpu']))
        command[1:1] = ['--cap-add', 'CAP_SYS_ADMIN', '--cap-add', 'CAP_SETUID', '--cap-add', 'CAP_SETGID', '--cap-add', 'CAP_SETFCAP']
        return command

    def verify(self):
        files = {**self.files, **{a['destination']: a['sha256'] for a in self.task['runtime_recipe'].get('pinned_assets', [])}}
        for name, expected in files.items():
            path = self.root / name
            if not path.is_file() or path.is_symlink() or digest(path) != expected:
                raise RuntimeError('frozen native source checksum mismatch: ' + name)

    def preflight(self):
        if self.preflight_done:
            return
        self.verify()
        for name in self.task['runtime_recipe']['required_assets']:
            if not (self.root / name).exists():
                raise RuntimeError('required native asset missing: ' + name)
        if self.config['task_id'] == 'ComputerSystems/MallocLab':
            tool = module('frontier_malloc_tools', self.root / 'benchmarks/_shared/malloc_wasm/setup.py')
            tool.resolve('/opt/frontier/toolchain')
        if self.config['task_id'] in ('Astrodynamics/MannedLunarLanding', 'EngDesign') and not shutil.which('octave'):
            raise RuntimeError('native task requires Octave in the runtime image')
        manifest = Path('/opt/frontier/build-manifest.json')
        if manifest.is_file():
            self.evidence = json.loads(manifest.read_text())
            self.evidence['manifest_sha256'] = digest(manifest)
        self.evidence['task_id'] = self.config['task_id']
        with tempfile.TemporaryDirectory(prefix='frontier-nested-preflight-') as directory:
            config_path = Path(directory) / 'probe.json'
            config_path.write_text(json.dumps({'imports': self.task['runtime_recipe']['imports'],
                                              'gpu': self.task['runtime_recipe']['gpu'],
                                              'environment': self.config.get('environment', {})}))
            result = subprocess.run(self.command(Path(directory), ['probe', str(self.root), str(config_path)]),
                                    capture_output=True, text=True, timeout=30)
            if result.returncode or 'frontier-native-namespace-ok' not in result.stdout:
                raise RuntimeError('nested native namespace preflight failed: ' + result.stderr[-6000:])
        self.evidence['nested_namespace_preflight'] = 'passed'
        self.preflight_done = True

    def __call__(self, source):
        self.preflight()
        with tempfile.TemporaryDirectory(prefix='rsi-frontier-native-') as directory:
            work = Path(directory)
            candidate = work / Path(self.task['native_metadata']['initial_program']).name
            candidate.write_text(source)
            public_config = {key: self.config[key] for key in ('source_root', 'source_commit', 'task_id', 'task', 'environment') if key in self.config}
            config_path = work / 'task-config.json'
            config_path.write_text(json.dumps(public_config))
            output = work / 'native-result.json'
            # The source tree is read-only, and /state, /opt/frontier/broker.json
            # and the broker PID are absent from this namespace.
            command = self.command(work, ['evaluate', str(config_path), str(candidate), str(output)])
            if self.config['task_id'] == 'StructuralOptimization/PyMOTOSIMPCompliance':
                scratch = work / 'native-temp'
                scratch.mkdir()
                destination = self.root / 'benchmarks' / self.config['task_id'] / 'temp'
                separator = command.index('--')
                command[separator:separator] = ['--bind', str(scratch), str(destination)]
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=self.task['native_evaluator_timeout_sec'] + 90,
                                       env={key: value for key, value in os.environ.items() if key in (
                                           'PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'OMP_NUM_THREADS',
                                           'OPENBLAS_NUM_THREADS', 'CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES',
                                           'NVIDIA_DRIVER_CAPABILITIES', 'PYTHONDONTWRITEBYTECODE')})
            self.verify()
            if completed.returncode or not output.is_file():
                raise RuntimeError('native unified evaluator failed: ' + completed.stderr[-6000:])
            value = json.loads(output.read_text())
            value['stdout'] = completed.stdout
            value['stderr'] = completed.stderr
            return value


if __name__ == '__main__':
    if sys.argv[1] == 'outer':
        prepare_outer_namespace()
        del sys.argv[1]
    if sys.argv[1] == 'bootstrap':
        bootstrap(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == 'evaluate':
        native_evaluate(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == 'probe':
        namespace_probe(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == 'pymoto':
        pymoto_main(sys.argv[2], sys.argv[3], sys.argv[4:])
    else:
        raise SystemExit('unknown Frontier full runtime operation')
