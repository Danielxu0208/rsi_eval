"""Small live subscription check of native HarnessX image observations.

Runs one native tool turn and one visual answer, without a benchmark or judge.
Use the optional HarnessX environment and an explicitly supplied Codex profile.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import zlib


def png(rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    width, height = 128, 128
    rows = []
    for y in range(height):
        pixels = b"".join(bytes(rgb if 24 <= x < 104 and 24 <= y < 104 else (255, 255, 255))
                          for x in range(width))
        rows.append(b"\0" + pixels)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


async def validate(args):
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["HARNESSX_HOME"] = str(args.output / "native-home")
    from integrations.harnessx.prepare import check_checkout
    check_checkout(args.checkout)
    sys.path.insert(0, str(args.checkout))
    from harnessx import BaseTask, HarnessConfig, ModelConfig
    from harnessx.tools.base import Tool, ToolResult
    from harnessx.tools.inmemory import InMemoryToolRegistry
    from harnessx.tracing.null_tracer import NullTracer
    from integrations.harnessx.provider import CodexProvider
    from rsi_eval.codex import CodexSettings

    settings = CodexSettings(**json.loads(args.profile.read_text())["codex"])
    images = [png((255, 0, 0)), png((0, 0, 255))]
    for i, content in enumerate(images, 1):
        (args.output / f"figure-{i}.png").write_bytes(content)
    observations = []

    async def inspect_fixture():
        observations.append("InspectFixture")
        return ToolResult(output="Two images in their recorded order.", content_blocks=[
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                           "data": base64.b64encode(content).decode()}}
            for content in images
        ])

    registry = InMemoryToolRegistry()
    registry.register(Tool(name="InspectFixture", description="Obtain the two figures to inspect.",
                           input_schema={"type": "object", "properties": {}, "required": [],
                                         "additionalProperties": False}, fn=inspect_fixture))
    provider = CodexProvider(settings, args.output / "calls")
    harness = ModelConfig(main=provider).agentic(HarnessConfig(
        tool_registry=registry, init_workspace=False, tracer=NullTracer()))
    record = {"status": "running", "kind": "native_harnessx_image_transport_smoke",
              "model": settings.model, "judge_calls": 0, "benchmark_calls": 0,
              "image_sha256": [hashlib.sha256(data).hexdigest() for data in images],
              "scope": "Native HarnessX tool results to Codex image attachments; synthetic figures, no benchmark score"}
    try:
        result = await harness.run(BaseTask(
            description="Call InspectFixture exactly once. Inspect the attached figures, then return only "
                        "the lowercase color name of each central square in image order, separated by a comma. "
                        "Do not infer colors from filenames or hashes. Do not call other tools.",
            max_steps=3, max_cost_usd=None))
        answer = str(result.final_output).strip().lower().replace(" ", "").rstrip(".")
        record.update(answer=answer, exit_reason=result.exit_reason,
                      native_tool_calls=len(observations), accounting=provider.accounting())
        assert observations == ["InspectFixture"], "Expected one native tool execution"
        assert answer == "red,blue", "The visual answer did not match both images in order"
        attached = list((args.output / "calls").glob("*/images.json"))
        assert attached, "No CLI attachment receipt was recorded"
        receipt = json.loads(attached[-1].read_text())
        assert [row["sha256"] for row in receipt] == record["image_sha256"]
        assert all(call.get("usage", {}).get("input_tokens") is not None for call in provider.calls)
        record.update(status="passed", attachment_receipt=str(attached[-1].relative_to(args.output)))
    except BaseException as exc:
        record.update(status="failed", error=type(exc).__name__ + ": " + str(exc),
                      accounting=provider.accounting())
        raise
    finally:
        (args.output / "validation.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: record[key] for key in ("status", "answer", "native_tool_calls", "model")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args()
    parsed.checkout = parsed.checkout.resolve()
    parsed.output = parsed.output.resolve()
    asyncio.run(validate(parsed))
