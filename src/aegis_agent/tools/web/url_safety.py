# Portions adapted from Hermes (hermes-agent), © 2025 Nous Research.
# Licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
#
# ADAPT of Hermes ``tools/url_safety.py`` (© 2025 Nous Research, MIT) — the
# SSRF gate.  Kept: the http/https scheme allowlist, the cloud-metadata /
# link-local always-blocked floor (with IPv4-mapped IPv6 variants), private /
# loopback / reserved / multicast / CGNAT blocking, and fail-closed behaviour
# on DNS failure.  Dropped (Hermes coupling): the ``security.allow_private_urls``
# config/env toggle and its cache, the QQ trusted-host allowlist, and the async
# wrapper.  Aegis always enforces private-IP blocking (no opt-out) and is
# synchronous-only.
"""URL safety checks — block requests to private/internal network addresses.

Prevents SSRF (Server-Side Request Forgery), where a malicious prompt or web
result could trick the agent into fetching internal resources such as cloud
metadata endpoints (``169.254.169.254``), localhost services, or private hosts.

Pure stdlib.  Fails closed: DNS errors and unexpected exceptions block the
request.  Cloud metadata endpoints are always blocked — they are never a
legitimate agent target.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Cloud metadata hostnames — always blocked.
_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

# Cloud metadata / credential endpoints — the #1 SSRF target — plus the
# link-local range where they live.  IPv4-mapped IPv6 variants included because
# resolvers may return ``::ffff:x.x.x.x``.
_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),   # AWS/GCP/Azure/DO/Oracle metadata
    ipaddress.ip_address("169.254.170.2"),     # AWS ECS task metadata
    ipaddress.ip_address("169.254.169.253"),   # Azure IMDS wire server
    ipaddress.ip_address("fd00:ec2::254"),     # AWS metadata (IPv6)
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud metadata
    ipaddress.ip_address("::ffff:169.254.169.254"),
    ipaddress.ip_address("::ffff:169.254.170.2"),
    ipaddress.ip_address("::ffff:169.254.169.253"),
    ipaddress.ip_address("::ffff:100.100.100.200"),
})
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),          # entire link-local range
    ipaddress.ip_network("::ffff:169.254.0.0/112"),  # IPv4-mapped link-local
)

# 100.64.0.0/10 (CGNAT / Shared Address Space, RFC 6598) is not covered by
# ipaddress.is_private — block it explicitly.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the IP should be blocked for SSRF protection."""
    # IPv4-mapped IPv6 → check the embedded IPv4 address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        embedded = ip.ipv4_mapped
        return (
            embedded.is_private or embedded.is_loopback or embedded.is_link_local
            or embedded.is_reserved or embedded.is_multicast or embedded.is_unspecified
            or embedded in _CGNAT_NETWORK
        )
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    return ip in _CGNAT_NETWORK


def _is_always_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip in _ALWAYS_BLOCKED_IPS or any(ip in net for net in _ALWAYS_BLOCKED_NETWORKS)


def is_safe_url(url: str) -> bool:
    """Return True if the URL target is not a private/internal address.

    Resolves the hostname and checks every answer against the blocked sets.
    Fails closed: DNS errors and unexpected exceptions block the request.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").strip().lower()
        if scheme not in {"http", "https"}:
            logger.warning("Blocked request — unsupported URL scheme: %s", scheme or "<empty>")
            return False
        if not hostname:
            return False

        # Always block known metadata hostnames.
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning("Blocked request to internal hostname: %s", hostname)
            return False

        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            # DNS failure — fail closed (the HTTP client would fail too).
            logger.warning("Blocked request — DNS resolution failed for: %s", hostname)
            return False

        for _family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if _is_always_blocked_ip(ip):
                logger.warning("Blocked request to cloud metadata address: %s -> %s", hostname, ip_str)
                return False
            if _is_blocked_ip(ip):
                logger.warning("Blocked request to private/internal address: %s -> %s", hostname, ip_str)
                return False

        return True

    except Exception as exc:  # noqa: BLE001 — fail closed on any parsing edge case
        logger.warning("Blocked request — URL safety check error for %s: %s", url, exc)
        return False


__all__ = ["is_safe_url"]
