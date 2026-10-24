"""Outbound endpoint guard: the stored API key must never go to a private host."""
from __future__ import annotations

import pytest

from app.services import net_guard
from app.services.net_guard import UnsafeURLError, ensure_public_base_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/v1",
        "http://127.1.2.3/v1",
        "http://10.0.0.5/v1",
        "http://192.168.1.10/v1",
        "http://172.16.0.1/v1",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://0.0.0.0/v1",
        "http://[::1]:8000/v1",
        "http://[fd00::1]/v1",
        "http://[::ffff:127.0.0.1]/v1",
    ],
)
async def test_private_literal_addresses_are_rejected(url):
    with pytest.raises(UnsafeURLError):
        await ensure_public_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/v1",
        "http://metadata.google.internal/v1",
    ],
)
async def test_hostnames_resolving_to_private_addresses_are_rejected(url, monkeypatch):
    async def _private(host: str) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr(net_guard, "_resolve", _private)
    with pytest.raises(UnsafeURLError):
        await ensure_public_base_url(url)


async def test_hostname_with_mixed_public_and_private_answers_is_rejected(monkeypatch):
    async def _mixed(host: str) -> list[str]:
        return ["93.184.216.34", "10.0.0.7"]

    monkeypatch.setattr(net_guard, "_resolve", _mixed)
    with pytest.raises(UnsafeURLError):
        await ensure_public_base_url("https://llm.example.test/v1")


async def test_public_hostname_is_allowed(monkeypatch):
    async def _public(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", _public)
    await ensure_public_base_url("https://llm.example.test/v1")


async def test_dns_failure_blocks_at_call_time_but_not_on_save(monkeypatch):
    async def _fail(host: str) -> list[str]:
        raise OSError("name resolution failed")

    monkeypatch.setattr(net_guard, "_resolve", _fail)
    with pytest.raises(UnsafeURLError):
        await ensure_public_base_url("https://offline.example/v1", require_dns=True)

    # Saving must stay possible on a machine without DNS.
    await ensure_public_base_url("https://offline.example/v1", require_dns=False)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.test/v1", "not-a-url"])
async def test_non_http_schemes_are_rejected(url):
    with pytest.raises(UnsafeURLError):
        await ensure_public_base_url(url)
