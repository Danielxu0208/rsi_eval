"""Small source-download helpers; no global caches or installed dependencies."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request


def download(url: str, destination: Path, sha256: str, *, headers: dict[str, str] | None = None) -> Path:
    """Download a public source and reject any change to its pinned contents."""
    if not url.startswith("https://"):
        raise ValueError("benchmark source downloads require HTTPS")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = sha256.removeprefix("sha256:")
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("source downloads require a SHA256 digest")
    cache_root = os.environ.get("RSI_EVAL_SOURCE_CACHE")
    cached = Path(cache_root).expanduser().resolve() / expected if cache_root else None
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".part", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(url, headers=headers) if headers else url
        source = cached.open("rb") if cached is not None and cached.is_file() else urllib.request.urlopen(request, timeout=60)
        with source as response, temporary.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                stream.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"benchmark source checksum mismatch: {url}")
        temporary.replace(destination)
        if cached is not None and not cached.exists():
            fd, cache_name = tempfile.mkstemp(prefix=expected + ".", dir=cached.parent)
            os.close(fd)
            cache_temp = Path(cache_name)
            try:
                shutil.copyfile(destination, cache_temp)
                cache_temp.replace(cached)
            finally:
                cache_temp.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
