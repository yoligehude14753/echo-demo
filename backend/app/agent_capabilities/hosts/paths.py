"""Canonical filesystem path and reparse-point verification for B06P-A."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ..types import DenyCode, WorkspaceCapability

_REPARSE_POINT = 0x0400


class PathHostError(ValueError):
    def __init__(self, code: DenyCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VerifiedPath:
    path: str
    root_id: str
    root_identity: str
    target_identity: str | None
    exists: bool
    is_directory: bool


class PathVerifier:
    """Verify lexical containment and physical identity immediately at the host edge."""

    def __init__(self, root: WorkspaceCapability) -> None:
        self.root = root
        self.root_path = os.path.abspath(os.path.normpath(root.canonical_path))
        if not os.path.isabs(self.root_path):
            raise PathHostError(DenyCode.TOOL_PATH_AMBIGUOUS, "workspace root must be absolute")

    @staticmethod
    def identity_for(path: str | os.PathLike[str]) -> str:
        info = os.stat(path, follow_symlinks=False)
        return f"{info.st_dev:x}:{info.st_ino:x}"

    @staticmethod
    def _is_reparse(info: os.stat_result) -> bool:
        return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)

    @classmethod
    def _check_entry(cls, path: str, *, allow_missing: bool = False) -> os.stat_result | None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise PathHostError(DenyCode.TOOL_PATH_AMBIGUOUS, "path does not exist") from None
        except OSError as exc:
            raise PathHostError(
                DenyCode.TOOL_PATH_AMBIGUOUS, "path identity cannot be inspected"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise PathHostError(
                DenyCode.TOOL_PATH_AMBIGUOUS, "symlink path component is not allowed"
            )
        if cls._is_reparse(info):
            raise PathHostError(
                DenyCode.TOOL_PATH_AMBIGUOUS, "reparse path component is not allowed"
            )
        return info

    def _check_components(self, candidate: str, *, allow_missing: bool) -> os.stat_result | None:
        root_info = self._check_entry(self.root_path)
        if root_info is None or not stat.S_ISDIR(root_info.st_mode):
            raise PathHostError(DenyCode.TOOL_PATH_AMBIGUOUS, "workspace root is not a directory")
        try:
            relative = os.path.relpath(candidate, self.root_path)
        except ValueError as exc:
            raise PathHostError(
                DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE, "path has a different anchor"
            ) from exc
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            raise PathHostError(DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE, "path is outside workspace")
        current = self.root_path
        parts = [] if relative == os.curdir else relative.split(os.sep)
        for index, part in enumerate(parts):
            if part in {"", os.curdir, os.pardir}:
                raise PathHostError(
                    DenyCode.TOOL_PATH_AMBIGUOUS, "path contains ambiguous components"
                )
            current = os.path.join(current, part)
            info = self._check_entry(
                current, allow_missing=allow_missing and index == len(parts) - 1
            )
            if info is None:
                return None
        return self._check_entry(candidate, allow_missing=allow_missing)

    def verify(self, path: str | os.PathLike[str], *, allow_missing: bool = False) -> VerifiedPath:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise PathHostError(DenyCode.TOOL_PATH_AMBIGUOUS, "path is not a valid string")
        if not os.path.isabs(raw) or any(part in {"..", "~"} for part in Path(raw).parts):
            raise PathHostError(DenyCode.TOOL_PATH_AMBIGUOUS, "path must be canonical and absolute")
        candidate = os.path.abspath(os.path.normpath(raw))
        try:
            if os.path.commonpath((self.root_path, candidate)) != self.root_path:
                raise PathHostError(
                    DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE, "path is outside workspace"
                )
        except ValueError as exc:
            raise PathHostError(
                DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE, "path has a different anchor"
            ) from exc
        root_info = self._check_entry(self.root_path)
        assert root_info is not None
        actual_root_identity = f"{root_info.st_dev:x}:{root_info.st_ino:x}"
        if actual_root_identity != self.root.identity:
            raise PathHostError(DenyCode.TOOL_PATH_IDENTITY_CHANGED, "workspace identity changed")
        target_info = self._check_components(candidate, allow_missing=allow_missing)
        try:
            if os.path.commonpath((self.root_path, os.path.realpath(candidate))) != self.root_path:
                raise PathHostError(
                    DenyCode.TOOL_PATH_IDENTITY_CHANGED, "real path escaped workspace"
                )
        except ValueError as exc:
            raise PathHostError(
                DenyCode.TOOL_PATH_IDENTITY_CHANGED, "real path has a different anchor"
            ) from exc
        return VerifiedPath(
            path=candidate,
            root_id=self.root.root_id,
            root_identity=actual_root_identity,
            target_identity=(
                f"{target_info.st_dev:x}:{target_info.st_ino:x}"
                if target_info is not None
                else None
            ),
            exists=target_info is not None,
            is_directory=bool(target_info is not None and stat.S_ISDIR(target_info.st_mode)),
        )


__all__ = ["PathHostError", "PathVerifier", "VerifiedPath"]
