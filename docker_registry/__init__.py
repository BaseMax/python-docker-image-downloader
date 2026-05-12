"""docker_registry, Docker Registry HTTP API v2 client package."""

from .client import DockerRegistry
from .exceptions import DockerRegistryError

__all__ = ["DockerRegistry", "DockerRegistryError"]
