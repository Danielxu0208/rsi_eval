"""A custom runtime must execute the candidate identified by the SDK report."""
import json
from pathlib import Path
import sys

import pytest

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate


@pytest.mark.parametrize("redirect", [False, True])
def test_custom_runtime_loads_snapshot_and_rejects_redirect(tmp_path, redirect):
    source = tmp_path / "mutable"
    source.mkdir()
    (source / "policy.txt").write_text("snapshot-version")
    fake = tmp_path / "harbor-fixture"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
if '--version' in sys.argv:
    print('harbor 0.18.0')
    raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index('--config') + 1]).read_text())
agent = config['agents'][0]
source = Path(agent['kwargs']['source_for_fixture'])
(source / 'policy.txt').write_text('later-edit')
snapshot = Path(agent['kwargs']['artifact_dir'])
assert snapshot != source
assert (snapshot / 'policy.txt').read_text() == 'snapshot-version'
if agent['kwargs']['redirect_for_fixture']:
    agent['kwargs']['artifact_dir'] = str(source)
task = config['tasks'][0]
org, name = task['name'].split('/', 1)
trial = Path(config['jobs_dir']) / config['job_name'] / 'trial'
trial.mkdir(parents=True)
(trial / 'result.json').write_text(json.dumps({
    'task_name': task['name'],
    'task_id': {'org': org, 'name': name, 'ref': task['ref']},
    'config': {'agent': agent},
    'finished_at': '2026-10-10T00:00:00Z',
    'verifier_result': {'rewards': {'reward': 1}},
}))
(trial.parent / 'result.json').write_text(json.dumps({
    'finished_at': '2026-10-10T00:00:00Z', 'n_total_trials': 1,
}))
''')
    fake.chmod(0o700)
    result = evaluate(
        target=TargetSpec(source, "custom_runtime:Agent", {
            "artifact_dir": str(source), "source_for_fixture": str(source),
            "redirect_for_fixture": redirect,
        }),
        benchmark=BenchmarkSelection("terminal-bench", "2.0", ("extract-elf",)),
        options=RunOptions(harbor_executable=str(fake)), output_dir=tmp_path / "out",
    )
    assert (source / "policy.txt").read_text() == "later-edit"
    assert (tmp_path / "out/candidate/policy.txt").read_text() == "snapshot-version"
    if redirect:
        assert result.status == "failed"
        assert result.trials[0].reward is None
        assert "artifact_dir does not match" in result.error
    else:
        assert result.status == "completed", result.error
        assert result.trials[0].reward == 1
