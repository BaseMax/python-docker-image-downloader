"""
DockerRegistry — high-level client that orchestrates the full download flow.

Architecture
------------
- Thread-safe via per-thread ``requests.Session`` (thread-local storage).
- All I/O helpers live in focused sub-modules; this class is the glue.
"""

import logging
import os
import shutil
import tempfile
import threading
from typing import Dict, List, Optional, Tuple

import requests

from .archive import build_archive
from .auth import get_token
from .blob import decompress_layer, download_blob
from .exceptions import DockerRegistryError
from .manifest import fetch_manifest, get_platforms as _fetch_platforms, resolve_manifest
from .parser import (
    format_repo_tag,
    parse_image,
    safe_filename,
    validate_image_name,
    validate_registry_host,
)

logger = logging.getLogger(__name__)


class DockerRegistry:
    """
    High-level Docker Registry HTTP API v2 client.

    Parameters
    ----------
    username / password:
        Optional Docker Hub credentials.  Without them, anonymous pulls are
        subject to Docker Hub rate limits (~100 / 6 h per server IP).
    """

    _local = threading.local()

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.username = username or None
        self.password = password or None

    # ── Thread-local session ──────────────────────────────────────────────────
    @property
    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            s = requests.Session()
            s.headers["User-Agent"] = "docker-image-downloader/1.0"
            self._local.session = s
        return self._local.session

    # ── Public API ────────────────────────────────────────────────────────────
    def get_platforms(self, image: str) -> List[Dict[str, str]]:
        """
        Return the available platforms for *image*.
        Returns an empty list for single-arch images.
        """
        validate_image_name(image)
        registry, repo, ref = parse_image(image)
        validate_registry_host(registry)

        token = get_token(self._session, registry, repo, self.username, self.password)
        return _fetch_platforms(self._session, registry, repo, ref, token)

    def download_image(
        self,
        image: str,
        os_filter: Optional[str] = None,
        arch_filter: Optional[str] = None,
        variant_filter: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        """
        Download *image* and package it in ``docker save`` format.

        Returns
        -------
        ``(tar_path, filename, tmpdir)``
            The caller **must** delete *tmpdir* once the file has been served.
        """
        validate_image_name(image)
        registry, repo, ref = parse_image(image)
        validate_registry_host(registry)

        logger.info("Downloading %s  registry=%s repo=%s ref=%s", image, registry, repo, ref)

        token = get_token(self._session, registry, repo, self.username, self.password)
        manifest, _ = fetch_manifest(self._session, registry, repo, ref, token)
        manifest = resolve_manifest(
            self._session, registry, repo, ref, token, manifest,
            os_filter, arch_filter, variant_filter,
        )

        tmpdir = tempfile.mkdtemp(prefix="docker_dl_")
        try:
            config_path, config_filename = self._download_config(
                registry, repo, manifest, token, tmpdir
            )
            layer_entries = self._download_layers(
                registry, repo, manifest["layers"], token, tmpdir
            )

            repo_tag = format_repo_tag(image, ref)
            filename = safe_filename(image)
            tar_path = build_archive(
                tmpdir, config_path, config_filename, layer_entries, repo_tag, filename
            )

            logger.info(
                "Archive ready: %s  (%.1f MB)",
                filename,
                os.path.getsize(tar_path) / 1_048_576,
            )
            return tar_path, filename, tmpdir

        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

    # ── Private helpers ───────────────────────────────────────────────────────
    def _download_config(
        self,
        registry: str,
        repo: str,
        manifest: dict,
        token: Optional[str],
        tmpdir: str,
    ) -> Tuple[str, str]:
        """Download the image config blob; return ``(local_path, filename)``."""
        digest: str = manifest["config"]["digest"]
        config_hash = digest.replace("sha256:", "")
        config_filename = f"{config_hash}.json"
        dest = os.path.join(tmpdir, config_filename)

        logger.debug("  config: %s", config_filename)
        download_blob(self._session, registry, repo, digest, token, dest)
        return dest, config_filename

    def _download_layers(
        self,
        registry: str,
        repo: str,
        layers: list,
        token: Optional[str],
        tmpdir: str,
    ) -> List[Tuple[str, str]]:
        """Download + decompress all layers; return list of ``(path, arcname)``."""
        entries: List[Tuple[str, str]] = []

        for idx, layer in enumerate(layers, start=1):
            digest: str = layer["digest"]
            media_type: str = layer.get("mediaType", "")
            layer_hash = digest.replace("sha256:", "")

            logger.info(
                "  layer %d/%d: %s…  (%s bytes)",
                idx, len(layers), layer_hash[:12], layer.get("size", "?"),
            )

            layer_dir = os.path.join(tmpdir, layer_hash)
            os.makedirs(layer_dir, exist_ok=True)

            compressed = os.path.join(layer_dir, "blob")
            download_blob(self._session, registry, repo, digest, token, compressed)

            layer_tar = os.path.join(layer_dir, "layer.tar")
            decompress_layer(compressed, layer_tar, media_type)
            os.unlink(compressed)  # free disk space immediately

            entries.append((layer_tar, f"{layer_hash}/layer.tar"))

        return entries
