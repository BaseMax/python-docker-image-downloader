"""Shared constants: media types and well-known URLs."""

DOCKER_HUB_REGISTRY = "registry-1.docker.io"
DOCKER_HUB_AUTH_URL = "https://auth.docker.io/token"

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v1+prettyjws",
])
