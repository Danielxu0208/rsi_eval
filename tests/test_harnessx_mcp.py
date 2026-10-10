"""Task MCP isolation and real SDK session tests, without model calls."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from integrations.harnessx.task_mcp import TaskMCPBridge, server_specs, tool_name


@pytest.mark.parametrize('server', [
    {'name': 'duplicate', 'transport': 'ssh'},
    {'name': '../host', 'transport': 'stdio', 'command': 'python'},
    {'name': 'api', 'transport': 'streamable-http', 'url': 'http://user:pass@world/mcp'},
    {'name': 'api', 'transport': 'streamable-http', 'url': 'http://world/mcp?key=secret'},
    {'name': 'stdio', 'transport': 'stdio', 'command': 'python', 'args': [1]},
])
def test_invalid_server_does_not_start(server):
    with pytest.raises(ValueError):
        server_specs([server])


def test_server_names_and_tool_names_do_not_shadow():
    spec = {'name': 'world', 'transport': 'stdio', 'command': 'python', 'args': ['a b.py']}
    assert server_specs([spec]) == [spec]
    with pytest.raises(ValueError, match='unique'):
        server_specs([spec, spec])
    assert tool_name('world', 'Bash') != 'Bash'
    assert tool_name('world', 'a/b') != tool_name('world', 'a_b')
    assert len(tool_name('world' * 20, 'tool' * 50)) <= 64


class LocalEnvironment:
    """Test-only Harbor environment double; runs the real broker and MCP SDK."""
    def __init__(self):
        self.commands = []

    async def exec(self, command, timeout_sec=30, **kwargs):
        self.commands.append(command)
        env = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']}
        process = await asyncio.create_subprocess_exec('/bin/sh', '-c', command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
        except BaseException:
            process.kill()
            await process.communicate()
            raise
        return SimpleNamespace(return_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())

    async def upload_file(self, source, target):
        shutil.copyfile(source, target)

    async def download_file(self, source, target):
        shutil.copyfile(source, target)


def native_registry(monkeypatch):
    value = os.environ.get('RSI_HARNESSX_CHECKOUT')
    if not value:
        pytest.skip('Requires optional pinned native HarnessX environment')
    monkeypatch.syspath_prepend(str(Path(value).resolve()))
    from harnessx.tools.inmemory import InMemoryToolRegistry
    return InMemoryToolRegistry()


def test_persistent_stdio_tools_preserve_schema_result_error_and_close(monkeypatch, tmp_path):
    registry = native_registry(monkeypatch)
    server = tmp_path / 'fixture_server.py'
    server.write_text('''from mcp.server.fastmcp import FastMCP
mcp = FastMCP("fixture")
count = 0
@mcp.tool()
def counter(value: str) -> str:
    global count
    count += 1
    return f"{count}:{value}"
@mcp.tool()
def large() -> str:
    return "x" * 51000
@mcp.tool()
def fails(value: str) -> str:
    raise ValueError("fixture-tool-error:" + value)
mcp.run()
''')
    environment = LocalEnvironment()
    bridge = TaskMCPBridge(environment, [{'name': 'task', 'transport': 'stdio',
        'command': sys.executable, 'args': [str(server)]}], tmp_path / 'logs')

    async def run():
        try:
            await bridge.start(registry)
            schemas = registry.get_schemas()
            assert len(schemas) == 3
            schema = next(t.input_schema for t in schemas if t.name == tool_name('task', 'counter'))
            assert schema['required'] == ['value']
            assert schema['properties']['value']['type'] == 'string'
            first = await registry.execute(tool_name('task', 'counter'), {'value': 'hello; $(echo NO)'})
            second = await registry.execute(tool_name('task', 'counter'), {'value': 'second'})
            error = await registry.execute(tool_name('task', 'fails'), {'value': 'expected'})
            assert '1:hello; $(echo NO)' in first.output
            assert '2:second' in second.output
            assert 'fixture-tool-error:expected' in error.error
            large = await registry.execute(tool_name('task', 'large'), {})
            assert '[truncated' in large.output
            assert bridge.root + '/results/' in large.output
            assert len(list((Path(bridge.root) / 'results').glob('*.txt'))) == 1
            assert len(next((Path(bridge.root) / 'results').glob('*.txt')).read_text()) == 51000

            from harnessx import BaseTask, HarnessConfig, ModelConfig
            from integrations.harnessx.provider import CodexProvider
            from rsi_eval.codex import CodexResult, CodexSettings
            from harnessx.tracing.null_tracer import NullTracer
            prompts = []
            def fake(prompt, **kwargs):
                prompts.append(prompt)
                response = {'content': 'done', 'tool_calls': []}
                if len(prompts) == 1:
                    response['tool_calls'] = [{'name': tool_name('task', 'counter'),
                        'arguments': json.dumps({'value': 'native-loop'})}]
                else:
                    assert '3:native-loop' in prompt
                return CodexResult(text=json.dumps(response), events=[],
                    usage={'input_tokens': 1, 'output_tokens': 1}, cli_version='fixture')
            monkeypatch.setattr('integrations.harnessx.provider.run_codex', fake)
            monkeypatch.setenv('HARNESSX_HOME', str(tmp_path / 'home'))
            provider = CodexProvider(CodexSettings('fixture', tmp_path / 'no-auth'), tmp_path / 'calls')
            harness = ModelConfig(main=provider).agentic(HarnessConfig(
                tool_registry=registry, init_workspace=False, tracer=NullTracer()))
            native = await harness.run(BaseTask(description='Use the task counter', max_steps=3, max_cost_usd=None))
            assert native.exit_reason != 'error'
            assert len(prompts) == 2
        finally:
            await bridge.close()
    asyncio.run(run())
    lifecycle = json.loads((tmp_path / 'logs/lifecycle.json').read_text())
    assert lifecycle == {'closed': True, 'forced': False}
    assert json.loads((tmp_path / 'logs/00004.json').read_text())['response']['result']['isError']
    assert all('pip install' not in command or 'mcp==1.30.0' in command for command in environment.commands)
    assert (Path(bridge.root) / 'closed.json').exists()
    shutil.rmtree(bridge.root)


def test_close_after_failed_start_and_schema_failure(monkeypatch, tmp_path):
    registry = native_registry(monkeypatch)
    server = tmp_path / 'bad.py'
    server.write_text('raise RuntimeError("expected fixture failure")')
    bridge = TaskMCPBridge(LocalEnvironment(), [{'name': 'bad', 'transport': 'stdio',
        'command': sys.executable, 'args': [str(server)]}], tmp_path / 'logs')
    async def run():
        try:
            with pytest.raises(RuntimeError, match='initialization failed'):
                await bridge.start(registry)
        finally:
            await bridge.close()
    asyncio.run(run())
    assert registry.list_names() == []
    assert json.loads((tmp_path / 'logs/lifecycle.json').read_text())['closed']
    shutil.rmtree(bridge.root)


def test_native_mcp_image_reaches_codex_as_attachment(monkeypatch, tmp_path):
    """Real MCP session + native tool loop; only the subscription call is fake."""
    import base64
    import struct
    import zlib
    registry = native_registry(monkeypatch)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))
    encoded = base64.b64encode(png).decode()
    server = tmp_path / 'image_server.py'
    server.write_text('''import base64
from mcp.server.fastmcp import FastMCP, Image
mcp = FastMCP("image-fixture")
@mcp.tool()
def picture() -> Image:
    return Image(data=base64.b64decode(%r), format="png")
mcp.run()
''' % encoded)
    bridge = TaskMCPBridge(LocalEnvironment(), [{'name': 'task', 'transport': 'stdio',
        'command': sys.executable, 'args': [str(server)]}], tmp_path / 'logs')
    from harnessx import BaseTask, HarnessConfig, ModelConfig
    from harnessx.tracing.null_tracer import NullTracer
    from integrations.harnessx.provider import CodexProvider
    from rsi_eval.codex import CodexResult, CodexSettings
    calls = []
    def fake(prompt, **kwargs):
        calls.append((prompt, kwargs))
        response = {'content': 'The image is red', 'tool_calls': []}
        if len(calls) == 1:
            assert not kwargs.get('images')
            response['tool_calls'] = [{'name': tool_name('task', 'picture'), 'arguments': '{}'}]
        else:
            assert len(kwargs['images']) == 1
            assert kwargs['images'][0].read_bytes() == png
            assert 'Image attachment 1' in prompt
            assert encoded not in prompt
            message = json.loads(prompt.split('\n\n', 1)[1])['messages'][-1]
            assert message['role'] == 'tool'
            assert message['tool_call_id']
            assert message['name'] == tool_name('task', 'picture')
        return CodexResult(text=json.dumps(response), events=[],
            usage={'input_tokens': 3, 'output_tokens': 2}, cli_version='fixture')
    monkeypatch.setattr('integrations.harnessx.provider.run_codex', fake)
    monkeypatch.setenv('HARNESSX_HOME', str(tmp_path / 'home'))
    provider = CodexProvider(CodexSettings('fixture', tmp_path / 'no-auth'), tmp_path / 'calls')
    async def run():
        try:
            await bridge.start(registry)
            harness = ModelConfig(main=provider).agentic(HarnessConfig(
                tool_registry=registry, init_workspace=False, tracer=NullTracer()))
            native = await harness.run(BaseTask(description='Use the picture tool, then identify the color',
                                                max_steps=3, max_cost_usd=None))
            assert native.exit_reason != 'error'
            assert len(calls) == 2
        finally:
            await bridge.close()
    asyncio.run(run())
    raw = json.loads((tmp_path / 'logs/00002.json').read_text())['response']['result']['content']
    assert next(block['data'] for block in raw if block['type'] == 'image') == encoded
    assert json.loads((tmp_path / 'logs/lifecycle.json').read_text()) == {'closed': True, 'forced': False}
    shutil.rmtree(bridge.root)


def test_selection_hook_keeps_raw_metrics_and_native_eligible_choices(tmp_path):
    from integrations.harnessx.launcher import choose_evaluated
    from rsi_eval.method_policy import normalize_policy
    hook = tmp_path / 'policy.py'
    hook.write_text('''def choose(payload):
    assert payload['native_choice'] == 'candidate'
    assert all(row['native_gates_passed'] for row in payload['candidates'])
    assert payload['candidates'][1]['evaluation']['trials'][0]['reward'] == .4
    return max(payload['candidates'], key=lambda row: sum(row['mapped_scores']))['id']
''')
    candidates = []
    for name, reward in [('incumbent', .8), ('candidate', .4)]:
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps({'trials': [{'status': 'completed', 'reward': reward}]}))
        candidates.append({'report_path': str(path), 'candidate_hash': name})
    config = {'evolve': {'name': 'drop', 'version': 'test'}, 'method_policy': normalize_policy({
        'score': {'kind': 'threshold', 'threshold': .5},
        'selection': {'kind': 'python', 'path': str(hook), 'function': 'choose'}}, tmp_path)}
    promoted, record = choose_evaluated(config, *candidates)
    assert not promoted
    assert record['selected'] == 'incumbent'
    assert [row['mapped_scores'] for row in record['eligible']] == [[1], [0]]
    assert choose_evaluated({'evolve': config['evolve']}, *candidates)[0]
