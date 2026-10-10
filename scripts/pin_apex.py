"""Refresh the reviewed APEX catalog from already resolved immutable packages.

Input is the official Harbor client's DatasetMetadata and package archive list.
No login or private API key is used; the application key is Harbor's published
anonymous public-read key. This script is a maintainer tool, not a moving release.
"""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import tarfile
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.runtime/workspace-expansion'
BASE = 'https://ofhuhcpkvzjlejydnvyd.supabase.co/storage/v1/object/authenticated/packages/'
PUBLIC_KEY = 'sb_publishable_Z-vuQbpvpG-PStjbh4yE0Q_e-d3MTIH'


def record(item):
    slug = item['name'].split('/')[1]
    archive = WORK / 'apex-archives' / (slug + '.tar.gz')
    archive.parent.mkdir(exist_ok=True)
    if not archive.exists():
        request = urllib.request.Request(BASE + item['archive_path'], headers={
            'apikey': PUBLIC_KEY, 'Authorization': 'Bearer ' + PUBLIC_KEY})
        with urllib.request.urlopen(request, timeout=120) as stream:
            archive.write_bytes(stream.read())
    with tarfile.open(archive) as source:
        files = {member.name: hashlib.sha256(source.extractfile(member).read()).hexdigest()
                 for member in source if member.isfile()}
    task_path = WORK / 'apex-tasks' / slug
    actual = {p.relative_to(task_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in task_path.rglob('*') if p.is_file()}
    if actual != files:
        raise ValueError('Archive differs from official Harbor export: ' + slug)
    cfg = tomllib.loads((task_path / 'task.toml').read_text())
    return {'id': slug, 'package_name': item['name'], 'package_ref': item['ref'],
            'archive_path': item['archive_path'],
            'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'archive_size': archive.stat().st_size, 'files': files,
            **{key: cfg['metadata'][key] for key in ('domain', 'world_id', 'task_id', 'expected_output')}}


if __name__ == '__main__':
    packages = json.loads((WORK / 'apex-archives.json').read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        tasks = sorted(pool.map(record, packages), key=lambda x: x['id'])
    assert len(tasks) == 240
    spec = {'name': 'apex-agents', 'version': '1.1.1',
        'dataset_ref': 'mercor/apex-agents-1-1@1.1.1', 'materializer': 'apex_agents',
        'source': {'url': 'https://hub.harborframework.com/datasets/mercor/apex-agents-1-1/latest',
                   'sha256': '3fe2900ebd603ea5a802049819ce6ead171575ee0166a61187065ad9606c955d'},
        'coverage': {'scope': 'complete-public-pinned-release', 'task_count': 240,
                     'by_domain': {d: sum(t['domain'] == d for t in tasks) for d in sorted({t['domain'] for t in tasks})}},
        'protocol': 'Native APEX-Agents 1.1.1 Harbor tasks: immutable agent/world/verifier images, original MCP environment, rubric grader and snapshot collection. The selected target is an agent variant; no claim of the published reference agent run.',
        'requirements': ['Docker with linux/amd64 images', 'World MCP support in the target',
                         'VERCEL_AI_GATEWAY_API_KEY for the published native judge'],
        'usage_terms': 'Publisher restricts APEX data to evaluation, not training, fine-tuning or parameter fitting. See publisher terms before using it for method development.',
        'oracle': {'supported': True, 'note': 'Original upstream solution scripts, not synthetic answers.'},
        'aggregation': {'type': 'rubric_mean', 'group_by': ['domain']},
        'tasks': tasks}
    (ROOT / 'src/rsi_eval/data/catalog_apex_agents.json').write_text(json.dumps([spec], indent=2) + '\n')
    print(json.dumps({'tasks': len(tasks), 'archive_bytes': sum(t['archive_size'] for t in tasks),
                      'domains': spec['coverage']['by_domain']}))
