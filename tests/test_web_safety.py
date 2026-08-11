"""SSRF safety-gate tests (no network access — DNS is monkeypatched)."""

from __future__ import annotations

import socket

from aegis_agent.tools.web.url_safety import is_safe_url


def _resolve_to(*ips: str):
    """Return a fake getaddrinfo that resolves any host to the given IPs."""
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]
    return fake_getaddrinfo


def _dns_fail(*args, **kwargs):
    raise socket.gaierror("name or service not known")


def test_blocks_unsupported_scheme(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("8.8.8.8"))
    assert is_safe_url("file:///etc/passwd") is False
    assert is_safe_url("ftp://example.com/x") is False
    assert is_safe_url("gopher://example.com") is False


def test_blocks_cloud_metadata_literal_ip():
    # 169.254.169.254 is always-blocked without needing DNS.
    assert is_safe_url("http://169.254.169.254/latest/meta-data") is False
    assert is_safe_url("https://169.254.169.254/") is False


def test_blocks_metadata_hostname():
    assert is_safe_url("http://metadata.google.internal/") is False


def test_blocks_loopback_and_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("127.0.0.1"))
    assert is_safe_url("http://localhost:8080/admin") is False
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("192.168.1.10"))
    assert is_safe_url("http://internal.example.local/") is False
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("10.0.0.5"))
    assert is_safe_url("http://ten.example.local/") is False


def test_blocks_cgnat(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("100.64.1.1"))
    assert is_safe_url("http://cgnat.example/") is False


def test_blocks_hostname_resolving_to_metadata(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("169.254.169.254"))
    assert is_safe_url("http://evil.example.com/") is False


def test_allows_public_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("93.184.216.34"))
    assert is_safe_url("https://example.com/") is True
    assert is_safe_url("http://example.com/page") is True


def test_fails_closed_on_dns_error(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns_fail)
    assert is_safe_url("http://does-not-resolve.example/") is False


def test_empty_or_malformed():
    assert is_safe_url("") is False
    assert is_safe_url("http://") is False
    assert is_safe_url("not-a-url") is False
