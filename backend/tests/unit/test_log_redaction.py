from __future__ import annotations

import logging

import pytest
from app.security.redaction import REDACTED, RedactingFormatter, RedactingLogFilter, sanitize_text


@pytest.mark.unit
def test_sanitize_text_removes_absolute_url_userinfo_query_and_relative_query() -> None:
    raw = (
        "GET /meetings/m1/share?share=fake-ticket HTTP/1.1 "
        "https://fake-user:fake-pass@example.test/v1?token=fake-token#fragment"
    )
    sanitized = sanitize_text(raw)
    assert "fake-ticket" not in sanitized
    assert "fake-user" not in sanitized
    assert "fake-pass" not in sanitized
    assert "fake-token" not in sanitized
    assert "fragment" not in sanitized
    assert "/meetings/m1/share?redacted" in sanitized
    assert "https://example.test/v1?redacted" in sanitized


@pytest.mark.unit
def test_log_filter_redacts_lazy_uvicorn_request_target_and_authorization() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d authorization=%s',
        (
            "127.0.0.1:1234",
            "GET",
            "/meetings/m1/share?share=fake-ticket",
            "1.1",
            200,
            "Bearer fake-bearer",
        ),
        None,
    )
    assert RedactingLogFilter().filter(record) is True
    rendered = RedactingFormatter("%(message)s").format(record)
    assert "fake-ticket" not in rendered
    assert "fake-bearer" not in rendered
    assert REDACTED in rendered
