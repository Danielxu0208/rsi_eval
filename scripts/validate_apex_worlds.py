"""Boot each pinned APEX world serially, without an agent or paid judge.

Uses prepared official tasks and Harbor's native healthcheck/Compose layers.
Only this script's projects, volumes and newly downloaded image references are
removed. Snapshot archives are inspected inside the container, not retained.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import tomllib
import uuid

import yaml
from harbor.models.task.config import TaskConfig
from harbor.models.trial.paths import TrialPaths
from rsi_eval.harbor_docker import DockerEnvironment

MCP_PROBE = r'''
import hashlib,json,urllib.request
url='http://world:8000/mcp'
headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'}
def call(method,params,ident):
 data={'jsonrpc':'2.0','method':method,'params':params}
 if ident is not None:data['id']=ident
 request=urllib.request.Request(url,data=json.dumps(data).encode(),headers=headers,method='POST')
 with urllib.request.urlopen(request,timeout=180) as response:
  if response.headers.get('Mcp-Session-Id'):headers['Mcp-Session-Id']=response.headers['Mcp-Session-Id']
  body=response.read().decode()
 if not body:return {}
 if body.lstrip().startswith('{'):return json.loads(body)
 return json.loads(next(line[5:].strip() for line in body.splitlines() if line.startswith('data:')))
init=call('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'rsi-world-audit','version':'1'}},1)
assert 'result' in init,init
call('notifications/initialized',{},None)
tools=[];params={};seen=set()
while True:
 data=call('tools/list',params,2)['result'];tools.extend(data['tools'])
 cursor=data.get('nextCursor')
 if not cursor:break
 assert cursor not in seen,'repeated cursor'
 seen.add(cursor);params={'cursor':cursor}
tool=next(t for t in tools if t['name']=='filesystem_server_list_files')
read=call('tools/call',{'name':tool['name'],'arguments':{'path':'/'}},3)
assert 'result' in read and not read['result'].get('isError'),read
assert read['result'].get('content'),read
try:
 urllib.request.urlopen(urllib.request.Request(url,headers=headers,method='DELETE'),timeout=30).read()
 closed=True
except Exception:closed=False
print(json.dumps({'initialize':init['result'],'tools':tools,'readonly_call':read,'session_closed':closed}))
'''

SNAPSHOT_PROBE = r'''
import hashlib,json,pathlib,tarfile
out={}
for phase in ('initial','final'):
 p=pathlib.Path('/logs/world/'+phase+'_snapshot.tar.gz')
 assert p.is_file() and p.stat().st_size>0,str(p)
 h=hashlib.sha256()
 with p.open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 with tarfile.open(p,'r:gz') as t:
  names=[m.name for m in t]
 assert names,str(p)
 out[phase]={'size':p.stat().st_size,'sha256':h.hexdigest(),'members':len(names),
             'member_names_sha256':hashlib.sha256(json.dumps(names).encode()).hexdigest()}
script=pathlib.Path('/app/tools/collect_world_snapshot.py')
out['collector_sha256']=hashlib.sha256(script.read_bytes()).hexdigest()
print(json.dumps(out))
'''


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


async def docker(args, *, timeout=1200, check=True):
    proc = await asyncio.create_subprocess_exec('docker', *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        proc.kill(); await proc.communicate(); raise
    text = out.decode(errors='replace')
    if check and proc.returncode:
        raise RuntimeError('Docker command failed: ' + text[-3000:])
    return proc.returncode, text


async def validate(args):
    for key in list(os.environ):
        if key.endswith(('_API_KEY', '_AUTH_TOKEN', '_ACCESS_TOKEN')):
            os.environ.pop(key)
    os.environ['DOCKER_DEFAULT_PLATFORM'] = 'linux/amd64'
    catalog = json.loads(args.catalog.read_text())[0]
    worlds = {}
    for task in catalog['tasks']:
        # Prefer a native file-output task, while covering all worlds.
        current = worlds.get(task['world_id'])
        if current is None or task['expected_output'] != 'message_in_console' and current['expected_output'] == 'message_in_console':
            worlds[task['world_id']] = task
    assert len(worlds) == 31
    args.output.mkdir(parents=True, exist_ok=True)
    inventory = [json.loads(line) for line in (await docker(['image', 'ls', '--digests', '--format', 'json']))[1].splitlines() if line.strip()]
    cached = {r['Repository'] + '@' + r['Digest'] for r in inventory if r['Digest'] != '<none>'}
    report = {'status': 'running', 'world_count': 31, 'model_calls': 0, 'judge_calls': 0,
              'scope': 'one pinned task per world: native boot/health, MCP discovery/read, initial/final snapshots; no grading',
              'catalog_sha256': hashlib.sha256(args.catalog.read_bytes()).hexdigest(), 'worlds': [], 'passed': 0}
    for index, (world, task) in enumerate(sorted(worlds.items()), 1):
        root = args.output / world
        existing = root / 'result.json'
        if existing.exists():
            row = json.loads(existing.read_text())
            if row.get('status') == 'passed' and row.get('cleanup', {}).get('passed'):
                report['worlds'].append(row)
                report['passed'] += 1
                continue
            archived = root.with_name(world + '-attempt-' + uuid.uuid4().hex[:8])
            root.rename(archived)
        root.mkdir(parents=True, exist_ok=True)
        source = args.tasks / task['id']
        native = yaml.safe_load((source / 'environment/docker-compose.yaml').read_text())
        image = native['services']['world']['image']
        assert '@sha256:' in image
        config = TaskConfig.model_validate(tomllib.loads((source / 'task.toml').read_text()))
        assert hashlib.sha256((source / 'environment/docker-compose.yaml').read_bytes()).hexdigest() == task['files']['environment/docker-compose.yaml']
        project = 'rsi-apex-audit-' + uuid.uuid4().hex[:12]
        trial = TrialPaths(root / 'trial'); trial.mkdir()
        # Message-only tasks intentionally do not need initial snapshots for
        # scoring. Enable the *existing native* capture path for this component
        # audit only; official task files and grading remain untouched.
        overlay = root / 'snapshot-audit.json'
        write(overlay, {'services': {'world': {'environment': {'WORLD_CAPTURE_INITIAL_SNAPSHOT': '1'}}}})
        environment = DockerEnvironment(source / 'environment', 'apex-world-audit', project, trial,
                                        config.environment, extra_docker_compose=[str(overlay)])
        row = {'world_id': world, 'task_id': task['id'], 'package_ref': task['package_ref'],
               'image': image, 'project': project, 'status': 'running', 'started_at': time.time(),
               'audit_only_environment_override': {'WORLD_CAPTURE_INITIAL_SNAPSHOT': '1'},
               'native_expected_output': task['expected_output']}
        print(json.dumps({'index': index, 'total': 31, 'world': world, 'task': task['id'], 'phase': 'pull_and_boot'}), flush=True)
        try:
            if image not in cached:
                _, pull = await docker(['pull', '--platform', 'linux/amd64', image], timeout=1800)
                (root / 'pull.log').write_text(pull)
            _, inspected = await docker(['image', 'inspect', image, '--format',
                '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}},"Architecture":{{json .Architecture}},"Os":{{json .Os}},"Size":{{json .Size}}}'])
            info = json.loads(inspected)
            row['image_identity'] = {key: info.get(key) for key in ('Id', 'RepoDigests', 'Architecture', 'Os', 'Size')}
            await asyncio.wait_for(environment.start(force_build=False), timeout=1800)
            row['native_healthcheck_passed'] = True
            print(json.dumps({'index': index, 'phase': 'mcp_and_snapshots'}), flush=True)
            result = await environment._run_docker_compose_command(['exec', '-T', 'main', 'python3', '-c', MCP_PROBE], timeout_sec=240)
            mcp = json.loads(result.stdout)
            write(root / 'mcp.json', mcp)
            row['mcp'] = {'tools': len(mcp['tools']), 'readonly_tool': 'filesystem_server_list_files',
                          'read_passed': True, 'session_closed': mcp['session_closed'],
                          'record_sha256': hashlib.sha256((root / 'mcp.json').read_bytes()).hexdigest()}
            collected = await environment._run_docker_compose_command(['exec', '-T', 'world', 'python', '/app/tools/collect_world_snapshot.py'], timeout_sec=900)
            (root / 'collect.log').write_text(collected.stdout or '')
            snapshot = await environment._run_docker_compose_command(['exec', '-T', 'world', 'python', '-c', SNAPSHOT_PROBE], timeout_sec=900)
            row['snapshots'] = json.loads(snapshot.stdout)
            row['context_provenance'] = environment.context_provenance
            row['status'] = 'passed'
        except BaseException as exc:
            row['status'] = 'failed'; row['error'] = type(exc).__name__ + ': ' + str(exc)[-6000:]
            try:
                logs = await environment._run_docker_compose_command(['logs', '--no-color', '--tail', '100'], check=False, timeout_sec=30)
                (root / 'failure.log').write_text(logs.stdout or '')
            except Exception:
                pass
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
        finally:
            try:
                result = await environment._run_docker_compose_command(['down', '--volumes', '--remove-orphans'], check=False, timeout_sec=120)
                code, cleanup = result.return_code, result.stdout
            except Exception as exc:
                code, cleanup = 1, str(exc)
            (root / 'cleanup.log').write_text(cleanup or '')
            _, leftovers = await docker(['ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project])
            _, volumes = await docker(['volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project])
            row['cleanup'] = {'passed': code == 0 and not leftovers.strip() and not volumes.strip()}
            if image not in cached and row['cleanup']['passed']:
                code, removed = await docker(['image', 'rm', image], check=False)
                row['cleanup']['new_image_reference_removed'] = code == 0
                (root / 'image-cleanup.log').write_text(removed)
            environment._cleanup_mounts_compose_file()
            environment._cleanup_resources_compose_file()
            environment._cleanup_egress_control_services_compose_file()
            row['elapsed_seconds'] = time.time() - row['started_at']
            write(existing, row)
            report['worlds'].append(row)
            report['passed'] = sum(r['status'] == 'passed' and r.get('cleanup', {}).get('passed') for r in report['worlds'])
            write(args.output / 'result.json', report)
            print(json.dumps({'index': index, 'status': row['status'], 'passed_so_far': report['passed'],
                              'elapsed_seconds': round(row['elapsed_seconds']), 'cleanup': row['cleanup']}), flush=True)
    report['status'] = 'passed' if report['passed'] == 31 else 'incomplete'
    write(args.output / 'result.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tasks', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, default=Path('src/rsi_eval/data/catalog_apex_agents.json'))
    args = parser.parse_args()
    args.tasks, args.output = args.tasks.resolve(), args.output.resolve()
    asyncio.run(validate(args))
