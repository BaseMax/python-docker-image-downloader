"""
docker_registry.py – Docker Registry HTTP API v2 client.

Handles:
  - Parsing image references  (registry, repo, tag/digest)
  - Bearer-token auth for Docker Hub and generic registries
  - Manifest retrieval (manifest list / OCI index → single manifest)
  - Platform detection
  - Streaming blob downloads to temp files
  - gzip / zstd layer decompression
  - Packaging everything into a docker-save compatible .tar archive
"""

import gzip
import ipaddress
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ── Media types ───────────────────────────────────────────────────────────────
MT_MANIFEST_V2    = "application/vnd.docker.distribution.manifest.v2+json"
MT_MANIFEST_LIST  = "application/vnd.docker.distribution.manifest.list.v2+json"
MT_OCI_MANIFEST   = "application/vnd.oci.image.manifest.v1+json"
MT_OCI_INDEX      = "application/vnd.oci.image.index.v1+json"
MT_MANIFEST_V1    = "application/vnd.docker.distribution.manifest.v1+prettyjws"

DOCKER_HUB_REGISTRY = "registry-1.docker.io"
DOCKER_HUB_AUTH_URL = "https://auth.docker.io/token"


class DockerRegistryError(Exception):
    """Raised for all expected registry / validation errors."""


