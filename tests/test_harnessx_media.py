"""Images reach Codex as images; native message and tool intent semantics remain."""
import asyncio
import base64
import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace
import zlib

import pytest

from integrations.harnessx.media import UnsupportedMediaError, mcp_media_blocks, prepare_messages


# One red pixel, generated from png_bytes() with macOS sips (JPEG) and cwebp
# lossless (WebP). These are fixed valid encoder outputs; neither utility is a
# test/runtime dependency and no external image or metadata is used.
JPEG_RED = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAASABIAAD/4QBMRXhpZgAATU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAAaADAAQAAAABAAAAAQAAAAD/7QA4UGhvdG9zaG9wIDMuMAA4QklNBAQAAAAAAAA4QklNBCUAAAAAABDUHYzZjwCyBOmACZjs+EJ+/8AAEQgAAQABAwEiAAIRAQMRAf/EAB8AAAEFAQEBAQEBAAAAAAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFBBhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/EAB8BAAMBAQEBAQEBAQEAAAAAAAABAgMEBQYHCAkKC//EALURAAIBAgQEAwQHBQQEAAECdwABAgMRBAUhMQYSQVEHYXETIjKBCBRCkaGxwQkjM1LwFWJy0QoWJDThJfEXGBkaJicoKSo1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoKDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uLj5OXm5+jp6vLz9PX29/j5+v/bAEMAAgICAgICAwICAwUDAwMFBgUFBQUGCAYGBgYGCAoICAgICAgKCgoKCgoKCgwMDAwMDA4ODg4ODw8PDw8PDw8PD//bAEMBAgICBAQEBwQEBxALCQsQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEP/dAAQAAf/aAAwDAQACEQMRAD8A+L6KKK/lM/38P//Z"
)
WEBP_RED = base64.b64decode("UklGRhwAAABXRUJQVlA4TA8AAAAvAAAAAAcQ/Y/+ByKi/wEA")


def png_bytes(color=b"\xff\x00\x00", *, animated=False):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    result = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    if animated:
        result += chunk(b"acTL", struct.pack(">II", 2, 0))
    return result + chunk(b"IDAT", zlib.compress(b"\x00" + color)) + chunk(b"IEND", b"")


def image_block(data=None, mime="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": mime,
        "data": base64.b64encode(data if data is not None else png_bytes()).decode()}}


@dataclass
class Message:
    role: str
    content: str | list
    tool_call_id: str | None = None
    tool_calls: tuple = ()


def fake_events(monkeypatch):
    for name in ("harnessx", "harnessx.core", "harnessx.core.events"):
        module = ModuleType(name)
        module.__path__ = []
        if name.endswith("events"):
            for member in ("ModelResponseEvent", "ToolCall", "Usage"):
                setattr(module, member, lambda **kwargs: SimpleNamespace(**kwargs))
        monkeypatch.setitem(sys.modules, name, module)


def test_order_call_identity_and_original_messages_preserved(tmp_path):
    red, blue = png_bytes(), png_bytes(b"\x00\x00\xff")
    messages = [Message("assistant", "request", tool_calls=({"id": "call-1", "name": "picture"},)),
        Message("tool", [{"type": "text", "text": "before"}, image_block(red),
                         {"type": "text", "text": "between"}, image_block(blue)], "call-1"),
        Message("user", [{"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(red).decode()}},
            {"type": "text", "text": "after"}])]
    original = copy.deepcopy(messages)
    prepared = prepare_messages(messages)
    assert messages == original
    assert prepared.messages[1]["tool_call_id"] == "call-1"
    assert prepared.messages[0]["tool_calls"][0]["id"] == "call-1"
    assert prepared.messages[1]["content"][0]["text"] == "before"
    assert prepared.messages[1]["content"][2]["text"] == "between"
    assert "Image attachment 2" in prepared.messages[1]["content"][3]["text"]
    assert "Image attachment 3" in prepared.messages[2]["content"][0]["text"]
    paths, receipts = prepared.materialize(tmp_path / "media")
    assert [path.read_bytes() for path in paths] == [red, blue, red]
    assert [r["block_path"] for r in receipts] == [[1], [3], [0]]
    assert all(not Path(r["path"]).is_absolute() for r in receipts)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    assert base64.b64encode(red).decode() not in json.dumps(prepared.messages)
    assert receipts[1]["sha256"] == hashlib.sha256(blue).hexdigest()


@pytest.mark.parametrize("block,match", [
    ({"type": "image_url", "image_url": {"url": "https://example.invalid/private.png"}}, "Remote URLs"),
    ({"type": "image", "source": {"type": "url", "url": "file:///tmp/image.png"}}, "Remote URLs"),
    ({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "not base64"}}, "malformed base64"),
    (image_block(b"not an image"), "does not match"),
    (image_block(mime="image/jpeg"), "JPEG"),
    (image_block(b"GIF89a", "image/gif"), "Unsupported image MIME"),
    (image_block(png_bytes(animated=True)), "Animated PNG"),
    ({"type": "input_audio", "input_audio": {"format": "wav", "data": "UklGRg=="}}, "Native input_audio"),
    ({"type": "video", "source": {}}, "Native video"),
    ({"type": "text", "text": 1}, "requires a string"),
    ({"type": "image", "source": "bad"}, "must be an object"),
])
def test_invalid_or_unsupported_media_is_explicit(block, match):
    with pytest.raises(UnsupportedMediaError, match=match):
        prepare_messages([Message("tool", [block])])


