"""Pure, host-independent security policy helpers for agent capabilities.

This module deliberately performs lexical classification only.  In particular,
it never resolves a path, asks the OS about a link/reparse point, resolves DNS,
opens a socket, starts a process, or follows an HTTP redirect.  Callers must
carry the returned host-obligation markers to the owning runtime gate.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import SplitResult, urlsplit

MODEL_TOOL_CORRELATION_MISMATCH = "MODEL_TOOL_CORRELATION_MISMATCH"
B06_HOST_FILESYSTEM_VERIFICATION = "B06_HOST_FILESYSTEM_VERIFICATION"
B06_CASE_SENSITIVITY_VERIFICATION = "B06_CASE_SENSITIVITY_VERIFICATION"
B09_HOST_NETWORK_VERIFICATION = "B09_HOST_NETWORK_VERIFICATION"
B09_DNS_REBINDING_VERIFICATION = "B09_DNS_REBINDING_VERIFICATION"
B09_REDIRECT_ORIGIN_REVERIFICATION = "B09_REDIRECT_ORIGIN_REVERIFICATION"


class Verdict(StrEnum):
    """The conservative outcome of a pure policy check."""

    ALLOW = "allow"
    DENY = "deny"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class PolicyResult:
    """A serializable decision with proof and deferred host obligations."""

    verdict: Verdict
    reasons: tuple[str, ...] = ()
    lexical_proof: tuple[str, ...] = ()
    host_obligations: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    @property
    def fail_closed(self) -> bool:
        return self.verdict is not Verdict.ALLOW

    @property
    def decision(self) -> str:
        return self.verdict.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reasons": self.reasons,
            "lexicalProof": self.lexical_proof,
            "hostObligations": self.host_obligations,
        }


class PathStyle(StrEnum):
    POSIX = "posix"
    DRIVE = "drive"
    UNC = "unc"
    RELATIVE = "relative"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PathFixture:
    """A test/runtime-provided link classification; never populated by us."""

    path: str
    platform: str = "posix"
    case_sensitive: bool | None = None
    link_kind: str | None = None


@dataclass(frozen=True)
class PathClassification(PolicyResult):
    style: PathStyle = PathStyle.UNKNOWN
    normalized_lexical: str = ""
    segments: tuple[str, ...] = ()
    case_sensitive: bool | None = None
    link_kind: str | None = None


_DRIVE_RE = re.compile(r"^[A-Za-z]:($|[/\\])")
_ENCODED_TRAVERSAL_RE = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_KNOWN_LINK_KINDS = frozenset({"symlink", "junction", "reparse", "reparse_point"})


def _path_style(path: str) -> PathStyle:
    if path.startswith(("\\\\", "//")):
        return PathStyle.UNC
    if path.startswith("/"):
        return PathStyle.POSIX
    if _DRIVE_RE.match(path):
        return PathStyle.DRIVE
    if path and not path.startswith(("\\",)):
        return PathStyle.RELATIVE
    return PathStyle.UNKNOWN


def _path_segments(path: str) -> tuple[str, ...]:
    return tuple(part for part in re.split(r"[/\\]+", path) if part not in ("", "."))


def classify_path(  # noqa: PLR0912
    path: str,
    *,
    platform: str = "posix",
    case_sensitive: bool | None = None,
    link_kind: str | None = None,
) -> PathClassification:
    """Classify a path using lexical evidence and supplied fixture metadata.

    ``link_kind`` is intentionally an input, not something this function
    discovers.  A clean lexical result still carries B06 because lexical path
    safety cannot prove the host's symlink/junction/reparse identity.
    """

    if not isinstance(path, str) or not path:
        return PathClassification(
            verdict=Verdict.AMBIGUOUS,
            reasons=("PATH_NOT_A_NONEMPTY_STRING",),
            host_obligations=(B06_HOST_FILESYSTEM_VERIFICATION,),
        )

    style = _path_style(path)
    segments = _path_segments(path)
    reasons: list[str] = []
    proof: list[str] = []
    obligations = [B06_HOST_FILESYSTEM_VERIFICATION]
    if "\x00" in path:
        reasons.append("PATH_NUL_BYTE")
    if any(segment == ".." for segment in segments) or _ENCODED_TRAVERSAL_RE.search(path):
        reasons.append("PATH_PARENT_TRAVERSAL")
    if style is PathStyle.UNKNOWN:
        reasons.append("PATH_STYLE_AMBIGUOUS")
    else:
        proof.append(f"PATH_STYLE_{style.value.upper()}")
    if style is PathStyle.UNC:
        reasons.append("PATH_UNC_HOST_BOUNDARY")
    if style is PathStyle.DRIVE:
        proof.append("PATH_DRIVE_ROOT_LEXICALLY_IDENTIFIED")
    if style is PathStyle.POSIX:
        proof.append("PATH_POSIX_ROOT_LEXICALLY_IDENTIFIED")

    effective_case = case_sensitive
    if effective_case is None and platform.lower() in {"windows", "win32", "nt"}:
        effective_case = False
    if effective_case is False:
        proof.append("PATH_CASE_INSENSITIVE_COMPARISON_REQUIRED")
    elif effective_case is None:
        obligations.append(B06_CASE_SENSITIVITY_VERIFICATION)
    normalized = re.sub(r"[/\\]+", "/", path)
    normalized = re.sub(r"(?:^|/)(?:\./)+", "/", normalized)

    normalized_link_kind = link_kind.lower() if isinstance(link_kind, str) else None
    if normalized_link_kind in _KNOWN_LINK_KINDS:
        reasons.append(f"PATH_{normalized_link_kind.upper()}_REQUIRES_HOST_PROOF")
    elif link_kind is not None:
        reasons.append("PATH_LINK_KIND_AMBIGUOUS")

    if reasons:
        verdict = (
            Verdict.DENY
            if "PATH_PARENT_TRAVERSAL" in reasons or "PATH_NUL_BYTE" in reasons
            else Verdict.AMBIGUOUS
        )
    else:
        verdict = Verdict.ALLOW
    return PathClassification(
        verdict=verdict,
        reasons=tuple(reasons),
        lexical_proof=tuple(proof),
        host_obligations=tuple(dict.fromkeys(obligations)),
        style=style,
        normalized_lexical=normalized,
        segments=segments,
        case_sensitive=effective_case,
        link_kind=normalized_link_kind,
    )


def classify_path_fixture(
    fixture: PathFixture | Mapping[str, Any] | str, **kwargs: Any
) -> PathClassification:
    """Classify a fixture without touching the filesystem."""

    if isinstance(fixture, PathFixture):
        return classify_path(
            fixture.path,
            platform=fixture.platform,
            case_sensitive=fixture.case_sensitive,
            link_kind=fixture.link_kind,
        )
    if isinstance(fixture, Mapping):
        raw_path = fixture.get("path")
        return classify_path(
            raw_path if isinstance(raw_path, str) else "",
            platform=str(fixture.get("platform", "posix")),
            case_sensitive=fixture.get("case_sensitive"),
            link_kind=fixture.get("link_kind", fixture.get("file_type")),
        )
    return classify_path(fixture, **kwargs)


_SHELL_META_RE = re.compile(r"[\x00-\x1f\x7f;&|`$<>\n\r]")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class ShellClassification(PolicyResult):
    argv: tuple[str, ...] = ()
    cwd: PathClassification | None = None
    unsafe_env_names: tuple[str, ...] = ()


def classify_shell_invocation(  # noqa: PLR0912
    argv: Sequence[str] | None,
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    shell: bool = False,
) -> ShellClassification:
    """Classify argv/cwd/env without tokenizing or executing a shell command."""

    values = tuple(argv) if argv is not None else ()
    reasons: list[str] = []
    proof: list[str] = []
    unsafe_env_names: list[str] = []
    path_result = classify_path(cwd) if cwd is not None else None
    if not values:
        reasons.append("ARGV_EMPTY")
    if shell:
        reasons.append("SHELL_INTERPRETER_REQUESTED")
    for index, token in enumerate(values):
        if not isinstance(token, str):
            reasons.append(f"ARGV_TOKEN_{index}_NOT_STRING")
        elif "\x00" in token:
            reasons.append(f"ARGV_TOKEN_{index}_NUL_BYTE")
        elif _SHELL_META_RE.search(token):
            reasons.append(f"ARGV_TOKEN_{index}_SHELL_META")
        elif _ENV_ASSIGNMENT_RE.match(token) and index == 0:
            reasons.append("ARGV_LEADING_ENV_ASSIGNMENT")
    if values and all(isinstance(token, str) for token in values) and not reasons:
        proof.append("ARGV_ISOLATED_TOKENS_NO_SHELL_META")
    if path_result is not None:
        if path_result.fail_closed:
            reasons.extend(f"CWD_{reason}" for reason in path_result.reasons)
        proof.extend(f"CWD_{item}" for item in path_result.lexical_proof)
    if env is not None:
        for name, value in env.items():
            if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
                unsafe_env_names.append(str(name))
                continue
            if not isinstance(value, str) or "\x00" in value:
                unsafe_env_names.append(name)
            elif _SHELL_META_RE.search(value):
                reasons.append(f"ENV_{name}_SHELL_META")
        if unsafe_env_names:
            reasons.append("ENV_NAME_OR_VALUE_INVALID")
        elif not reasons:
            proof.append("ENV_NAMES_AND_VALUES_LEXICALLY_SAFE")
    if path_result is not None and path_result.verdict is Verdict.AMBIGUOUS:
        reasons.append("CWD_HOST_IDENTITY_UNPROVEN")
    verdict = Verdict.DENY if reasons else Verdict.ALLOW
    return ShellClassification(
        verdict=verdict,
        reasons=tuple(dict.fromkeys(reasons)),
        lexical_proof=tuple(dict.fromkeys(proof)),
        host_obligations=path_result.host_obligations if path_result else (),
        argv=values,
        cwd=path_result,
        unsafe_env_names=tuple(unsafe_env_names),
    )


def classify_shell_tokens(command: str) -> ShellClassification:
    """Classify a raw command as unsafe; this function never parses/executes it."""

    return classify_shell_invocation((command,), shell=True)


@dataclass(frozen=True)
class NetworkClassification(PolicyResult):
    scheme: str = ""
    hostname: str = ""
    canonical_hostname: str = ""
    address_kind: str = "hostname"
    redirect_count: int = 0


_NETWORK_SCHEMES = frozenset({"http", "https"})


def _url_parts(target: str) -> SplitResult | None:
    try:
        parts = urlsplit(target)
        if not parts.scheme or not parts.netloc or parts.hostname is None:
            return None
        return parts
    except ValueError:
        return None


def _canonical_hostname(hostname: str) -> tuple[str, bool]:
    try:
        canonical = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return "", False
    return canonical, canonical != hostname.lower().rstrip(".")


def classify_network_target(  # noqa: PLR0912, PLR0915
    target: str,
    *,
    redirect_chain: Sequence[str] = (),
    allow_private: bool = False,
    allow_link_local: bool = False,
    allow_redirects: bool = False,
) -> NetworkClassification:
    """Classify a URL lexically; hostname resolution is a B09 obligation."""

    parts = _url_parts(target) if isinstance(target, str) else None
    reasons: list[str] = []
    proof: list[str] = []
    obligations = [B09_HOST_NETWORK_VERIFICATION]
    if parts is None:
        reasons.append("URL_MALFORMED_OR_HOST_MISSING")
        return NetworkClassification(
            Verdict.DENY, tuple(reasons), host_obligations=tuple(obligations)
        )
    scheme = parts.scheme.lower()
    hostname = parts.hostname or ""
    canonical, is_idn = _canonical_hostname(hostname)
    if scheme not in _NETWORK_SCHEMES:
        reasons.append("URL_SCHEME_NOT_ALLOWED")
    if parts.username is not None or parts.password is not None:
        reasons.append("URL_USERINFO_NOT_ALLOWED")
    try:
        port = parts.port
    except ValueError:
        port = None
        reasons.append("URL_PORT_MALFORMED")
    if port is not None and not 1 <= port <= 65535:
        reasons.append("URL_PORT_OUT_OF_RANGE")
    if is_idn:
        reasons.append("URL_IDN_REQUIRES_ORIGIN_POLICY")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    address_kind = "hostname"
    if address is not None:
        address_kind = "ipv6" if address.version == 6 else "ipv4"
        if address.is_loopback:
            reasons.append("URL_LOOPBACK_ADDRESS")
        if address.is_private and not allow_private:
            reasons.append("URL_PRIVATE_ADDRESS")
        if address.is_link_local and not allow_link_local:
            reasons.append("URL_LINK_LOCAL_ADDRESS")
        if address.is_reserved:
            reasons.append("URL_RESERVED_ADDRESS")
        if address.is_unspecified:
            reasons.append("URL_UNSPECIFIED_ADDRESS")
        proof.append("URL_LITERAL_IP_CLASSIFIED_WITHOUT_RESOLUTION")
    else:
        if hostname.lower().rstrip(".") == "localhost" or hostname.lower().endswith(".localhost"):
            reasons.append("URL_LOCALHOST_NAME")
        reasons.append("URL_HOSTNAME_REQUIRES_DNS_REBINDING_CHECK")
        obligations.append(B09_DNS_REBINDING_VERIFICATION)
        proof.append("URL_HOSTNAME_SYNTAX_ONLY_NO_DNS_LOOKUP")
    if redirect_chain:
        reasons.append("URL_REDIRECT_CHAIN_PRESENT")
        obligations.append(B09_REDIRECT_ORIGIN_REVERIFICATION)
        if not allow_redirects:
            reasons.append("URL_REDIRECTS_NOT_ALLOWED")
    verdict = (
        Verdict.DENY
        if any(
            reason in reasons
            for reason in (
                "URL_MALFORMED_OR_HOST_MISSING",
                "URL_SCHEME_NOT_ALLOWED",
                "URL_USERINFO_NOT_ALLOWED",
                "URL_LOOPBACK_ADDRESS",
                "URL_PRIVATE_ADDRESS",
                "URL_LINK_LOCAL_ADDRESS",
                "URL_LOCALHOST_NAME",
                "URL_REDIRECTS_NOT_ALLOWED",
            )
        )
        else (Verdict.AMBIGUOUS if reasons else Verdict.ALLOW)
    )
    return NetworkClassification(
        verdict=verdict,
        reasons=tuple(dict.fromkeys(reasons)),
        lexical_proof=tuple(dict.fromkeys(proof)),
        host_obligations=tuple(dict.fromkeys(obligations)),
        scheme=scheme,
        hostname=hostname,
        canonical_hostname=canonical,
        address_kind=address_kind,
        redirect_count=len(redirect_chain),
    )


@dataclass(frozen=True)
class ToolCorrelation:
    """Correlation outcome; a mismatch can never invoke a tool."""

    ok: bool
    code: str | None
    tool_invoked: bool
    retryable: bool
    tool_use_id: str | None
    expected_tool_use_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "toolInvoked": self.tool_invoked,
            "retryable": self.retryable,
            "toolUseId": self.tool_use_id,
            "expectedToolUseId": self.expected_tool_use_id,
        }


def correlate_tool_use_id(
    expected_tool_use_id: str | None,
    tool_use_id: str | None,
    *,
    seen_tool_use_ids: Sequence[str] = (),
) -> ToolCorrelation:
    """Accept exactly one known id; unknown, duplicate, or mismatched all fail closed."""

    known = isinstance(expected_tool_use_id, str) and bool(expected_tool_use_id)
    supplied = isinstance(tool_use_id, str) and bool(tool_use_id)
    duplicate = supplied and tool_use_id in set(seen_tool_use_ids)
    if not known or not supplied or duplicate or tool_use_id != expected_tool_use_id:
        return ToolCorrelation(
            ok=False,
            code=MODEL_TOOL_CORRELATION_MISMATCH,
            tool_invoked=False,
            retryable=False,
            tool_use_id=tool_use_id,
            expected_tool_use_id=expected_tool_use_id,
        )
    return ToolCorrelation(
        ok=True,
        code=None,
        tool_invoked=False,
        retryable=False,
        tool_use_id=tool_use_id,
        expected_tool_use_id=expected_tool_use_id,
    )


validate_tool_correlation = correlate_tool_use_id


__all__ = [
    "B06_CASE_SENSITIVITY_VERIFICATION",
    "B06_HOST_FILESYSTEM_VERIFICATION",
    "B09_DNS_REBINDING_VERIFICATION",
    "B09_HOST_NETWORK_VERIFICATION",
    "B09_REDIRECT_ORIGIN_REVERIFICATION",
    "MODEL_TOOL_CORRELATION_MISMATCH",
    "NetworkClassification",
    "PathClassification",
    "PathFixture",
    "PathStyle",
    "PolicyResult",
    "ShellClassification",
    "ToolCorrelation",
    "Verdict",
    "classify_network_target",
    "classify_path",
    "classify_path_fixture",
    "classify_shell_invocation",
    "classify_shell_tokens",
    "correlate_tool_use_id",
    "validate_tool_correlation",
]
