"""CSRF / cross-origin middleware tests.

The defense should:
- Allow loopback origins (the obvious case)
- Allow same-host origins regardless of address (Tailscale, Nebula, tunnels)
- Allow non-browser callers that omit the Origin header (curl, CLI)
- Block legitimately-cross-origin mutating requests
- Allow safe HTTP methods even from cross-origin (GET, HEAD, OPTIONS)
"""

from __future__ import annotations

import pytest
from app.main import _origin_is_same_host


@pytest.mark.parametrize(
    "origin,request_host,expected",
    [
        # Same host, exact match (with port)
        ("http://100.100.100.100:5173", "100.100.100.100:5173", True),
        # Same host, different port (Vite proxy → backend)
        ("http://100.100.100.100:5173", "100.100.100.100:8000", True),
        # Loopback over IPv4
        ("http://127.0.0.1:5173", "127.0.0.1:8000", True),
        # localhost
        ("http://localhost:5173", "localhost:8000", True),
        # Tunnel domain (e.g. *.trycloudflare.com)
        ("https://example-tunnel.trycloudflare.com", "example-tunnel.trycloudflare.com", True),
        # Different host — the actual CSRF threat
        ("https://evil.com", "100.100.100.100:5173", False),
        ("https://evil.example.org", "127.0.0.1:8000", False),
        # Different host even on same scheme
        ("http://malicious.local", "100.100.100.100:5173", False),
        # Empty / missing values
        ("", "127.0.0.1:8000", False),
        ("http://localhost:5173", None, False),
        # Malformed origin
        ("not-a-url", "127.0.0.1:8000", False),
    ],
)
def test_origin_is_same_host(origin: str, request_host: str | None, expected: bool) -> None:
    assert _origin_is_same_host(origin, request_host) is expected