def test_animated_webp_and_turn_budget_rejected(monkeypatch):
    chunks = b"VP8X" + struct.pack("<I", 10) + b"\x02" + b"\0" * 9
    webp = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WEBP" + chunks
    with pytest.raises(UnsupportedMediaError, match="Animated WebP"):
        prepare_messages([Message("user", [image_block(webp, "image/webp")])])
    monkeypatch.setattr("integrations.harnessx.media.MAX_TURN_IMAGE_BYTES", len(png_bytes()) + 1)
    with pytest.raises(UnsupportedMediaError, match="per-turn"):
        prepare_messages([Message("user", [image_block(), image_block()])])


def test_embedded_resource_and_large_mcp_images_are_not_lost():
    image = image_block()["source"]["data"]
    rows = [SimpleNamespace(mimeType="image/png", data=image),
        SimpleNamespace(resource=SimpleNamespace(mimeType="audio/wav", blob="UklGRg==")),
        SimpleNamespace(mimeType="image/png", data=image * 20000),
        SimpleNamespace(text="text remains native")]
    result = mcp_media_blocks(rows)
    assert [row["type"] for row in result] == ["image", "audio", "image"]
    assert len(result[2]["source"]["data"]) > 1024 * 1024
    assert result[1]["source"]["data"] == "UklGRg=="


def test_provider_attachments_survive_serialization_retry(monkeypatch, tmp_path):
    fake_events(monkeypatch)
    from integrations.harnessx.provider import CodexProvider
    from rsi_eval.codex import CodexSettings, CodexResult
    seen = []
    def fake(prompt, **kwargs):
        seen.append((prompt, kwargs))
        assert kwargs["mode"] == "role"
        assert [path.read_bytes() for path in kwargs["images"]] == [png_bytes()]
        assert "Image attachment 1" in prompt
        assert "call-image" in prompt
        assert image_block()["source"]["data"] not in prompt
        return CodexResult(text="invalid-json" if len(seen) == 1 else '{"content":"red","tool_calls":[]}',
            events=[], usage={"input_tokens": 11, "output_tokens": 2}, cli_version="fixture")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("test", tmp_path / "auth"), tmp_path / "calls")
    message = Message("tool", [{"type": "text", "text": "Look at the pixel"}, image_block()], "call-image")
    response = asyncio.run(provider.complete([message], []))
    assert response.content == "red"
    assert len(seen) == 2
    assert response.usage.input_tokens == 22
    for _, kwargs in seen:
        record = json.loads((kwargs["log_dir"] / "transport.json").read_text())
        assert record["image_attachments"][0]["message_index"] == 0
        assert record["image_attachments"][0]["block_path"] == [1]
        assert (kwargs["log_dir"] / record["image_attachments"][0]["path"]).read_bytes() == png_bytes()


@pytest.mark.parametrize("mime,data,extension", [
    ("image/jpeg", JPEG_RED, ".jpg"),
    ("image/webp", WEBP_RED, ".webp"),
])
def test_provider_static_jpeg_and_webp_attachments(monkeypatch, tmp_path, mime, data, extension):
    fake_events(monkeypatch)
    from integrations.harnessx.provider import CodexProvider
    from rsi_eval.codex import CodexSettings, CodexResult
    calls = []
    def fake(prompt, **kwargs):
        calls.append(kwargs)
        assert len(kwargs["images"]) == 1
        assert kwargs["images"][0].suffix == extension
        assert kwargs["images"][0].read_bytes() == data
        assert "Image attachment 1" in prompt
        return CodexResult(text='{"content":"red","tool_calls":[]}', events=[],
            usage={"input_tokens": 11, "output_tokens": 2}, cli_version="fixture")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("test", tmp_path / "auth"), tmp_path / "calls")
    block = image_block(data, mime) if mime == "image/jpeg" else {
        "type": "input_image", "image_url": "data:" + mime + ";base64," + base64.b64encode(data).decode()}
    response = asyncio.run(provider.complete([Message("user", [block])], []))
    assert response.content == "red"
    receipt = json.loads((calls[0]["log_dir"] / "transport.json").read_text())["image_attachments"][0]
    assert receipt["mime_type"] == mime
    assert receipt["bytes"] == len(data)
    assert receipt["sha256"] == hashlib.sha256(data).hexdigest()


def test_unsupported_media_never_calls_model_and_latches(monkeypatch, tmp_path):
    fake_events(monkeypatch)
    from integrations.harnessx.provider import CodexProvider
    from rsi_eval.codex import CodexSettings
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", lambda *a, **kw: pytest.fail("Model must not run"))
    provider = CodexProvider(CodexSettings("test", tmp_path / "auth"), tmp_path / "calls")
    for _ in range(2):
        with pytest.raises(UnsupportedMediaError, match="audio"):
            asyncio.run(provider.complete([Message("tool", [{"type": "audio", "source": {}}])], []))
    assert not provider.calls
    failures = list((tmp_path / "calls").glob("media-error-*.json"))
    assert len(failures) == 1
    assert json.loads(failures[0].read_text())["codex_cli_calls"] == 0
