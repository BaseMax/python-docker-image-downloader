"""
Manifest fetching and platform resolution.

Supports:
  - Docker manifest v2 (single-arch)
  - Docker manifest list v2 (multi-arch)
  - OCI image manifest v1
  - OCI image index v1
"""

from typing import Dict, List, Optional, Tuple

import requests

from .auth import auth_headers
from .constants import MANIFEST_ACCEPT
from .exceptions import DockerRegistryError


def fetch_manifest(
    session: requests.Session,
    registry: str,
    repo: str,
    ref: str,
    token: Optional[str],
) -> Tuple[dict, str]:
    """
    Fetch a manifest by tag or digest.

    Returns ``(manifest_dict, content_type)``.
    """
    url = f"https://{registry}/v2/{repo}/manifests/{ref}"
    headers = {"Accept": MANIFEST_ACCEPT, **auth_headers(token)}

    resp = session.get(url, headers=headers, timeout=60)
    _raise_for_manifest_status(resp)

    return resp.json(), resp.headers.get("Content-Type", "")


def get_platforms(
    session: requests.Session,
    registry: str,
    repo: str,
    ref: str,
    token: Optional[str],
) -> List[Dict[str, str]]:
    """
    Return the list of platforms advertised in a manifest list / OCI index.
    Returns an empty list for single-arch manifests.
    """
    manifest, _ = fetch_manifest(session, registry, repo, ref, token)

    if "manifests" not in manifest:
        return []

    return [
        {
            "os": entry.get("platform", {}).get("os", "unknown"),
            "architecture": entry.get("platform", {}).get("architecture", "unknown"),
            "variant": entry.get("platform", {}).get("variant", ""),
            "digest": entry.get("digest", ""),
        }
        for entry in manifest["manifests"]
    ]


def resolve_manifest(
    session: requests.Session,
    registry: str,
    repo: str,
    ref: str,
    token: Optional[str],
    manifest: dict,
    os_filter: Optional[str],
    arch_filter: Optional[str],
    variant_filter: Optional[str],
) -> dict:
    """
    Given a manifest (which may be a manifest list or an OCI index), return a
    single-platform manifest suitable for downloading layers.

    Platform selection priority:
    1. Exact match on all provided filters.
    2. Default to ``linux/amd64`` when no filters are given.
    3. First non-unknown entry.
    4. Absolute first entry (fallback).
    """
    if "manifests" not in manifest:
        return manifest  # already single-arch

    candidates = _filter_candidates(
        manifest["manifests"], os_filter, arch_filter, variant_filter
    )

    if not candidates and (os_filter or arch_filter):
        platform_str = "/".join(filter(None, [os_filter, arch_filter, variant_filter]))
        raise DockerRegistryError(f"no manifest found for platform {platform_str}")

    if not candidates:
        candidates = _default_platform_candidates(manifest["manifests"])

    if not candidates:
        raise DockerRegistryError("no suitable platform found in manifest list")

    resolved, _ = fetch_manifest(session, registry, repo, candidates[0]["digest"], token)
    return resolved


# ── Private helpers ───────────────────────────────────────────────────────────

def _filter_candidates(
    entries: list,
    os_filter: Optional[str],
    arch_filter: Optional[str],
    variant_filter: Optional[str],
) -> list:
    result = []
    for entry in entries:
        p = entry.get("platform", {})
        if p.get("os", "") in ("unknown", "") or p.get("architecture", "") in ("unknown", ""):
            continue
        if os_filter and p.get("os") != os_filter:
            continue
        if arch_filter and p.get("architecture") != arch_filter:
            continue
        if variant_filter and p.get("variant", "") != variant_filter:
            continue
        result.append(entry)
    return result


def _default_platform_candidates(entries: list) -> list:
    """Prefer linux/amd64, then first non-unknown, then absolute first."""
    for entry in entries:
        p = entry.get("platform", {})
        if p.get("os") == "linux" and p.get("architecture") == "amd64":
            return [entry]

    for entry in entries:
        p = entry.get("platform", {})
        if p.get("os") not in ("unknown", "") and p.get("architecture") not in ("unknown", ""):
            return [entry]

    return entries[:1]


def _raise_for_manifest_status(resp: requests.Response) -> None:
    if resp.status_code == 401:
        raise DockerRegistryError(
            "authentication failed, image may be private or credentials are wrong"
        )
    if resp.status_code == 403:
        raise DockerRegistryError("access denied, image may be private")
    if resp.status_code == 404:
        raise DockerRegistryError("image not found")

    if not resp.ok:
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
