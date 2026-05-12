"""
Image reference parsing, input validation, and SSRF protection.

All functions are stateless and take plain strings; no I/O is performed.
"""

import ipaddress
import re
from typing import Tuple

from .constants import DOCKER_HUB_REGISTRY
from .exceptions import DockerRegistryError

# Characters allowed in Docker image references
_IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@\-]*$")


# ── Validation ────────────────────────────────────────────────────────────────

def validate_image_name(image: str) -> None:
    """Reject empty, oversized, or syntactically invalid image references."""
    if not image:
        raise DockerRegistryError("image name cannot be empty")
    if len(image) > 256:
        raise DockerRegistryError("image name too long (maximum 256 characters)")
    if not _IMAGE_RE.match(image):
        raise DockerRegistryError("invalid image name: contains invalid characters")


def validate_registry_host(host: str) -> None:
    """
    Prevent SSRF by blocking private / loopback / link-local IP addresses
    and well-known metadata service hostnames.
    """
    clean = host.split(":")[0].lower()

    _BLOCKED_NAMES = {"localhost", "metadata.google.internal", "metadata.internal"}
    if clean in _BLOCKED_NAMES:
        raise DockerRegistryError(f"invalid registry: {host!r} is not allowed")

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
                "invalid registry: private/reserved IP addresses are not allowed"
            )
    except ValueError:
        pass  # Not an IP — that is fine


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_image(image: str) -> Tuple[str, str, str]:
    """
    Parse an image reference into ``(registry, repository, tag_or_digest)``.

    Examples::

        ubuntu              → (registry-1.docker.io, library/ubuntu, latest)
        ubuntu:22.04        → (registry-1.docker.io, library/ubuntu, 22.04)
        user/app:1.0        → (registry-1.docker.io, user/app, 1.0)
        ghcr.io/u/img:tag   → (ghcr.io, u/img, tag)
        img@sha256:abc...   → (registry-1.docker.io, library/img, sha256:abc...)
    """
    image = image.strip()
    ref = "latest"

    if "@sha256:" in image:
        idx = image.index("@sha256:")
        ref = image[idx + 1:]   # "sha256:…"
        image = image[:idx]
    else:
        last_slash = image.rfind("/")
        last_part = image[last_slash + 1:]
        if ":" in last_part:
            colon = last_part.rindex(":")
            ref = last_part[colon + 1:]
            image = image[: last_slash + 1] + last_part[:colon]

    parts = image.split("/")

    if len(parts) >= 2 and (
        "." in parts[0] or ":" in parts[0] or parts[0] == "localhost"
    ):
        # explicit registry host
        registry = parts[0]
        repo = "/".join(parts[1:])
    elif len(parts) == 1:
        # short name  →  official Docker Hub image
        registry = DOCKER_HUB_REGISTRY
        repo = "library/" + parts[0]
    else:
        # user/image  →  Docker Hub user repository
        registry = DOCKER_HUB_REGISTRY
        repo = "/".join(parts)

    return registry, repo, ref


# ── Naming helpers ─────────────────────────────────────────────────────────────

def format_repo_tag(image: str, ref: str) -> str:
    """
    Build a clean ``RepoTags`` entry for *manifest.json* inside a docker-save
    archive.  Strips well-known registry prefixes and ``library/`` for official
    Docker Hub images.
    """
    tag = image if (":" in image.split("/")[-1] or "@" in image) else f"{image}:{ref}"

    for prefix in (f"{DOCKER_HUB_REGISTRY}/", "docker.io/"):
        if tag.startswith(prefix):
            tag = tag[len(prefix):]

    if tag.startswith("library/"):
        tag = tag[8:]

    if "@sha256:" in tag:
        tag = tag.split("@")[0] + ":latest"

    return tag


def safe_filename(image: str) -> str:
    """Convert an image reference to a filesystem-safe ``.tar`` filename."""
    name = image.split("/")[-1]
    name = re.sub(r"[^a-zA-Z0-9._\-]", "_", name)
    name = name.lstrip(".")[:100] or "image"
    return name + ".tar"
