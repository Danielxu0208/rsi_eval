"""Task-scoped MCP tools for the native HarnessX registry.

Only explicit Harbor task servers are loaded. The SDK broker runs inside the
trial environment, so service names, stdio commands and network policies retain
their original meaning. No host MCP discovery or authentication is forwarded.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import uuid
from urllib.parse import urlsplit

from .media import mcp_media_blocks

MCP_VERSION = '1.30.0'


def server_specs(servers):
    specs, names = [], set()
    for server in servers or ():
        row = server.model_dump() if hasattr(server, 'model_dump') else dict(server)
        name, transport = row.get('name'), row.get('transport')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name) or name in names:
            raise ValueError('Task MCP server names must be unique simple names')
        names.add(name)
        if transport in {'streamable-http', 'sse'}:
            url = row.get('url')
            parsed = urlsplit(url) if isinstance(url, str) else None
            if not parsed or parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Task MCP URL must be HTTP without embedded credentials')
            specs.append({'name': name, 'transport': transport, 'url': url})
        elif transport == 'stdio':
            command, args = row.get('command'), row.get('args', [])
            if not isinstance(command, str) or not command or not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                raise ValueError('Task MCP stdio requires a command and string arguments')
            specs.append({'name': name, 'transport': transport, 'command': command, 'args': args})
        else:
            raise ValueError('Unsupported task MCP transport: ' + str(transport))
    return specs


def tool_name(server, name):
    # Preserve readable names, but never shadow a harness builtin. Hash the full
    # identity rather than truncating into ambiguous collisions.
    original = f'{server}__{name}'
    cleaned = re.sub('[^A-Za-z0-9_-]', '_', original)
    digest = hashlib.sha256(original.encode()).hexdigest()[:10]
    return f'mcp__{cleaned[:44]}_{digest}'


class TaskMCPBridge:
    def __init__(self, environment, servers, logs_dir):
        self.environment = environment
        self.servers = server_specs(servers)
        self.logs = Path(logs_dir)
        self.root = '/tmp/rsi-harnessx-mcp-' + uuid.uuid4().hex
        self.python = self.root + '/python'
        self.started = False
        self.lock = asyncio.Lock()
        self.counter = 0

    async def _exec(self, command, timeout=30):
        result = await self.environment.exec(command=command, timeout_sec=timeout)
        if result.return_code:
            raise RuntimeError(f'Task MCP command failed with exit {result.return_code}: {result.stderr or result.stdout}')
        return result.stdout or ''

    async def start(self, registry):
        if not self.servers:
            return
        from harnessx.tools.base import Tool, ToolResult
        from harnessx.tools.mcp import MCPClient, _mcp_results_ctx
        from mcp.types import CallToolResult, TextContent
        self.logs.mkdir(parents=True, exist_ok=True)
        self.started = True  # cleanup partial installation/startup too
        await self._exec('mkdir -m 700 ' + shlex.quote(self.root) + ' && mkdir ' + shlex.quote(self.root + '/results'))
        config = self.logs / 'servers.json'
        config.write_text(json.dumps(self.servers, indent=2) + '\n')
        await self.environment.upload_file(config, self.root + '/servers.json')
        await self.environment.upload_file(Path(__file__).with_name('mcp_worker.py'), self.root + '/worker.py')
        # Use an existing matching SDK when present. Otherwise install only in
        # a dedicated per-trial venv, leaving task Python packages unchanged.
        bootstrap = """set -eu
ROOT=%s
if python3 -c 'import importlib.metadata; assert importlib.metadata.version("mcp") == "%s"' >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif command -v uv >/dev/null 2>&1; then
    uv venv --python python3 "$ROOT/venv"
    uv pip install --python "$ROOT/venv/bin/python" 'mcp==%s'
    PYTHON="$ROOT/venv/bin/python"
else
    python3 -m venv "$ROOT/venv"
    "$ROOT/venv/bin/python" -m pip install --disable-pip-version-check 'mcp==%s'
    PYTHON="$ROOT/venv/bin/python"
