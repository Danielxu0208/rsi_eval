"""Lossless image attachments for the subscription-only Codex transport.

HarnessX retains its original messages. Only the text transport replaces image
payloads with ordered references; the corresponding bytes travel through the
CLI's image input. No URL or local-path resolution occurs here.
"""
from __future__ import annotations

import base64
import binascii
import copy
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import struct


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TURN_IMAGE_BYTES = 64 * 1024 * 1024
IMAGE_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class UnsupportedMediaError(ValueError):
    """The subscription transport cannot represent this native content block."""


def _validate_static_image(data: bytes, mime: str) -> None:
    if mime not in IMAGE_EXTENSIONS:
        raise UnsupportedMediaError(f"Unsupported image MIME type: {mime}; use static PNG, JPEG or WebP")
    if mime == "image/png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise UnsupportedMediaError("Image payload does not match image/png")
        cursor, first, ended = 8, True, False
        while cursor + 12 <= len(data):
            length = struct.unpack(">I", data[cursor:cursor + 4])[0]
            kind = data[cursor + 4:cursor + 8]
            if cursor + length + 12 > len(data) or (first and (kind != b"IHDR" or length != 13)):
                raise UnsupportedMediaError("Malformed PNG image")
            if kind == b"acTL":
                raise UnsupportedMediaError("Animated PNG is unsupported; supply individual static frames explicitly")
            if kind == b"IEND":
                ended = length == 0 and cursor + 12 == len(data)
                break
            first = False
            cursor += length + 12
        if not ended:
            raise UnsupportedMediaError("Malformed PNG image")
    elif mime == "image/jpeg":
        if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
            raise UnsupportedMediaError("Malformed JPEG image or MIME mismatch")
    else:
        if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise UnsupportedMediaError("Image payload does not match image/webp")
        if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
            raise UnsupportedMediaError("Malformed WebP image")
        cursor, image = 12, False
        while cursor + 8 <= len(data):
            kind = data[cursor:cursor + 4]
            length = struct.unpack("<I", data[cursor + 4:cursor + 8])[0]
            end = cursor + 8 + length
            if end > len(data):
                raise UnsupportedMediaError("Malformed WebP image")
            if kind in {b"ANIM", b"ANMF"} or (kind == b"VP8X" and length and data[cursor + 8] & 2):
                raise UnsupportedMediaError("Animated WebP is unsupported; supply individual static frames explicitly")
            image |= kind in {b"VP8 ", b"VP8L"}
            cursor = end + (length % 2)
        if cursor != len(data) or not image:
            raise UnsupportedMediaError("Malformed WebP image")


