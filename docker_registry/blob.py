"""
Blob downloading and layer decompression.

Layers arrive from registries as gzip-compressed or zstd-compressed tarballs.
We stream them to disk, then decompress in place so the archive builder can
include plain ``layer.tar`` entries, matching the ``docker save`` format.
"""

import gzip
import os
import shutil
from typing import Optional

import requests

from .auth import auth_headers
from .exceptions import DockerRegistryError

_CHUNK_SIZE = 65_536  # 64 KiB


def download_blob(
    session: requests.Session,
    registry: str,
    repo: str,
    digest: str,
    token: Optional[str],
    dest_path: str,
) -> None:
    """Stream a registry blob to *dest_path* without loading it into memory."""
    url = f"https://{registry}/v2/{repo}/blobs/{digest}"

    with session.get(url, headers=auth_headers(token), stream=True, timeout=600) as resp:
        if resp.status_code == 404:
            raise DockerRegistryError(f"blob not found: {digest[:19]}…")
        if not resp.ok:
            raise DockerRegistryError(f"failed to download blob: {resp.status_code}")

        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if chunk:
                    fh.write(chunk)


def decompress_layer(src_path: str, dest_path: str, media_type: str) -> None:
    """
    Decompress *src_path* into *dest_path* as a plain tar.

    Handles gzip (most common), zstd (OCI estargz / nydus), and uncompressed
    tars transparently, using *media_type* as a hint and falling back to magic
    bytes.
    """
    if "gzip" in media_type or _is_gzip(src_path):
        _decompress_gzip(src_path, dest_path)
    elif "zstd" in media_type or _is_zstd(src_path):
        _decompress_zstd(src_path, dest_path)
    else:
        shutil.copy2(src_path, dest_path)


# ── Magic byte detectors ──────────────────────────────────────────────────────

def _is_gzip(path: str) -> bool:
    with open(path, "rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def _is_zstd(path: str) -> bool:
    with open(path, "rb") as fh:
        return fh.read(4) == b"\x28\xb5\x2f\xfd"


# ── Decompression helpers ─────────────────────────────────────────────────────

def _decompress_gzip(src: str, dest: str) -> None:
    with gzip.open(src, "rb") as gz_f, open(dest, "wb") as out_f:
        shutil.copyfileobj(gz_f, out_f, length=_CHUNK_SIZE)


def _decompress_zstd(src: str, dest: str) -> None:
    try:
        import zstandard as zstd  # type: ignore
    except ImportError:
        raise DockerRegistryError(
            'zstd-compressed image requires the "zstandard" Python package'
        )

    dctx = zstd.ZstdDecompressor()
    with open(src, "rb") as in_f, open(dest, "wb") as out_f:
        dctx.copy_stream(in_f, out_f)
