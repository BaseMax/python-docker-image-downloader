"""
Bearer-token authentication for Docker Hub and generic OCI registries.

Functions are stateless helpers that accept a ``requests.Session`` so that
the caller controls connection pooling and threading.
"""

from typing import Dict, Optional

import requests

from .constants import DOCKER_HUB_AUTH_URL, DOCKER_HUB_REGISTRY
from .exceptions import DockerRegistryError


def auth_headers(token: Optional[str]) -> Dict[str, str]:
    """Return the ``Authorization`` header dict for a Bearer token, or empty."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def get_token(
    session: requests.Session,
    registry: str,
    repo: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Optional[str]:
    """
    Obtain a pull-scoped Bearer token for *registry* / *repo*.

    Returns ``None`` when the registry allows anonymous access without a token.
    """
    if registry in (DOCKER_HUB_REGISTRY, "docker.io"):
        return _docker_hub_token(session, repo, username, password)

    return _generic_token(session, registry, repo)


def _docker_hub_token(
    session: requests.Session,
    repo: str,
    username: Optional[str],
    password: Optional[str],
) -> str:
    params: Dict[str, str] = {
        "service": "registry.docker.io",
        "scope": f"repository:{repo}:pull",
    }
    kwargs = {}
    if username and password:
        kwargs["auth"] = (username, password)

    resp = session.get(DOCKER_HUB_AUTH_URL, params=params, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp.json()["token"]


def _generic_token(
    session: requests.Session,
    registry: str,
    repo: str,
) -> Optional[str]:
    """Probe the registry's /v2/ endpoint and follow the WWW-Authenticate challenge."""
    probe = session.get(f"https://{registry}/v2/", timeout=30)

    if probe.status_code == 200:
        return None  # anonymous access is allowed

    if probe.status_code != 401:
        raise DockerRegistryError(
            f"authentication failed: registry returned {probe.status_code}"
        )

    www_auth = probe.headers.get("WWW-Authenticate", "")
    if not www_auth.startswith("Bearer "):
        raise DockerRegistryError(
            "authentication failed: no WWW-Authenticate Bearer challenge"
        )

    challenge = _parse_bearer_challenge(www_auth[7:])
    realm = challenge.get("realm", "")
    if not realm:
        return None

    params = {"scope": f"repository:{repo}:pull"}
    if "service" in challenge:
        params["service"] = challenge["service"]

    resp = session.get(realm, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("token") or data.get("access_token")


def _parse_bearer_challenge(challenge_str: str) -> Dict[str, str]:
    """Parse a Bearer challenge string into a ``{key: value}`` dict."""
    result: Dict[str, str] = {}
    for piece in challenge_str.split(","):
        if "=" in piece:
            k, v = piece.strip().split("=", 1)
            result[k.strip()] = v.strip().strip('"')
    return result
