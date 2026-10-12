"""Exercise the real SDK with a labeled fake Harbor process. No model calls."""
import json
from pathlib import Path
import sys
import tempfile

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate


with tempfile.TemporaryDirectory(prefix="rsi-offline-") as temp:
    root = Path(temp)
    candidate = root / "candidate"
    candidate.mkdir()
    (candidate / "agent.py").write_text("# Offline fixture candidate\n")
    fake = root / "fake-harbor"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
if '--version' in sys.argv:
    print('harbor 0.18.0 (offline fixture)'); raise SystemExit(0)
config = json.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
task = config['tasks'][0]
org, name = task['name'].split('/', 1)
job = Path(config['jobs_dir']) / config['job_name']
trial = job / 'extract-elf__fixture'
trial.mkdir(parents=True)
(trial / 'result.json').write_text(json.dumps({'task_name': task['name'], 'task_id': {'org': org, 'name': name, 'ref': task['ref']}, 'config': {'agent': config['agents'][0]}, 'finished_at': '2026-10-09T00:00:00Z', 'verifier_result': {'rewards': {'reward': 0}}, 'agent_result': {'n_input_tokens': 0, 'n_output_tokens': 0, 'n_cache_tokens': 0}}))
(job / 'result.json').write_text(json.dumps({'fixture': True, 'stats': {'reward': 0}, 'finished_at': '2026-10-09T00:00:00Z', 'n_total_trials': 1}))
''')
    fake.chmod(0o755)
    result = evaluate(target=TargetSpec(candidate, "oracle"),
                      benchmark=BenchmarkSelection("terminal-bench", "2.0", ("extract-elf",)),
                      options=RunOptions(harbor_executable=str(fake)), output_dir=root / "result")
    assert result.status == "completed", result.error
    print(json.dumps({"mode": "offline_fixture", "status": result.status,
                      "reward": result.trials[0].reward, "model_calls": 0}, indent=2))
