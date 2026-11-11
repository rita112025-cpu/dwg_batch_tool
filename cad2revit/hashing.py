"""File hashing and copying through the repo's existing helpers.

``convert_single.sha256`` and ``convert_single.copy_hashed`` are the same
chunked readers the DWG batch tools use before and after accoreconsole.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _convert_single():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    folder = str(scripts)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    import convert_single

    return convert_single


def file_sha256(path) -> str:
    """Return the SHA-256 hex digest of ``path``."""
    return _convert_single().sha256(path)


def copy_hashed(src, dest) -> str:
    """Copy ``src`` to ``dest`` and return the SHA-256 of the bytes that were read."""
    return _convert_single().copy_hashed(src, dest)
