"""
Build a ``docker save``-compatible ``.tar`` archive from downloaded layers.

The format is::

    <archive>.tar
    ├── manifest.json
    ├── <config_hash>.json
    └── <layer_hash>/
        └── layer.tar
"""

import json
import os
import tarfile
from typing import List, Tuple


def build_archive(
    tmpdir: str,
    config_path: str,
    config_filename: str,
    layer_entries: List[Tuple[str, str]],
    repo_tag: str,
    filename: str,
) -> str:
    """
    Assemble a ``docker save`` tar archive and return its absolute path.

    Parameters
    ----------
    tmpdir:
        Writable scratch directory (already contains config + layer files).
    config_path:
        Absolute path to the image config JSON blob.
    config_filename:
        Filename the config should have inside the archive (``<hash>.json``).
    layer_entries:
        List of ``(local_path, archive_name)`` tuples for each layer tar.
    repo_tag:
        Value for the ``RepoTags`` field (e.g. ``ubuntu:24.04``).
    filename:
        Desired filename for the resulting tar (e.g. ``ubuntu_24.04.tar``).
    """
    manifest_path = _write_manifest_json(
        tmpdir, config_filename, layer_entries, repo_tag
    )

    tar_path = os.path.join(tmpdir, filename)
    with tarfile.open(tar_path, "w") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        tar.add(config_path, arcname=config_filename)
        for local_path, arcname in layer_entries:
            tar.add(local_path, arcname=arcname)

    return tar_path


def _write_manifest_json(
    tmpdir: str,
    config_filename: str,
    layer_entries: List[Tuple[str, str]],
    repo_tag: str,
) -> str:
    """Serialise *manifest.json* into *tmpdir* and return its path."""
    data = json.dumps([
        {
            "Config": config_filename,
            "RepoTags": [repo_tag],
            "Layers": [arcname for _, arcname in layer_entries],
        }
    ])
    path = os.path.join(tmpdir, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data)
    return path
