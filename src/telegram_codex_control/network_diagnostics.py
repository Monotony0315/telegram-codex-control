from __future__ import annotations


_DNS_RESTRICTION_MARKERS = (
    "could not resolve host",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided, or not known",
    "getaddrinfo failed",
    "socket.gaierror",
    "errno 8",
    "enotfound",
)


def is_dns_network_restriction_error(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in _DNS_RESTRICTION_MARKERS)


def build_dns_network_restriction_guidance(*, failure_detail: str | None = None) -> str:
    lines = [
        "External network appears restricted in this session (DNS resolution failed).",
        (
            "Requests to hosts such as api.notion.com, api.unsplash.com, and "
            "www.google.com can fail before authentication."
        ),
        "Local workspace commands still work: /files, /search, /read, /download.",
        "For internet-required tasks, run the same command in a network-enabled terminal/session.",
    ]
    detail = (failure_detail or "").strip()
    if detail:
        lines.append(f"Detected error: {detail}")
    return "\n".join(lines)
