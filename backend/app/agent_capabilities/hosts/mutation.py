"""Atomic write, patch, and delete hosts for B06P-A."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable

from ..types import CapabilityName, DenyCode, PermissionRight
from .common import HostContext, HostResult, denied, failed, receipt_for, succeeded, target_digest
from .file import FileReadHost
from .paths import PathHostError, PathVerifier, VerifiedPath


class AtomicMutationHost:
    """Commit filesystem mutations only after a second grant and identity check."""

    def __init__(self, *, before_commit: Callable[[], None] | None = None) -> None:
        self.before_commit = before_commit

    @staticmethod
    def _root(context: HostContext, root_id: str) -> PathVerifier | None:
        root = next((item for item in context.grant.workspace_roots if item.root_id == root_id), None)
        return PathVerifier(root) if root is not None else None

    def _prepare(
        self,
        context: HostContext,
        *,
        operation: str,
        capability: CapabilityName,
        path: str,
        root_id: str,
        allow_missing: bool,
        right: PermissionRight,
    ) -> tuple[PathVerifier | None, VerifiedPath | None, HostResult[object] | None]:
        verifier = self._root(context, root_id)
        if verifier is None:
            return None, None, denied(context, operation=operation, capability=capability.value, code=DenyCode.TOOL_PATH_OUTSIDE_WORKSPACE)
        try:
            verified = verifier.verify(path, allow_missing=allow_missing)
        except PathHostError as exc:
            return None, None, denied(context, operation=operation, capability=capability.value, code=exc.code, metadata={"target_digest": target_digest(path)})
        decision = context.authorize(
            context.path_request(
                capability=capability,
                path=verified.path,
                root_id=root_id,
                right=right,
                host_verified=True,
                observed_identity=verified.root_identity,
            )
        )
        if not decision.allowed:
            from .common import receipt_for

            return None, None, HostResult(None, decision, receipt_for(context, operation=operation, decision=decision, result="denied", metadata={"target_digest": target_digest(verified.path)}))
        return verifier, verified, None

    @staticmethod
    def _same_target(verifier: PathVerifier, expected: VerifiedPath) -> VerifiedPath:
        current = verifier.verify(expected.path, allow_missing=True)
        if current.target_identity != expected.target_identity or current.exists != expected.exists:
            raise PathHostError(DenyCode.TOOL_PATH_IDENTITY_CHANGED, "target identity changed before commit")
        return current

    @staticmethod
    def _fsync_parent(path: str) -> None:
        if os.name != "posix":
            return
        fd = os.open(os.path.dirname(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write_bytes(self, context: HostContext, path: str, data: bytes, *, root_id: str) -> HostResult[None]:
        if not isinstance(data, bytes):
            return denied(context, operation="file.write", capability=CapabilityName.PATH_WRITE.value, code=DenyCode.TOOL_PATH_AMBIGUOUS)  # type: ignore[return-value]
        verifier, expected, failure = self._prepare(
            context,
            operation="file.write",
            capability=CapabilityName.PATH_WRITE,
            path=path,
            root_id=root_id,
            allow_missing=True,
            right=PermissionRight.WRITE,
        )
        if failure is not None:
            return failure  # type: ignore[return-value]
        assert verifier is not None and expected is not None
        parent = os.path.dirname(expected.path)
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".echodesk-b06p-", dir=parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if self.before_commit is not None:
                self.before_commit()
            current_decision = context.authorize(
                context.path_request(
                    capability=CapabilityName.PATH_WRITE,
                    path=expected.path,
                    root_id=root_id,
                    right=PermissionRight.WRITE,
                    host_verified=True,
                    observed_identity=verifier.verify(expected.path, allow_missing=True).root_identity,
                )
            )
            if not current_decision.allowed:
                return HostResult(None, current_decision, receipt_for(context, operation="file.write", decision=current_decision, result="denied"))  # type: ignore[return-value]
            self._same_target(verifier, expected)
            os.replace(temp_path, expected.path)
            temp_path = None
            self._fsync_parent(expected.path)
        except PathHostError as exc:
            decision = context.authorize(
                context.path_request(
                    capability=CapabilityName.PATH_WRITE,
                    path=expected.path,
                    root_id=root_id,
                    right=PermissionRight.WRITE,
                    host_verified=True,
                    observed_identity=expected.root_identity,
                )
            )
            denied_decision = decision.model_copy(update={"outcome": "deny", "code": exc.code})
            return HostResult(None, denied_decision, receipt_for(context, operation="file.write", decision=denied_decision, result="denied"))  # type: ignore[return-value]
        except OSError:
            decision = context.authorize(
                context.path_request(
                    capability=CapabilityName.PATH_WRITE,
                    path=expected.path,
                    root_id=root_id,
                    right=PermissionRight.WRITE,
                    host_verified=True,
                    observed_identity=expected.root_identity,
                )
            )
            return failed(context, operation="file.write", decision=decision, error_code="HOST_IO_FAILED")  # type: ignore[return-value]
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
        return succeeded(context, operation="file.write", decision=current_decision, value=None, metadata={"target_digest": target_digest(expected.path), "bytes": len(data)})

    def write_text(self, context: HostContext, path: str, text: str, *, root_id: str, encoding: str = "utf-8") -> HostResult[None]:
        try:
            data = text.encode(encoding)
        except UnicodeEncodeError:
            return denied(context, operation="file.write", capability=CapabilityName.PATH_WRITE.value, code=DenyCode.TOOL_PATH_AMBIGUOUS)  # type: ignore[return-value]
        return self.write_bytes(context, path, data, root_id=root_id)

    def patch_text(
        self,
        context: HostContext,
        path: str,
        expected_text: str,
        replacement: str,
        *,
        root_id: str,
        encoding: str = "utf-8",
    ) -> HostResult[None]:
        reader = FileReadHost(max_bytes=8 * 1024 * 1024)
        current = reader.read_text(context, path, root_id=root_id, encoding=encoding)
        if not current.ok or current.value is None:
            return current  # type: ignore[return-value]
        if current.value.count(expected_text) != 1:
            return denied(context, operation="file.patch", capability=CapabilityName.PATH_WRITE.value, code=DenyCode.TOOL_PATH_AMBIGUOUS)  # type: ignore[return-value]
        return self.write_text(context, path, current.value.replace(expected_text, replacement, 1), root_id=root_id, encoding=encoding)

    def delete(self, context: HostContext, path: str, *, root_id: str) -> HostResult[None]:
        verifier, expected, failure = self._prepare(
            context,
            operation="file.delete",
            capability=CapabilityName.PATH_DELETE,
            path=path,
            root_id=root_id,
            allow_missing=False,
            right=PermissionRight.DELETE,
        )
        if failure is not None:
            return failure  # type: ignore[return-value]
        assert verifier is not None and expected is not None
        if expected.is_directory:
            return denied(context, operation="file.delete", capability=CapabilityName.PATH_DELETE.value, code=DenyCode.TOOL_PATH_AMBIGUOUS)  # type: ignore[return-value]
        try:
            if self.before_commit is not None:
                self.before_commit()
            decision = context.authorize(
                context.path_request(
                    capability=CapabilityName.PATH_DELETE,
                    path=expected.path,
                    root_id=root_id,
                    right=PermissionRight.DELETE,
                    host_verified=True,
                    observed_identity=verifier.verify(expected.path).root_identity,
                )
            )
            if not decision.allowed:
                return HostResult(None, decision, receipt_for(context, operation="file.delete", decision=decision, result="denied"))  # type: ignore[return-value]
            self._same_target(verifier, expected)
            os.unlink(expected.path)
        except PathHostError as exc:
            decision = context.authorize(
                context.path_request(
                    capability=CapabilityName.PATH_DELETE,
                    path=expected.path,
                    root_id=root_id,
                    right=PermissionRight.DELETE,
                    host_verified=True,
                    observed_identity=expected.root_identity,
                )
            )
            decision = decision.model_copy(update={"outcome": "deny", "code": exc.code})
            return HostResult(None, decision, receipt_for(context, operation="file.delete", decision=decision, result="denied"))  # type: ignore[return-value]
        except OSError:
            return failed(context, operation="file.delete", decision=decision, error_code="HOST_IO_FAILED")  # type: ignore[return-value]
        self._fsync_parent(expected.path)
        return succeeded(context, operation="file.delete", decision=decision, value=None, metadata={"target_digest": target_digest(expected.path)})


__all__ = ["AtomicMutationHost"]