def _decode_image(block: dict) -> tuple[bytes, str]:
    kind = block.get("type")
    if kind == "image" and "source" in block:
        source = block["source"]
        if not isinstance(source, dict):
            raise UnsupportedMediaError("Image source must be an object")
        if source.get("type") == "base64":
            mime, encoded = source.get("media_type"), source.get("data")
        elif source.get("type") == "url":
            mime, encoded = _data_url(source.get("url"))
        else:
            raise UnsupportedMediaError("Image source must contain base64 bytes or a data URL")
    elif kind == "image" and "data" in block:
        mime, encoded = block.get("mimeType"), block.get("data")
    else:
        url = block.get("image_url")
        if isinstance(url, dict):
            url = url.get("url")
        mime, encoded = _data_url(url)
    if not isinstance(mime, str) or not isinstance(encoded, str) or not encoded:
        raise UnsupportedMediaError("Image needs a MIME type and nonempty base64 data")
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise UnsupportedMediaError("Image exceeds the 20 MiB transport limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise UnsupportedMediaError("Image contains malformed base64 data") from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise UnsupportedMediaError("Image is empty or exceeds the 20 MiB transport limit")
    _validate_static_image(data, mime)
    return data, mime


def _data_url(url) -> tuple[str, str]:
    if not isinstance(url, str) or not url.startswith("data:"):
        raise UnsupportedMediaError("Remote URLs and file paths are not image inputs; provide embedded base64 bytes or a data URL")
    header, separator, encoded = url[5:].partition(",")
    if not separator or not header.endswith(";base64"):
        raise UnsupportedMediaError("Image data URL must use base64 encoding")
    return header[:-7], encoded


@dataclass(frozen=True)
class ImageAttachment:
    ordinal: int
    message_index: int
    block_path: tuple[int, ...]
    mime_type: str
    data: bytes

    def receipt(self) -> dict:
        return {"ordinal": self.ordinal, "message_index": self.message_index,
                "block_path": list(self.block_path), "mime_type": self.mime_type,
                "sha256": hashlib.sha256(self.data).hexdigest(), "bytes": len(self.data)}


@dataclass(frozen=True)
class PreparedMessages:
    messages: list[dict]
    images: list[ImageAttachment]

    def materialize(self, directory: Path) -> tuple[list[Path], list[dict]]:
        paths, records = [], []
        if self.images:
            directory.mkdir(mode=0o700)
        for image in self.images:
            receipt = image.receipt()
            name = f"image-{image.ordinal:04d}-{receipt['sha256']}{IMAGE_EXTENSIONS[image.mime_type]}"
            path = directory / name
            with path.open("xb") as stream:
                stream.write(image.data)
            path.chmod(0o600)
            paths.append(path)
            records.append({**receipt, "path": str(Path(directory.name) / name)})
        return paths, records


def prepare_messages(messages) -> PreparedMessages:
    """Keep message/call order and replace each image in-place with its ordinal."""
    images: list[ImageAttachment] = []
    total_bytes = 0

    def content(value, message_index, prefix=()):
        nonlocal total_bytes
        if isinstance(value, str):
            return value
        if not isinstance(value, (list, tuple)):
            raise UnsupportedMediaError("HarnessX message content must be text or a list of content blocks")
        converted = []
        for index, original in enumerate(value):
            if not isinstance(original, dict):
                raise UnsupportedMediaError("HarnessX content blocks must be objects")
            block = copy.deepcopy(original)
            kind = block.get("type")
            position = prefix + (index,)
            if kind in {"image", "image_url", "input_image"}:
                data, mime = _decode_image(block)
                total_bytes += len(data)
                if total_bytes > MAX_TURN_IMAGE_BYTES:
                    raise UnsupportedMediaError("Images exceed the 64 MiB per-turn transport limit")
                attachment = ImageAttachment(len(images) + 1, message_index, position, mime, data)
                images.append(attachment)
                block = {"type": "text", "text": f"[Image attachment {attachment.ordinal}; original message {message_index}, block {'.'.join(map(str, position))}; {mime}]"}
            elif kind in {"audio", "input_audio", "video", "input_video", "document", "file", "unsupported_media"}:
                raise UnsupportedMediaError(f"Native {kind} input is unsupported by the Codex subscription CLI; raw task media remains in the native/MCP records")
            elif kind == "tool_result":
                block["content"] = content(block.get("content", ""), message_index, position)
            elif kind == "text":
                if not isinstance(block.get("text"), str):
                    raise UnsupportedMediaError("Text content block requires a string")
            elif kind not in {"tool_use", "thinking", "redacted_thinking"}:
                raise UnsupportedMediaError(f"Unsupported HarnessX content block type: {kind}")
            converted.append(block)
        return converted

    converted = []
    for index, message in enumerate(messages):
        row = asdict(message)
        row["content"] = content(row["content"], index)
        converted.append(row)
    return PreparedMessages(converted, images)


def mcp_media_blocks(content) -> list[dict]:
    """Preserve model-facing media even when the native MCP formatter spills it.

    The original wire result remains in the bridge log. Audio/video blocks are
    retained so the provider reports unsupported input before calling a model.
    """
    blocks = []
    for block in content:
        mime, encoded = getattr(block, "mimeType", None), getattr(block, "data", None)
        resource = getattr(block, "resource", None)
        if resource is not None:
            mime, encoded = getattr(resource, "mimeType", None), getattr(resource, "blob", None)
        if not isinstance(mime, str) or encoded is None:
            continue
        modality = mime.partition("/")[0]
        if modality in {"image", "audio", "video"}:
            blocks.append({"type": modality, "source": {"type": "base64", "media_type": mime, "data": encoded}})
    return blocks
