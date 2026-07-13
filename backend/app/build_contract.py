"""Immutable desktop/backend build handshake metadata.

The build identity is derived from the executable that is actually serving the
request.  Source runs use the Python and migration SQL bytes under ``app/``;
frozen runs use the complete executable bytes.  User configuration and process
environment therefore cannot relabel an older backend as the current build.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from app import __version__
from app.adapters.repo.migrator import migration_catalog_max_version

BACKEND_PRODUCT_ID = "com.echodesk.app.backend"
DESKTOP_API_CONTRACT = "echodesk.desktop-backend/v1"
BUILD_CONTRACT_SCHEMA_VERSION = 1
_SOURCE_SUFFIXES = frozenset({".py", ".sql"})
_HASH_CHUNK_BYTES = 1024 * 1024


def _update_file_digest(digest: object, path: Path) -> None:
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)  # type: ignore[attr-defined]


def _source_tree_build_id(root: Path) -> str:
    digest = sha256()
    files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.suffix in _SOURCE_SUFFIXES and path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        _update_file_digest(digest, path)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


@lru_cache(maxsize=1)
def runtime_build_id() -> str:
    if getattr(sys, "frozen", False):
        digest = sha256()
        _update_file_digest(digest, Path(sys.executable).resolve())
        return f"sha256:{digest.hexdigest()}"
    return _source_tree_build_id(Path(__file__).resolve().parent)


def backend_build_contract() -> dict[str, object]:
    """Return the immutable metadata required by the Electron supervisor."""

    return {
        "schema_version": BUILD_CONTRACT_SCHEMA_VERSION,
        "product_id": BACKEND_PRODUCT_ID,
        "product_version": __version__,
        "api_contract": DESKTOP_API_CONTRACT,
        "build_id": runtime_build_id(),
        "schema_catalog_max": migration_catalog_max_version(),
    }


__all__ = [
    "BACKEND_PRODUCT_ID",
    "BUILD_CONTRACT_SCHEMA_VERSION",
    "DESKTOP_API_CONTRACT",
    "backend_build_contract",
    "runtime_build_id",
]
