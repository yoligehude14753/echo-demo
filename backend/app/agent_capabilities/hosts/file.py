"""Read-only file, glob, and grep capability hosts."""

from __future__ import annotations

import glob as glob_module
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..types import CapabilityName, DenyCode, PermissionRight
from .common import HostContext, HostResult, denied, failed, receipt_for, succeeded, target_digest
from .paths import PathHostError, PathVerifier, VerifiedPath


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line_number: int
    line: str


class FileReadHost:
    """Perform only verified reads; no HOME/PATH discovery or implicit roots."""

    def __init__(self, *, max_bytes: int = 1_048_576, max_matches: int = 10_000) -> None:
        if max_bytes < 1 or max_matches < 1:
            raise ValueError("read limits must be positive")
        self.max_bytes = max_bytes
        self.max_matches = max_matches

    @staticmethod
    def _root(context: HostContext, root_id: str) -> PathVerifier | None:
        root = next((item for item in context.grant.workspace_roots if item.root_id == root_id), None)
        return PathVerifier(root) if root is not None else None

    def _verify(
        self,
        context: HostContext,
        *,
        operation: str,
        capability: CapabilityName,
        path: str,
        root_id: str,
        allow_missing: bool = False,
    ) -> tuple[VerifiedPath | None, HostResult[object] | None]:
        verifier = self._root(context, root_id)
        if verifier is None:
            return None, denied(
                context,
                operation=operation,
                capability=capability.value,
                code=DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE,
            )
        try:
            verified = verifier.verify(path, allow_missing=allow_missing)
        except PathHostError as exc:
            return None, denied(
                context,
                operation=operation,
                capability=capability.value,
                code=exc.code,
                metadata={"target_digest": target_digest(path)},
            )
        decision = context.authorize(
            context.path_request(
                capability=capability,
                path=verified.path,
                root_id=root_id,
                right=PermissionRight.READ,
                host_verified=True,
                observed_identity=verified.root_identity,
            )
        )
        if not decision.allowed:
            return None, HostResult(
                None,
                decision,
                receipt_for(
                    context,
                    operation=operation,
                    decision=decision,
                    result="denied",
                    metadata={"target_digest": target_digest(verified.path)},
                ),
            )
        return verified, None

    def read_bytes(self, context: HostContext, path: str, *, root_id: str) -> HostResult[bytes]:
        verified, failure = self._verify(
            context,
            operation="file.read",
            capability=CapabilityName.PATH_READ,
            path=path,
            root_id=root_id,
        )
        if failure is not None:
            return failure  # type: ignore[return-value]
        assert verified is not None
        if not verified.exists or verified.is_directory:
            return denied(
                context,
                operation="file.read",
                capability=CapabilityName.PATH_READ.value,
                code=DenyCode.TOOL_PATH_AMBIGUOUS,
            )  # type: ignore[return-value]
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(verified.path, flags)
            try:
                current = os.fstat(fd)
                current_identity = f"{current.st_dev:x}:{current.st_ino:x}"
                if current_identity != verified.target_identity:
                    return denied(
                        context,
                        operation="file.read",
                        capability=CapabilityName.PATH_READ.value,
                        code=DenyCode.TOOL_PATH_IDENTITY_CHANGED,
                    )  # type: ignore[return-value]
                data = os.read(fd, self.max_bytes + 1)
            finally:
                os.close(fd)
            if len(data) > self.max_bytes:
                return denied(
                    context,
                    operation="file.read",
                    capability=CapabilityName.PATH_READ.value,
                    code=DenyCode.TOOL_BUDGET_EXCEEDED,
                )  # type: ignore[return-value]
        except OSError:
            decision = context.authorize(
                context.path_request(
                    capability=CapabilityName.PATH_READ,
                    path=verified.path,
                    root_id=root_id,
                    right=PermissionRight.READ,
                    host_verified=True,
                    observed_identity=verified.root_identity,
                )
            )
            return failed(
                context,
                operation="file.read",
                decision=decision,
                error_code="HOST_IO_FAILED",
                metadata={"target_digest": target_digest(verified.path)},
            )  # type: ignore[return-value]
        decision = context.authorize(
            context.path_request(
                capability=CapabilityName.PATH_READ,
                path=verified.path,
                root_id=root_id,
                right=PermissionRight.READ,
                host_verified=True,
                observed_identity=verified.root_identity,
            )
        )
        if not decision.allowed:
            return HostResult(
                None,
                decision,
                receipt_for(
                    context, operation="file.read", decision=decision, result="denied"
                ),
            )  # type: ignore[return-value]
        return succeeded(
            context,
            operation="file.read",
            decision=decision,
            value=data,
            metadata={"target_digest": target_digest(verified.path), "bytes": len(data)},
        )

    def read_text(self, context: HostContext, path: str, *, root_id: str, encoding: str = "utf-8") -> HostResult[str]:
        result = self.read_bytes(context, path, root_id=root_id)
        if not result.ok or result.value is None:
            return result  # type: ignore[return-value]
        try:
            return HostResult(result.value.decode(encoding), result.decision, result.receipt)
        except UnicodeDecodeError:
            return failed(
                context,
                operation="file.read",
                decision=result.decision,
                error_code="HOST_TEXT_DECODE_FAILED",
                metadata={"target_digest": target_digest(path)},
            )  # type: ignore[return-value]

    def glob(self, context: HostContext, pattern: str, *, root_id: str) -> HostResult[tuple[str, ...]]:
        verifier = self._root(context, root_id)
        if verifier is None:
            return denied(context, operation="file.glob", capability=CapabilityName.PATH_READ.value, code=DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE)  # type: ignore[return-value]
        if not pattern or "\x00" in pattern or os.path.isabs(pattern) or any(part == ".." for part in Path(pattern).parts):
            return denied(context, operation="file.glob", capability=CapabilityName.PATH_READ.value, code=DenyCode.TOOL_PATH_AMBIGUOUS)  # type: ignore[return-value]
        try:
            root = verifier.verify(verifier.root_path)
            decision = context.authorize(
                context.path_request(
                    capability=CapabilityName.PATH_READ,
                    path=root.path,
                    root_id=root_id,
                    right=PermissionRight.READ,
                    host_verified=True,
                    observed_identity=root.root_identity,
                )
            )
            if not decision.allowed:
                return HostResult(None, decision, receipt_for(context, operation="file.glob", decision=decision, result="denied"))  # type: ignore[return-value]
            matches: list[str] = []
            for candidate in glob_module.iglob(os.path.join(verifier.root_path, pattern), recursive=True):
                verified = verifier.verify(candidate)
                if verified.is_directory:
                    continue
                matches.append(verified.path)
                if len(matches) > self.max_matches:
                    return denied(context, operation="file.glob", capability=CapabilityName.PATH_READ.value, code=DenyCode.TOOL_BUDGET_EXCEEDED)  # type: ignore[return-value]
        except PathHostError as exc:
            return denied(context, operation="file.glob", capability=CapabilityName.PATH_READ.value, code=exc.code)  # type: ignore[return-value]
        return succeeded(
            context,
            operation="file.glob",
            decision=decision,
            value=tuple(sorted(matches)),
            metadata={"match_count": len(matches), "pattern_digest": target_digest(pattern)},
        )

    def grep(
        self,
        context: HostContext,
        pattern: str,
        paths: Iterable[str],
        *,
        root_id: str,
    ) -> HostResult[tuple[GrepMatch, ...]]:
        try:
            compiled = re.compile(pattern)
        except re.error:
            return denied(context, operation="file.grep", capability=CapabilityName.PATH_READ.value, code=DenyCode.TOOL_PATH_AMBIGUOUS)  # type: ignore[return-value]
        verified_paths: list[VerifiedPath] = []
        for path in paths:
            verified, failure = self._verify(context, operation="file.grep", capability=CapabilityName.PATH_READ, path=path, root_id=root_id)
            if failure is not None:
                return failure  # type: ignore[return-value]
            assert verified is not None
            if verified.is_directory:
                return denied(context, operation="file.grep", capability=CapabilityName.PATH_READ.value, code=DenyCode.TOOL_PATH_AMBIGUOUS)  # type: ignore[return-value]
            verified_paths.append(verified)
        if not verified_paths:
            return denied(context, operation="file.grep", capability=CapabilityName.PATH_READ.value, code=DenyCode.TOOL_PATH_AMBIGUOUS)  # type: ignore[return-value]
        decision = context.authorize(
            context.path_request(
                capability=CapabilityName.PATH_READ,
                path=verified_paths[0].path if verified_paths else self._root(context, root_id).root_path,  # type: ignore[union-attr]
                root_id=root_id,
                right=PermissionRight.READ,
                host_verified=True,
                observed_identity=verified_paths[0].root_identity if verified_paths else self._root(context, root_id).identity,  # type: ignore[union-attr]
            )
        )
        if not decision.allowed:
            return HostResult(None, decision, receipt_for(context, operation="file.grep", decision=decision, result="denied"))  # type: ignore[return-value]
        matches: list[GrepMatch] = []
        try:
            for verified in verified_paths:
                with open(verified.path, encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if compiled.search(line):
                            matches.append(GrepMatch(verified.path, line_number, line.rstrip("\n")))
                            if len(matches) > self.max_matches:
                                return denied(context, operation="file.grep", capability=CapabilityName.PATH_READ.value, code=DenyCode.TOOL_BUDGET_EXCEEDED)  # type: ignore[return-value]
        except (OSError, UnicodeError):
            return failed(context, operation="file.grep", decision=decision, error_code="HOST_IO_FAILED")  # type: ignore[return-value]
        return succeeded(
            context,
            operation="file.grep",
            decision=decision,
            value=tuple(matches),
            metadata={"match_count": len(matches), "pattern_digest": target_digest(pattern)},
        )


__all__ = ["FileReadHost", "GrepMatch"]
