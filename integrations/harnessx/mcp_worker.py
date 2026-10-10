"""Container-only MCP session broker. Launched by task_mcp, never by a model.

The MCP SDK and all task commands run inside the Harbor environment. A local
Unix socket transports requests; the session owner also performs teardown.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import signal
import socket
import sys


async def serve(root: Path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client

    queue = asyncio.Queue()
    owner = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, owner.cancel)
    sessions = {}
    server = None
    try:
        async with AsyncExitStack() as stack:
            for config in json.loads((root / 'servers.json').read_text()):
                if config['transport'] == 'stdio':
                    transport = stdio_client(StdioServerParameters(command=config['command'], args=config['args']))
                elif config['transport'] == 'sse':
                    transport = sse_client(config['url'], timeout=30, sse_read_timeout=300)
                else:
                    transport = streamablehttp_client(config['url'], timeout=30, sse_read_timeout=300)
                streams = await stack.enter_async_context(transport)
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
                sessions[config['name']] = session

            async def accept(reader, writer):
                try:
                    request = json.loads(await reader.readline())
                    future = loop.create_future()
                    await queue.put((request, future))
                    result = await future
                    writer.write((json.dumps(result) + '\n').encode())
                    await writer.drain()
                except (ConnectionError, asyncio.CancelledError):
                    pass
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_unix_server(accept, str(root / 'broker.sock'), limit=32 * 1024 * 1024)
            (root / 'ready.json').write_text(json.dumps({'ok': True}))
            while True:
                request, future = await queue.get()
                try:
                    async with asyncio.timeout(request.get('timeout', 300)):
                        session = sessions[request['server']]
                        if request['operation'] == 'list':
                            rows, cursor, seen = [], None, set()
                            while True:
                                result = await session.list_tools(cursor=cursor)
                                rows.extend(tool.model_dump(mode='json') for tool in result.tools)
                                cursor = result.nextCursor
                                if not cursor:
                                    break
                                if cursor in seen:
                                    raise RuntimeError('MCP tools/list returned a repeated pagination cursor')
                                seen.add(cursor)
                            response = {'tools': rows}
                        elif request['operation'] == 'call':
                            response = (await session.call_tool(request['name'], request['arguments'])).model_dump(mode='json')
                        else:
                            raise ValueError('Unknown MCP broker operation')
                    result = {'ok': True, 'result': response}
                except Exception as exc:
                    result = {'ok': False, 'error': {'type': type(exc).__name__, 'message': str(exc)}}
                if not future.done():
                    future.set_result(result)
    except asyncio.CancelledError:
        pass
    except BaseException as exc:
        (root / 'ready.json').write_text(json.dumps({'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)}))
        raise
    finally:
        if server:
            server.close()
            await server.wait_closed()
        (root / 'closed.json').write_text(json.dumps({'closed': True}))


def request(root: Path, encoded: str):
    import base64
    payload = base64.b64decode(encoded)
    timeout = json.loads(payload).get('timeout', 300) + 10
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(root / 'broker.sock'))
        client.sendall(payload + b'\n')
        reader = client.makefile('rb')
        result = reader.readline()
        if not result:
            raise RuntimeError('MCP broker disconnected before returning a result')
        sys.stdout.buffer.write(result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('serve', 'request'))
    parser.add_argument('root', type=Path)
    parser.add_argument('payload', nargs='?')
    args = parser.parse_args()
    if args.operation == 'serve':
        try:
            asyncio.run(serve(args.root))
        except BaseException as exc:
            (args.root / 'ready.json').write_text(json.dumps({'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)}))
            (args.root / 'closed.json').write_text(json.dumps({'closed': True}))
            raise
    else:
        request(args.root, args.payload)
