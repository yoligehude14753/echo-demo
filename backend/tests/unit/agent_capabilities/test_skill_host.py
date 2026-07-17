"""Focused B06P-C contract/security harness for bundled EchoSkill hosts."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.agent_capabilities.skill_host import (
    SKILL_MANIFEST_SIGNATURE_INVALID,
    SKILL_RESOURCE_HASH_MISMATCH,
    UNSUPPORTED_P0_FAIL_CLOSED,
    EchoSkillHost,
    HmacSha256ManifestVerifier,
    SkillManifest,
    SkillResolver,
    SkillResource,
)
from app.agent_capabilities.types import (
    CapabilityName,
    CapabilityRequest,
    DenyCode,
    GrantInput,
    InvocationBinding,
    SkillCapability,
    SkillRequest,
    WorkspaceIdentity,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
KEY = b"b06p-test-signing-key"
IDENTITY = WorkspaceIdentity(workspace_id="ws-1", identity="bundle-root-1")


def _grant(*, task_id: str = "task-1", operation_key: str = "op-1", revision: int = 7):
    from app.agent_capabilities.catalog import freeze_grant

    return freeze_grant(
        GrantInput(
            grant_id="grant-1",
            revision=revision,
            policy_revision=11,
            task_id=task_id,
            operation_key=operation_key,
            workspace_identity=IDENTITY,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            skills=SkillCapability(
                mode="allowlist", identities=("bundled.echo",), versions=("1.0.0",)
            ),
        )
    )


def _manifest(
    content: bytes = b"prompt content\n", *, capabilities: tuple[str, ...] = ("skill.use",)
):
    resource = SkillResource(path="prompt.txt", sha256=hashlib.sha256(content).hexdigest())
    unsigned = SkillManifest(
        identity="bundled.echo",
        version="1.0.0",
        entrypoint="echo.handler",
        required_capabilities=capabilities,
        platforms=("macos", "windows", "linux"),
        resources=(resource,),
        provenance="bundle:echo-test",
        signer_id="echo-test-signer",
        signature="hmac-sha256:" + "00" * 32,
    )
    signature = hmac.new(KEY, unsigned.signed_payload(), hashlib.sha256).digest()
    encoded = base64.b16encode(signature).decode("ascii").lower()
    return unsigned.model_copy(update={"signature": "hmac-sha256:" + encoded})


def _request(
    grant,
    *,
    task_id: str | None = None,
    operation_key: str | None = None,
    skill: SkillRequest | None = None,
):
    return CapabilityRequest(
        capability=CapabilityName.SKILL_USE,
        binding=InvocationBinding(
            task_id=task_id or grant.task_id,
            operation_key=operation_key or grant.operation_key,
            workspace_identity=IDENTITY,
            policy_revision=grant.policy_revision,
        ),
        skill=skill or SkillRequest(identity="bundled.echo", version="1.0.0"),
    )


def _host(tmp_path: Path, *, platform: str = "macos", handler=None):
    content = b"prompt content\n"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "prompt.txt").write_bytes(content)
    resolver = SkillResolver(
        bundle,
        HmacSha256ManifestVerifier({"echo-test-signer": KEY}),
        platform=platform,
    )
    calls: list[Mapping[str, Any]] = []

    def default_handler(payload, context):
        calls.append(payload)
        assert context.task_id == "task-1"
        return {"ok": True, "text": payload["text"]}

    return EchoSkillHost(
        resolver,
        {"echo.handler": handler or default_handler},
    ), calls


@pytest.mark.parametrize("platform", ("macos", "windows", "linux"))
def test_cross_platform_signed_hash_and_provenance_allow(tmp_path: Path, platform: str) -> None:
    host, calls = _host(tmp_path, platform=platform)
    grant = _grant()
    result = host.invoke(
        manifest=_manifest(),
        payload={"text": "hello", "secret": "do-not-log"},
        grant=grant,
        request=_request(grant),
        tool_use_id="tool-1",
        grant_revision=7,
        now=NOW,
    )

    assert result.ok
    assert result.value == {"ok": True, "text": "hello"}
    assert calls == [{"text": "hello", "secret": "do-not-log"}]
    assert result.receipt.skill_identity == "bundled.echo"
    assert result.receipt.provenance == "bundle:echo-test"
    assert result.receipt.manifest_sha256.startswith("sha256:")
    assert result.receipt.resource_hashes[0].startswith("sha256:")
    receipt_json = result.receipt.model_dump_json()
    assert "do-not-log" not in receipt_json
    assert '"redacted":true' in receipt_json


@pytest.mark.parametrize(
    ("label", "manifest", "expected"),
    (
        (
            "signature",
            _manifest().model_copy(update={"signature": "hmac-sha256:" + "00" * 32}),
            SKILL_MANIFEST_SIGNATURE_INVALID,
        ),
        ("p0 npm", _manifest(b"npm install attacker\n"), UNSUPPORTED_P0_FAIL_CLOSED),
        ("p0 home", _manifest(b"HOME=/tmp\n"), UNSUPPORTED_P0_FAIL_CLOSED),
        ("p0 hooks", _manifest(b"hooks/post-run\n"), UNSUPPORTED_P0_FAIL_CLOSED),
    ),
)
def test_manifest_security_matrix(
    tmp_path: Path, label: str, manifest: SkillManifest, expected: str
) -> None:
    del label
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "prompt.txt").write_bytes(
        b"npm install attacker\n"
        if expected == UNSUPPORTED_P0_FAIL_CLOSED
        and manifest.resources[0].sha256 == hashlib.sha256(b"npm install attacker\n").hexdigest()
        else b"prompt content\n"
    )
    if manifest.resources[0].sha256 == hashlib.sha256(b"HOME=/tmp\n").hexdigest():
        (bundle / "prompt.txt").write_bytes(b"HOME=/tmp\n")
    if manifest.resources[0].sha256 == hashlib.sha256(b"hooks/post-run\n").hexdigest():
        (bundle / "prompt.txt").write_bytes(b"hooks/post-run\n")
    host = EchoSkillHost(
        SkillResolver(
            bundle, HmacSha256ManifestVerifier({"echo-test-signer": KEY}), platform="macos"
        ),
        {"echo.handler": lambda payload, context: {"ok": True}},
    )
    grant = _grant()
    result = host.invoke(
        manifest=manifest,
        payload={"text": "hello"},
        grant=grant,
        request=_request(grant),
        tool_use_id="tool-1",
        grant_revision=7,
        now=NOW,
    )
    assert result.receipt.code == expected
    assert not result.ok


def test_resource_hash_mismatch_is_denied_without_handler(tmp_path: Path) -> None:
    host, calls = _host(tmp_path)
    manifest = _manifest().model_copy(
        update={"resources": (SkillResource(path="prompt.txt", sha256="f" * 64),)}
    )
    signature = hmac.new(KEY, manifest.signed_payload(), hashlib.sha256).digest()
    manifest = manifest.model_copy(
        update={"signature": "hmac-sha256:" + base64.b16encode(signature).decode().lower()}
    )
    grant = _grant()
    result = host.invoke(
        manifest=manifest,
        payload={"text": "hello"},
        grant=grant,
        request=_request(grant),
        tool_use_id="tool-1",
        grant_revision=7,
        now=NOW,
    )
    assert result.receipt.code == SKILL_RESOURCE_HASH_MISMATCH
    assert calls == []


@pytest.mark.parametrize(
    ("name", "kwargs", "expected"),
    (
        ("task mismatch", {"task_id": "other"}, DenyCode.GRANT_BINDING_MISMATCH.value),
        ("operation mismatch", {"operation_key": "other"}, DenyCode.GRANT_BINDING_MISMATCH.value),
        (
            "skill mismatch",
            {"skill": SkillRequest(identity="other", version="1.0.0")},
            DenyCode.TOOL_SKILL_DENIED.value,
        ),
    ),
)
def test_binding_and_skill_mismatch_matrix(
    tmp_path: Path, name: str, kwargs: dict[str, Any], expected: str
) -> None:
    del name
    host, calls = _host(tmp_path)
    grant = _grant()
    result = host.invoke(
        manifest=_manifest(),
        payload={"text": "hello"},
        grant=grant,
        request=_request(grant, **kwargs),
        tool_use_id="tool-1",
        grant_revision=7,
        now=NOW,
    )
    assert result.receipt.code == expected
    assert calls == []


@pytest.mark.parametrize(
    ("tool_use_id", "grant_revision", "current_grant", "cancelled", "expected"),
    (
        ("", 7, None, False, DenyCode.TOOL_CAPABILITY_DENIED.value),
        ("tool-1", 6, None, False, DenyCode.GRANT_REVISION_MISMATCH.value),
        ("tool-1", 7, lambda: None, False, DenyCode.GRANT_REVOKED.value),
        ("tool-1", 7, None, True, DenyCode.GRANT_REVOKED.value),
    ),
)
def test_tool_revision_revoke_cancel_matrix(
    tmp_path: Path,
    tool_use_id: str,
    grant_revision: int,
    current_grant,
    cancelled: bool,
    expected: str,
) -> None:
    host, calls = _host(tmp_path)
    grant = _grant()
    result = host.invoke(
        manifest=_manifest(),
        payload={"text": "hello"},
        grant=grant,
        request=_request(grant),
        tool_use_id=tool_use_id,
        grant_revision=grant_revision,
        current_grant=current_grant,
        is_cancelled=(lambda: cancelled),
        now=NOW,
    )
    assert result.receipt.code == expected
    assert calls == []


def test_manifest_capability_beyond_skill_is_deferred_fail_closed(tmp_path: Path) -> None:
    host, calls = _host(tmp_path)
    grant = _grant()
    manifest = _manifest(capabilities=("skill.use", "path.read"))
    result = host.invoke(
        manifest=manifest,
        payload={"text": "hello"},
        grant=grant,
        request=_request(grant),
        tool_use_id="tool-1",
        grant_revision=7,
        now=NOW,
    )
    assert result.receipt.code == "SKILL_CAPABILITY_DEFERRED"
    assert calls == []


def test_handler_failure_is_receipted_without_raw_error(tmp_path: Path) -> None:
    def failing_handler(payload, context):
        raise RuntimeError("secret-token-value")

    host, calls = _host(tmp_path, handler=failing_handler)
    grant = _grant()
    result = host.invoke(
        manifest=_manifest(),
        payload={"secret": "secret-token-value"},
        grant=grant,
        request=_request(grant),
        tool_use_id="tool-1",
        grant_revision=7,
        now=NOW,
    )
    assert result.receipt.code == "SKILL_HANDLER_FAILED"
    assert "secret-token-value" not in result.receipt.model_dump_json()
    assert calls == []
