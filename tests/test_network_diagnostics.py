from __future__ import annotations

from telegram_codex_control.network_diagnostics import (
    build_dns_network_restriction_guidance,
    is_dns_network_restriction_error,
)


def test_dns_restriction_detection_matches_known_patterns() -> None:
    assert is_dns_network_restriction_error("Could not resolve host: api.unsplash.com")
    assert is_dns_network_restriction_error("socket.gaierror: [Errno 8] nodename nor servname provided")
    assert is_dns_network_restriction_error("request failed: ENOTFOUND api.notion.com")
    assert not is_dns_network_restriction_error("401 Unauthorized")


def test_dns_restriction_guidance_includes_detail() -> None:
    guidance = build_dns_network_restriction_guidance(failure_detail="Could not resolve host: www.google.com")
    assert "External network appears restricted in this session" in guidance
    assert "Detected error: Could not resolve host: www.google.com" in guidance
