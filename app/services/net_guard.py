"""Outbound endpoint guard.

The LLM ``base_url`` is user-supplied and the application attaches the stored
API key to every request, so pointing it at a private address would turn the
app into an SSRF primitive (cloud metadata, router admin pages, local services).

Rule: every address a host resolves to must be globally routable.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """Raised when a URL must not be used as an outbound LLM endpoint."""


async def _resolve(host: str) -> list[str]:
    """Return every address the host resolves to (IPv4 + IPv6)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _is_public(address: str) -> bool:
    """``is_global`` already excludes loopback, private, link-local, reserved
    and multicast ranges — including IPv4-mapped IPv6 forms."""
    return ipaddress.ip_address(address).is_global


async def ensure_public_base_url(url: str, *, require_dns: bool = True) -> None:
    """Validate that ``url`` points at a public host.

    ``require_dns=False`` (used when saving settings) treats an unresolvable
    host as acceptable so an offline machine can still store its config; the
    call-time check with ``require_dns=True`` remains authoritative.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError("base_url must be an http(s) URL")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("base_url must include a host")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = await _resolve(host)
        except OSError as exc:
            if require_dns:
                raise UnsafeURLError(f"无法解析主机 {host}") from exc
            return
    else:
        addresses = [host]

    if not addresses:
        if require_dns:
            raise UnsafeURLError(f"无法解析主机 {host}")
        return

    for address in addresses:
        if not _is_public(address):
            raise UnsafeURLError(f"base_url 不能指向内网/环回地址（{address}）")