fi
printf '#!/bin/sh\nexec "%%s" "$@"\n' "$PYTHON" > "$ROOT/python"
chmod 700 "$ROOT/python"
nohup "$ROOT/python" "$ROOT/worker.py" serve "$ROOT" >"$ROOT/worker.log" 2>&1 </dev/null &
echo $! > "$ROOT/pid"
""" % (shlex.quote(self.root), MCP_VERSION, MCP_VERSION, MCP_VERSION)
        await self._exec(bootstrap, timeout=180)
        ready = await self._exec(
            f'{shlex.quote(self.python)} -c ' + shlex.quote(
                "import json,time,pathlib; p=pathlib.Path(" + repr(self.root + '/ready.json') + "); "
                "\nfor _ in range(240):\n if p.exists(): print(p.read_text());break\n time.sleep(.5)\nelse: raise TimeoutError('Task MCP initialization timed out')"), timeout=125)
        if not json.loads(ready).get('ok'):
            raise RuntimeError('Task MCP initialization failed: ' + ready)
        definitions, pending = [], []
        occupied = set(registry.list_names())
        for server in self.servers:
            result = await self._request({'operation': 'list', 'server': server['name']})
            for definition in result['tools']:
                original = definition.get('name')
                schema = definition.get('inputSchema')
                if not isinstance(original, str) or not original or not isinstance(schema, dict):
                    raise ValueError('Task MCP returned an invalid tool definition')
                public_name = tool_name(server['name'], original)
                if public_name in occupied:
                    raise ValueError('Task MCP tool name collision: ' + public_name)
                occupied.add(public_name)
                definitions.append({'server': server['name'], 'registered_name': public_name, 'definition': definition})
                def bind_call(_server, _name):
                    async def call(**arguments):
                        result = await self._request({'operation': 'call', 'server': _server, 'name': _name, 'arguments': arguments})
                        native = CallToolResult.model_validate(result)
                        # Native HarnessX controls truncation, media handling and
                        # error propagation. The complete wire result remains saved.
                        content = list(native.content)
                        if native.structuredContent is not None and not any(getattr(block, 'text', '') for block in content):
                            structured = json.dumps(native.structuredContent, ensure_ascii=False)
                            content.append(TextContent(type='text', text=structured))
                        formatter = MCPClient(transport='http')
                        saved = self.logs / 'results'
                        saved.mkdir(exist_ok=True)
                        before = set(saved.iterdir())
                        token = _mcp_results_ctx.set(str(saved))
                        try:
                            text, _, is_error = await formatter._process_content(content, _name, native.isError, frozenset())
                            # Retain native file spill/truncation and error behavior,
                            # while also passing media to the subscription provider.
                            # It validates image bytes and rejects unsupported audio
                            # explicitly; no media is mistaken for plain path text.
                            blocks = mcp_media_blocks(content)
                        finally:
                            _mcp_results_ctx.reset(token)
                        # Native text/media spill files must be readable by
                        # subsequent task tools inside the Harbor container.
                        for path in set(saved.iterdir()) - before:
                            target = self.root + '/results/' + path.name
                            await self.environment.upload_file(path, target)
                            text = text.replace(str(path), target)
                        return ToolResult(output='' if is_error else text,
                                          error=text if is_error else None, content_blocks=blocks or None)
                    return call
                pending.append(Tool(name=public_name, description=definition.get('description') or '',
                    input_schema=schema, fn=bind_call(server['name'], original), tags=['mcp', 'task', server['name']]))
        (self.logs / 'tools.json').write_text(json.dumps(definitions, indent=2) + '\n')
        for tool in pending:
            registry.register(tool)

    async def _request(self, request):
        async with self.lock:
            self.counter += 1
            path = self.logs / f'{self.counter:05d}.json'
            request = {**request, 'timeout': 300}
            encoded = base64.b64encode(json.dumps(request).encode()).decode()
            record = {'request': request}
            try:
                raw = await self._exec(f'{shlex.quote(self.python)} {shlex.quote(self.root + "/worker.py")} request {shlex.quote(self.root)} {shlex.quote(encoded)}', timeout=315)
                response = json.loads(raw)
                record['response'] = response
                if not response.get('ok'):
                    raise RuntimeError('Task MCP request failed: ' + str(response.get('error')))
                return response['result']
            except BaseException as exc:
                record['error'] = {'type': type(exc).__name__, 'message': str(exc)}
                raise
            finally:
                path.write_text(json.dumps(record, indent=2) + '\n')

    async def close(self):
        if not self.started:
            return
        self.started = False
        root = shlex.quote(self.root)
        # SIGTERM cancels the session owner, which closes the SDK ExitStack and
        # its stdio children. Bound teardown in a dying environment.
        command = f'if test -f {root}/pid; then kill -TERM "$(cat {root}/pid)" 2>/dev/null || true; fi'
        status = {'closed': False}
        try:
            await self._exec(command)
            raw = await self._exec(f'python3 -c ' + shlex.quote(
                "import json,time,pathlib,os,signal; p=pathlib.Path(" + repr(self.root) + "); "
                "\nfor _ in range(40):\n if (p/'closed.json').exists() or not (p/'pid').exists(): break\n time.sleep(.25)"
                "\nclosed=(p/'closed.json').exists() or not (p/'pid').exists()"
                "\nif not closed:\n try: os.kill(int((p/'pid').read_text()),signal.SIGKILL)\n except ProcessLookupError: pass"
                "\nprint(json.dumps({'closed':closed,'forced':not closed}))"), timeout=15)
            status = json.loads(raw)
        except Exception as exc:
            status['error'] = str(exc)
        finally:
            try:
                await self.environment.download_file(self.root + '/worker.log', self.logs / 'worker.log')
            except Exception:
                pass
            self.logs.mkdir(parents=True, exist_ok=True)
            (self.logs / 'lifecycle.json').write_text(json.dumps(status, indent=2) + '\n')