class DockerRegistry:
    """
    Docker Registry HTTP API v2 client.

    Thread-safe: each thread uses its own requests.Session via thread-local
    storage, enabling connection keep-alive per worker while staying safe
    when gunicorn runs with multiple threads.
    """

    _local = threading.local()

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.username = username or None
        self.password = password or None

    # ── Thread-local session ─────────────────────────────────────────────────
    @property
    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            s = requests.Session()
            s.headers["User-Agent"] = "docker-image-downloader/1.0"
            self._local.session = s
        return self._local.session

    # ── Image reference parsing ───────────────────────────────────────────────
    def parse_image(self, image: str) -> Tuple[str, str, str]:
        """
        Parse an image reference into (registry, repository, tag_or_digest).

        Examples
        --------
        ubuntu              → (registry-1.docker.io, library/ubuntu, latest)
        ubuntu:22.04        → (registry-1.docker.io, library/ubuntu, 22.04)
        user/app:1.0        → (registry-1.docker.io, user/app, 1.0)
        ghcr.io/u/img:tag   → (ghcr.io, u/img, tag)
        img@sha256:abc...   → (registry-1.docker.io, library/img, sha256:abc...)
        """
        image = image.strip()
        ref = "latest"

        # ── Digest reference ─────────────────────────────────────────────────
        if "@sha256:" in image:
            idx = image.index("@sha256:")
            ref = image[idx + 1:]        # sha256:...
            image = image[:idx]
        else:
            # ── Tag in last path component ───────────────────────────────────
            last_slash = image.rfind("/")
            last_part = image[last_slash + 1:]
            if ":" in last_part:
                colon = last_part.rindex(":")
                ref = last_part[colon + 1:]
                image = image[: last_slash + 1] + last_part[:colon]

        parts = image.split("/")

        # First component is a registry when it contains "." or ":" or is "localhost"
        if len(parts) >= 2 and (
            "." in parts[0] or ":" in parts[0] or parts[0] == "localhost"
        ):
            registry = parts[0]
            repo = "/".join(parts[1:])
        elif len(parts) == 1:
            # Short name: ubuntu → library/ubuntu on Docker Hub
            registry = DOCKER_HUB_REGISTRY
            repo = "library/" + parts[0]
        else:
            # user/image style on Docker Hub
            registry = DOCKER_HUB_REGISTRY
            repo = "/".join(parts)

        return registry, repo, ref

    # ── Input validation & SSRF protection ───────────────────────────────────
    def validate_image_name(self, image: str) -> None:
        """Reject empty, oversized, or malformed image references."""
        if not image:
            raise DockerRegistryError("image name cannot be empty")
        if len(image) > 256:
            raise DockerRegistryError(
                "image name too long (maximum 256 characters)"
            )
        # Allow only safe Docker image reference characters
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@\-]*$", image):
            raise DockerRegistryError(
                "invalid image name: contains invalid characters"
            )

    def _validate_registry_host(self, host: str) -> None:
        """
        Block private / loopback / link-local addresses and known metadata
        endpoints to prevent Server-Side Request Forgery (SSRF).
        """
        # Strip port
        clean = host.split(":")[0].lower()

        blocked_names = {"localhost", "metadata.google.internal", "metadata.internal"}
        if clean in blocked_names:
            raise DockerRegistryError(f"invalid registry: {host}")

        try:
            ip = ipaddress.ip_address(clean)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                raise DockerRegistryError(
                    f"invalid registry: private/reserved IP addresses are not allowed"
                )
        except ValueError:
            pass  # Not an IP address – that's fine

    # ── Authentication ────────────────────────────────────────────────────────
    def get_token(self, registry: str, repo: str) -> Optional[str]:
        """Obtain a Bearer token for the given registry + repository."""

        if registry in (DOCKER_HUB_REGISTRY, "docker.io"):
            params: Dict[str, str] = {
                "service": "registry.docker.io",
                "scope": f"repository:{repo}:pull",
            }
            kwargs = {}
            if self.username and self.password:
                kwargs["auth"] = (self.username, self.password)
            resp = self._session.get(
                DOCKER_HUB_AUTH_URL, params=params, timeout=30, **kwargs
            )
            resp.raise_for_status()
            return resp.json().get("token")

        # Generic registry: issue a probe request to get WWW-Authenticate
        probe = self._session.get(f"https://{registry}/v2/", timeout=30)
        if probe.status_code == 200:
            return None  # Anonymous access allowed

        if probe.status_code == 401:
            www_auth = probe.headers.get("WWW-Authenticate", "")
            if www_auth.startswith("Bearer "):
                challenge: Dict[str, str] = {}
                for piece in www_auth[7:].split(","):
                    if "=" in piece:
                        k, v = piece.strip().split("=", 1)
                        challenge[k.strip()] = v.strip().strip('"')

                realm = challenge.get("realm", "")
                if realm:
                    token_params: Dict[str, str] = {
                        "scope": f"repository:{repo}:pull"
                    }
                    if "service" in challenge:
                        token_params["service"] = challenge["service"]
                    resp = self._session.get(realm, params=token_params, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                    return data.get("token") or data.get("access_token")

        return None

    # ── Manifest helpers ──────────────────────────────────────────────────────
    def _auth_headers(self, token: Optional[str]) -> Dict[str, str]:
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def get_manifest(
        self,
        registry: str,
        repo: str,
        ref: str,
        token: Optional[str],
    ) -> Tuple[dict, str]:
        """
        Fetch a manifest and return (manifest_dict, content_type).
        Accepts manifest lists, OCI indices, and single-arch manifests.
        """
        url = f"https://{registry}/v2/{repo}/manifests/{ref}"
        headers = {
            "Accept": ", ".join(
                [MT_MANIFEST_LIST, MT_OCI_INDEX, MT_MANIFEST_V2, MT_OCI_MANIFEST, MT_MANIFEST_V1]
            ),
            **self._auth_headers(token),
        }

        resp = self._session.get(url, headers=headers, timeout=60)

        if resp.status_code == 401:
            raise DockerRegistryError(
                "authentication failed - image may be private or credentials are wrong"
            )
        if resp.status_code == 403:
            raise DockerRegistryError(
                "access denied - image may be private"
            )
        if resp.status_code == 404:
            raise DockerRegistryError("image not found")

        if not resp.ok:
            # Try to extract a registry error message
            try:
                errors = resp.json().get("errors", [])
                if errors:
                    code = errors[0].get("code", "")
                    msg = errors[0].get("message", resp.text[:200])
                    raise DockerRegistryError(
                        f"failed to get manifest: {resp.status_code} - {code} {msg}"
                    )
            except (ValueError, KeyError):
                pass
            raise DockerRegistryError(
                f"failed to get manifest: {resp.status_code} - {resp.text[:200]}"
            )

        return resp.json(), resp.headers.get("Content-Type", "")

    # ── Platform detection ────────────────────────────────────────────────────
    def get_platforms(self, image: str) -> List[Dict[str, str]]:
        """Return the list of platforms for a multi-arch image."""
        self.validate_image_name(image)
        registry, repo, ref = self.parse_image(image)
        self._validate_registry_host(registry)

        token = self.get_token(registry, repo)
        manifest, _ = self.get_manifest(registry, repo, ref, token)

        if "manifests" not in manifest:
            return []  # Single-arch image → no platform list

        return [
            {
                "os": e.get("platform", {}).get("os", "unknown"),
                "architecture": e.get("platform", {}).get("architecture", "unknown"),
                "variant": e.get("platform", {}).get("variant", ""),
                "digest": e.get("digest", ""),
            }
            for e in manifest["manifests"]
        ]

    # ── Manifest list resolution ──────────────────────────────────────────────
    def _resolve_manifest(
        self,
        manifest: dict,
        registry: str,
        repo: str,
        ref: str,
        token: Optional[str],
        os_filter: Optional[str],
        arch_filter: Optional[str],
        variant_filter: Optional[str],
    ) -> dict:
        """Select a single-platform manifest from a manifest list / OCI index."""
        if "manifests" not in manifest:
            return manifest  # Already a single-arch manifest

        candidates = []
        for entry in manifest["manifests"]:
            p = entry.get("platform", {})
            p_os = p.get("os", "")
            p_arch = p.get("architecture", "")
            p_variant = p.get("variant", "")

            # Skip synthetic / unknown entries
            if p_os in ("unknown", "") or p_arch in ("unknown", ""):
                continue
            if os_filter and p_os != os_filter:
                continue
            if arch_filter and p_arch != arch_filter:
                continue
            if variant_filter and p_variant != variant_filter:
                continue
            candidates.append(entry)

        if not candidates and (os_filter or arch_filter):
            platform_str = "/".join(
                filter(None, [os_filter, arch_filter, variant_filter])
            )
            raise DockerRegistryError(
                f"no manifest found for platform {platform_str}"
            )

        if not candidates:
            # Default: linux/amd64
            for entry in manifest["manifests"]:
                p = entry.get("platform", {})
                if p.get("os") == "linux" and p.get("architecture") == "amd64":
                    candidates.append(entry)
                    break

        if not candidates:
            # First non-unknown entry
            for entry in manifest["manifests"]:
                p = entry.get("platform", {})
                if p.get("os") not in ("unknown", "") and p.get("architecture") not in (
                    "unknown",
                    "",
                ):
                    candidates.append(entry)
                    break

        if not candidates and manifest["manifests"]:
            candidates.append(manifest["manifests"][0])

        if not candidates:
            raise DockerRegistryError("no suitable platform found in manifest list")

        resolved, _ = self.get_manifest(registry, repo, candidates[0]["digest"], token)
        return resolved

    # ── Blob download ─────────────────────────────────────────────────────────
    def _download_blob_to_file(
        self,
        registry: str,
        repo: str,
        digest: str,
        token: Optional[str],
        dest_path: str,
    ) -> None:
        """Stream a registry blob directly to *dest_path* (memory-efficient)."""
        url = f"https://{registry}/v2/{repo}/blobs/{digest}"
        headers = self._auth_headers(token)

        with self._session.get(
            url, headers=headers, stream=True, timeout=600
        ) as resp:
            if resp.status_code == 404:
                raise DockerRegistryError(
                    f"blob not found: {digest[:19]}..."
                )
            if not resp.ok:
                raise DockerRegistryError(
                    f"failed to download blob: {resp.status_code}"
                )
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65_536):
                    if chunk:
                        f.write(chunk)

    # ── Layer decompression ───────────────────────────────────────────────────
    @staticmethod
    def _is_gzip(path: str) -> bool:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"

    @staticmethod
    def _is_zstd(path: str) -> bool:
        with open(path, "rb") as f:
            return f.read(4) == b"\x28\xb5\x2f\xfd"

    def _decompress_layer(
        self, src_path: str, dest_path: str, media_type: str
    ) -> None:
        """Decompress a compressed layer blob to a plain tar file."""
        if "gzip" in media_type or self._is_gzip(src_path):
            with gzip.open(src_path, "rb") as gz_f, open(dest_path, "wb") as out_f:
                shutil.copyfileobj(gz_f, out_f, length=65_536)

        elif "zstd" in media_type or self._is_zstd(src_path):
            try:
                import zstandard as zstd  # type: ignore

                dctx = zstd.ZstdDecompressor()
                with open(src_path, "rb") as in_f, open(dest_path, "wb") as out_f:
                    dctx.copy_stream(in_f, out_f)
            except ImportError:
                raise DockerRegistryError(
                    'zstd-compressed image requires the "zstandard" Python package'
                )
        else:
            # Assume already uncompressed tar
            shutil.copy2(src_path, dest_path)

    # ── docker-save tar creation ──────────────────────────────────────────────
    @staticmethod
    def _format_repo_tag(image: str, ref: str) -> str:
        """
        Build a clean RepoTags entry for manifest.json.
        Strips registry prefixes and normalises library/ images.
        """
        tag = image
        if not any(c in image.split("/")[-1] for c in (":", "@")):
            tag = f"{image}:{ref}"

        # Remove well-known registry prefixes
        for prefix in (f"{DOCKER_HUB_REGISTRY}/", "docker.io/"):
            if tag.startswith(prefix):
                tag = tag[len(prefix):]

        # Strip library/ for official Docker Hub images
        if tag.startswith("library/"):
            tag = tag[8:]

        # Remove digest reference
        if "@sha256:" in tag:
            tag = tag.split("@")[0] + ":latest"

        return tag

    @staticmethod
    def _safe_filename(image: str) -> str:
        """Convert an image reference to a safe .tar filename."""
        name = image.split("/")[-1]
        name = re.sub(r"[^a-zA-Z0-9._\-]", "_", name)
        name = name.lstrip(".")[:100] or "image"
        return name + ".tar"

    # ── Public download entrypoint ────────────────────────────────────────────
    def download_image(
        self,
        image: str,
        os_filter: Optional[str] = None,
        arch_filter: Optional[str] = None,
        variant_filter: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        """
        Download a Docker image and package it in *docker save* format.

        Returns
        -------
        (tar_path, filename, tmpdir)
            ``tmpdir`` must be deleted by the caller once the file has been
            served to the client.
        """
        self.validate_image_name(image)
        registry, repo, ref = self.parse_image(image)
        self._validate_registry_host(registry)

        logger.info("Downloading %s from %s (repo=%s, ref=%s)", image, registry, repo, ref)

        token = self.get_token(registry, repo)
        manifest, _ = self.get_manifest(registry, repo, ref, token)

        # Resolve manifest list → single-platform manifest
        manifest = self._resolve_manifest(
            manifest, registry, repo, ref, token,
            os_filter, arch_filter, variant_filter,
        )

        config_digest: str = manifest["config"]["digest"]
        layers: list = manifest["layers"]

        tmpdir = tempfile.mkdtemp(prefix="docker_dl_")
        try:
            # ── Config blob ──────────────────────────────────────────────────
            config_hash = config_digest.replace("sha256:", "")
            config_raw = os.path.join(tmpdir, "config_raw")
            self._download_blob_to_file(registry, repo, config_digest, token, config_raw)
            config_filename = f"{config_hash}.json"
            config_path = os.path.join(tmpdir, config_filename)
            os.rename(config_raw, config_path)
            logger.info("  config: %s", config_filename)

            # ── Layer blobs ──────────────────────────────────────────────────
            layer_entries: List[Tuple[str, str]] = []  # (local_path, arcname)

            for idx, layer in enumerate(layers):
                layer_digest: str = layer["digest"]
                layer_hash = layer_digest.replace("sha256:", "")
                media_type: str = layer.get("mediaType", "")
                layer_size: int = layer.get("size", 0)

                logger.info(
                    "  layer %d/%d: %s... (%s bytes)",
                    idx + 1, len(layers), layer_hash[:12], layer_size,
                )

                layer_dir = os.path.join(tmpdir, layer_hash)
                os.makedirs(layer_dir, exist_ok=True)

                compressed_path = os.path.join(layer_dir, "blob")
                self._download_blob_to_file(
                    registry, repo, layer_digest, token, compressed_path
                )

                layer_tar_path = os.path.join(layer_dir, "layer.tar")
                self._decompress_layer(compressed_path, layer_tar_path, media_type)
                os.unlink(compressed_path)  # free disk space immediately

                arcname = f"{layer_hash}/layer.tar"
                layer_entries.append((layer_tar_path, arcname))

            # ── manifest.json ────────────────────────────────────────────────
            repo_tag = self._format_repo_tag(image, ref)
            manifest_data = json.dumps(
                [
                    {
                        "Config": config_filename,
                        "RepoTags": [repo_tag],
                        "Layers": [arc for _, arc in layer_entries],
                    }
                ]
            )
            manifest_path = os.path.join(tmpdir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as f:
                f.write(manifest_data)

            # ── Build the tar archive ─────────────────────────────────────────
            filename = self._safe_filename(image)
            tar_path = os.path.join(tmpdir, filename)

            with tarfile.open(tar_path, "w") as tar:
                tar.add(manifest_path, arcname="manifest.json")
                tar.add(config_path, arcname=config_filename)
                for layer_path, arcname in layer_entries:
                    tar.add(layer_path, arcname=arcname)

            logger.info(
                "Archive ready: %s (%.1f MB)",
                filename,
                os.path.getsize(tar_path) / 1_048_576,
            )
            return tar_path, filename, tmpdir

        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
