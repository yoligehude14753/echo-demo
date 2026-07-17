"""B03 falsifiable policy matrix: all inputs are in-memory and host-independent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.agent_capabilities.compiler import compile_grant, decide
from app.agent_capabilities.policy import (
    CapabilityFact,
    DecisionStatus,
    PermissionFacts,
    ReasonCode,
    normalize_command_scope,
    normalize_network_target,
    normalize_path_root,
    normalize_skill_scope,
)
from app.agent_capabilities.types import GrantSnapshot
from pydantic import ValidationError

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def _facts(*capabilities: CapabilityFact, revision: int = 7, **kwargs: object) -> PermissionFacts:
    return PermissionFacts(revision=revision, capabilities=capabilities, **kwargs)


def _happy_facts() -> PermissionFacts:
    return _facts(
        CapabilityFact("path.read", {"platform": "posix", "root": "/workspace/project"}),
        CapabilityFact(
            "command.execute",
            {
                "platform": "posix",
                "argv": ("git", "status", "--short"),
                "cwd": "/workspace/project",
                "env_names": ("LANG",),
            },
        ),
        CapabilityFact(
            "network.connect",
            {"scheme": "https", "host": "93.184.216.34", "port": 443},
        ),
        CapabilityFact(
            "skill.use",
            {"identity": "echo/transcript", "version": "1.2.3", "provenance": "fact:F-ECHO-SKILL-1"},
        ),
    )


def test_compile_and_decide_happy_path_produces_immutable_grant() -> None:
    result = compile_grant(_happy_facts(), task_id="task-b03", now=NOW)

    assert result.allowed
    assert isinstance(result.grant, GrantSnapshot)
    assert result.grant.schema_version == 1
    assert result.grant.revision == 7
    assert result.grant.grant_id.startswith("grant_")
    assert all(isinstance(item, CapabilityFact) is False for item in result.rules)
    assert decide(result, "path.read", {"platform": "posix", "path": "/workspace/project/src/app.py"}, now=NOW).allowed
    assert decide(
        result,
        "command.execute",
        {"platform": "posix", "argv": ("git", "status", "--short"), "cwd": "/workspace/project", "env_names": ()},
        now=NOW,
    ).allowed
    assert decide(result, "network.connect", {"scheme": "https", "host": "93.184.216.34", "port": 443}, now=NOW).allowed
    assert decide(
        result,
        "skill.use",
        {"identity": "echo/transcript", "version": "1.2.3", "provenance": "fact:F-ECHO-SKILL-1"},
        now=NOW,
    ).allowed
    with pytest.raises(ValidationError):
        result.grant.grant_revision = 8  # type: ignore[misc]


def test_path_root_requires_explicit_platform_and_blocks_prefix_escape() -> None:
    ambiguous = compile_grant(
        _facts(CapabilityFact("path.read", {"root": "/workspace/project"})), task_id="task", now=NOW
    )
    assert ambiguous.status is DecisionStatus.DENY
    assert ambiguous.reason_code is ReasonCode.AMBIGUOUS_INPUT

    result = compile_grant(
        _facts(CapabilityFact("path.read", {"platform": "posix", "root": "/workspace/project"})),
        task_id="task",
        now=NOW,
    )
    assert not decide(result, "path.read", {"platform": "posix", "path": "/workspace/project-evil/file"}, now=NOW).allowed
    assert decide(result, "path.read", {"platform": "posix", "path": "/workspace/project/ok"}, now=NOW).allowed


def test_path_normalization_is_lexical_and_rejects_host_dependent_forms() -> None:
    assert normalize_path_root("/workspace/./project/../project", platform="posix").root == "/workspace/project"
    assert normalize_path_root(r"C:\\Echo Desk\\project\\..\\project", platform="windows").root == r"C:\Echo Desk\project"
    with pytest.raises(ValueError):
        normalize_path_root("~/project", platform="posix")
    with pytest.raises(ValueError):
        normalize_path_root(r"\\?\C:\\device", platform="windows")


@pytest.mark.parametrize(
    ("command_argv", "reason"),
    [
        (("git", "status", "--porcelain"), ReasonCode.COMMAND_NOT_AUTHORIZED),
        (("git", "status", "--short", "--branch"), ReasonCode.COMMAND_NOT_AUTHORIZED),
    ],
)
def test_command_authority_is_exact_argv_and_cwd(command_argv: tuple[str, ...], reason: ReasonCode) -> None:
    facts = _facts(
        CapabilityFact(
            "command.execute",
            {"platform": "posix", "argv": ("git", "status", "--short"), "cwd": "/workspace", "env_names": ("LANG",)},
        )
    )
    result = compile_grant(facts, task_id="task", now=NOW)
    decision = decide(
        result,
        "command.execute",
        {"platform": "posix", "argv": command_argv, "cwd": "/workspace", "env_names": ()},
        now=NOW,
    )
    assert decision.status is DecisionStatus.DENY
    assert decision.reason_code is reason
    assert decide(
        result,
        "command.execute",
        {"platform": "posix", "argv": ("git", "status", "--short"), "cwd": "/other", "env_names": ()},
        now=NOW,
    ).reason_code is ReasonCode.COMMAND_NOT_AUTHORIZED


def test_command_shell_text_and_env_assignment_are_fail_closed() -> None:
    shell_text = compile_grant(
        _facts(CapabilityFact("command.execute", {"platform": "posix", "argv": "git status", "cwd": "/workspace"})),
        task_id="task",
        now=NOW,
    )
    assert shell_text.reason_code is ReasonCode.AMBIGUOUS_INPUT

    invalid_env = compile_grant(
        _facts(CapabilityFact("command.execute", {"platform": "posix", "argv": ("git",), "cwd": "/workspace", "env_names": ("TOKEN=secret",)})),
        task_id="task",
        now=NOW,
    )
    assert invalid_env.reason_code is ReasonCode.INVALID_INPUT
    authority = normalize_command_scope(("git",), cwd="/workspace", env_names=("LANG",), platform="posix")
    assert authority.env_names == ("LANG",)


def test_network_matrix_blocks_ssrf_and_unverified_or_redirected_targets() -> None:
    private = compile_grant(_facts(CapabilityFact("network.connect", {"scheme": "http", "host": "127.0.0.1", "port": 80})), task_id="task", now=NOW)
    assert private.reason_code is ReasonCode.NETWORK_SSRF_BLOCKED

    unverified = compile_grant(_facts(CapabilityFact("network.connect", {"scheme": "https", "host": "api.example", "port": 443})), task_id="task", now=NOW)
    assert unverified.status is DecisionStatus.HOST_VERIFICATION_REQUIRED

    verified = compile_grant(
        _facts(CapabilityFact("network.connect", {"scheme": "https", "host": "api.example", "port": 443, "verified_ips": ("93.184.216.34",)})),
        task_id="task",
        now=NOW,
    )
    assert verified.allowed
    redirect = decide(
        verified,
        "network.connect",
        {"scheme": "https", "host": "api.example", "port": 443, "verified_ips": ("93.184.216.34",), "redirects": ({"scheme": "https", "host": "cdn.example", "port": 443, "verified_ips": ("93.184.216.35",)},)},
        now=NOW,
    )
    assert redirect.reason_code is ReasonCode.REDIRECT_NOT_AUTHORIZED

    private_resolution = compile_grant(
        _facts(CapabilityFact("network.connect", {"scheme": "https", "host": "api.example", "port": 443, "verified_ips": ("10.0.0.8",)})),
        task_id="task",
        now=NOW,
    )
    assert private_resolution.reason_code is ReasonCode.NETWORK_SSRF_BLOCKED


def test_network_normalization_rejects_unknown_scheme_and_url_authority_syntax() -> None:
    with pytest.raises(ValueError):
        normalize_network_target("ftp", "example.com")
    with pytest.raises(ValueError):
        normalize_network_target("https", "https://example.com")


def test_unknown_capability_conflict_and_revision_freshness_never_allow() -> None:
    unknown = compile_grant(_facts(CapabilityFact("filesystem.delete", {"root": "/workspace", "platform": "posix"})), task_id="task", now=NOW)
    assert unknown.reason_code is ReasonCode.UNKNOWN_CAPABILITY

    conflict = compile_grant(
        _facts(
            CapabilityFact("path.read", {"platform": "posix", "root": "/workspace"}, "allow"),
            CapabilityFact("path.read", {"platform": "posix", "root": "/workspace/project"}, "deny"),
        ),
        task_id="task",
        now=NOW,
    )
    assert conflict.reason_code is ReasonCode.CONFLICTING_SCOPE

    stale = compile_grant(_facts(CapabilityFact("path.read", {"platform": "posix", "root": "/workspace"}), revision=0), task_id="task", now=NOW)
    assert stale.reason_code is ReasonCode.STALE_REVISION
    marked_stale = compile_grant(PermissionFacts(7, (CapabilityFact("path.read", {"platform": "posix", "root": "/workspace"}),), stale=True), task_id="task", now=NOW)
    assert marked_stale.reason_code is ReasonCode.STALE_REVISION
    expired = compile_grant(_facts(CapabilityFact("path.read", {"platform": "posix", "root": "/workspace"}), expires_at="2026-07-15T08:59:00Z"), task_id="task", now=NOW)
    assert expired.reason_code is ReasonCode.EXPIRED_REVISION


def test_skill_identity_and_version_are_exact_and_ranges_are_ambiguous() -> None:
    result = compile_grant(
        _facts(CapabilityFact("skill.use", {"identity": "echo/transcript", "version": "1.2.3", "provenance": "fact:F-SKILL-1"})),
        task_id="task",
        now=NOW,
    )
    assert decide(result, "skill.use", {"identity": "echo/transcript", "version": "1.2.4", "provenance": "fact:F-SKILL-1"}, now=NOW).reason_code is ReasonCode.SKILL_NOT_AUTHORIZED
    assert decide(result, "skill.use", {"identity": "echo/transcript", "version": "1.2.3", "provenance": "fact:F-SKILL-2"}, now=NOW).reason_code is ReasonCode.SKILL_NOT_AUTHORIZED
    with pytest.raises(ValueError):
        normalize_skill_scope({"identity": "echo/transcript", "version": "latest", "provenance": "fact:F-SKILL-1"})
    with pytest.raises(ValueError):
        normalize_skill_scope({"identity": "echo/transcript", "version": "1.2.3", "provenance": ""})
