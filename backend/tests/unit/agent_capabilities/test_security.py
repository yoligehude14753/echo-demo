from __future__ import annotations

import pytest
from app.agent_capabilities.redaction import REDACTED, redact_audit_event
from app.agent_capabilities.security import (
    B06_HOST_FILESYSTEM_VERIFICATION,
    B09_DNS_REBINDING_VERIFICATION,
    B09_HOST_NETWORK_VERIFICATION,
    MODEL_TOOL_CORRELATION_MISMATCH,
    PathFixture,
    Verdict,
    classify_network_target,
    classify_path,
    classify_path_fixture,
    classify_shell_invocation,
    correlate_tool_use_id,
)


@pytest.mark.unit
def test_path_fixture_matrix_is_lexical_and_defers_host_identity_to_b06() -> None:
    clean = classify_path("/workspace/project/file.txt", platform="posix", case_sensitive=True)
    drive = classify_path(r"C:\Workspace\Project\file.txt", platform="windows")
    unc = classify_path(r"\\server\share\project\file.txt", platform="windows")
    case_sensitive = classify_path("/Workspace/Project", case_sensitive=True)
    parent = classify_path("/workspace/project/../outside")

    assert clean.verdict is Verdict.ALLOW
    assert "PATH_STYLE_POSIX" in clean.lexical_proof
    assert B06_HOST_FILESYSTEM_VERIFICATION in clean.host_obligations
    assert drive.style.value == "drive"
    assert drive.case_sensitive is False
    assert unc.style.value == "unc"
    assert "PATH_UNC_HOST_BOUNDARY" in unc.reasons
    assert case_sensitive.case_sensitive is True
    assert parent.verdict is Verdict.DENY
    assert "PATH_PARENT_TRAVERSAL" in parent.reasons

    for link_kind in ("symlink", "junction", "reparse"):
        linked = classify_path_fixture(
            PathFixture("/workspace/project/link", link_kind=link_kind, case_sensitive=True)
        )
        assert linked.verdict is Verdict.AMBIGUOUS
        assert any(link_kind.upper() in reason for reason in linked.reasons)
        assert B06_HOST_FILESYSTEM_VERIFICATION in linked.host_obligations


@pytest.mark.unit
def test_shell_argv_cwd_and_env_injection_fail_closed_without_execution() -> None:
    safe = classify_shell_invocation(
        ("echo", "hello world"),
        cwd="/workspace/project",
        env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
    )
    argv_injection = classify_shell_invocation(("echo", "ok; touch /tmp/pwned"))
    env_injection = classify_shell_invocation(("echo", "ok"), env={"SAFE": "$(whoami)"})
    cwd_traversal = classify_shell_invocation(("echo", "ok"), cwd="/workspace/../etc")

    assert safe.verdict is Verdict.ALLOW
    assert "ARGV_ISOLATED_TOKENS_NO_SHELL_META" in safe.lexical_proof
    assert argv_injection.verdict is Verdict.DENY
    assert any("SHELL_META" in reason for reason in argv_injection.reasons)
    assert env_injection.verdict is Verdict.DENY
    assert "ENV_SAFE_SHELL_META" in env_injection.reasons
    assert cwd_traversal.verdict is Verdict.DENY
    assert any(reason.startswith("CWD_PATH_PARENT_TRAVERSAL") for reason in cwd_traversal.reasons)


@pytest.mark.unit
def test_network_matrix_classifies_literals_and_marks_b09_host_obligations() -> None:
    hostname = classify_network_target("https://example.test/api")
    idn = classify_network_target("https://例子.测试/api")
    private = classify_network_target("http://192.168.1.7:8080")
    link_local = classify_network_target("http://169.254.169.254/latest")
    redirect = classify_network_target(
        "https://example.test/start",
        redirect_chain=("https://other.test/final",),
        allow_redirects=False,
    )

    assert hostname.verdict is Verdict.AMBIGUOUS
    assert "URL_HOSTNAME_REQUIRES_DNS_REBINDING_CHECK" in hostname.reasons
    assert B09_HOST_NETWORK_VERIFICATION in hostname.host_obligations
    assert B09_DNS_REBINDING_VERIFICATION in hostname.host_obligations
    assert idn.verdict is Verdict.AMBIGUOUS
    assert "URL_IDN_REQUIRES_ORIGIN_POLICY" in idn.reasons
    assert private.verdict is Verdict.DENY
    assert "URL_PRIVATE_ADDRESS" in private.reasons
    assert link_local.verdict is Verdict.DENY
    assert "URL_LINK_LOCAL_ADDRESS" in link_local.reasons
    assert redirect.verdict is Verdict.DENY
    assert "B09_REDIRECT_ORIGIN_REVERIFICATION" in redirect.host_obligations
    assert "URL_REDIRECTS_NOT_ALLOWED" in redirect.reasons


@pytest.mark.unit
def test_unknown_duplicate_and_mismatched_tool_use_ids_share_fail_closed_code() -> None:
    results = (
        correlate_tool_use_id("expected", "unknown"),
        correlate_tool_use_id("expected", "expected", seen_tool_use_ids=("expected",)),
        correlate_tool_use_id("expected", "other"),
    )

    assert all(result.code == MODEL_TOOL_CORRELATION_MISMATCH for result in results)
    assert all(
        not result.ok and not result.tool_invoked and not result.retryable for result in results
    )


@pytest.mark.unit
def test_audit_redaction_preserves_policy_shape_but_never_secret_or_value() -> None:
    secret = "super-secret-token-7f3c"
    value = "raw-tool-result-value"
    event = {
        "decision": "deny",
        "toolUseId": "tool-123",
        "secret": secret,
        "value": value,
        "env": {"TOKEN": secret, "PATH": "/usr/bin"},
        "message": (
            f"authorization=Bearer {secret} "
            f"https://user:pass@example.test/api?token={secret}&value={value}"
        ),
        "reasons": ["URL_PRIVATE_ADDRESS"],
    }

    redacted = redact_audit_event(event)
    rendered = repr(redacted)

    assert redacted["decision"] == "deny"
    assert redacted["reasons"] == ["URL_PRIVATE_ADDRESS"]
    assert redacted["secret"] == REDACTED
    assert redacted["value"] == REDACTED
    assert redacted["env"]["TOKEN"] == REDACTED
    assert redacted["env"]["PATH"] == "/usr/bin"
    assert secret not in rendered
    assert value not in rendered
    assert "user:pass" not in rendered
    assert "?redacted" in redacted["message"]
