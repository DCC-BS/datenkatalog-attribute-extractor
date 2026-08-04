"""Tests for the URL policy that keeps the scraper off our own network."""

import socket

import pytest

from datenkatalog_attribute_extractor.services.web import url_policy
from datenkatalog_attribute_extractor.services.web.url_policy import UnsafeUrlError, ensure_safe_url


@pytest.fixture
def resolves_to(monkeypatch):
    """Pin name resolution to a fixed answer, so these tests need no DNS."""

    def _install(*addresses: str):
        def fake_resolve(host: str) -> list[str]:
            return list(addresses)

        monkeypatch.setattr(url_policy, "_resolve", fake_resolve)

    return _install


def _fail_resolution(monkeypatch) -> None:
    def raises(host: str) -> list[str]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(url_policy, "_resolve", raises)


async def test_public_address_is_allowed(resolves_to) -> None:
    resolves_to("93.184.216.34")

    await ensure_safe_url("https://example.org/formular")


@pytest.mark.parametrize(
    "scheme",
    ["file", "gopher", "ftp", "data", "javascript"],
)
async def test_non_http_schemes_are_rejected(scheme: str) -> None:
    with pytest.raises(UnsafeUrlError, match="only http and https"):
        await ensure_safe_url(f"{scheme}://example.org/formular")


async def test_url_without_a_host_is_rejected() -> None:
    with pytest.raises(UnsafeUrlError, match="no host"):
        await ensure_safe_url("https:///formular")


@pytest.mark.parametrize(
    ("label", "address"),
    [
        ("loopback", "127.0.0.1"),
        ("loopback range", "127.99.12.3"),
        ("rfc1918 ten", "10.0.0.5"),
        ("rfc1918 172", "172.16.4.9"),
        ("rfc1918 192", "192.168.1.1"),
        ("link local metadata", "169.254.169.254"),
        ("unspecified", "0.0.0.0"),
        ("ipv6 loopback", "::1"),
        ("ipv6 unique local", "fd00::1"),
        ("ipv6 link local", "fe80::1"),
        ("ipv4 mapped loopback", "::ffff:127.0.0.1"),
    ],
)
async def test_internal_addresses_are_rejected(resolves_to, label: str, address: str) -> None:
    resolves_to(address)

    with pytest.raises(UnsafeUrlError, match="internal address"):
        await ensure_safe_url("https://sneaky.example.org/formular")


async def test_a_host_resolving_to_both_public_and_internal_is_rejected(resolves_to) -> None:
    """One bad answer is enough: we cannot choose which address the scraper will use."""
    resolves_to("93.184.216.34", "127.0.0.1")

    with pytest.raises(UnsafeUrlError, match="internal address"):
        await ensure_safe_url("https://split-horizon.example.org/formular")


async def test_numeric_host_forms_are_caught_by_resolution(resolves_to) -> None:
    """http://2130706433/ is 127.0.0.1 written as a decimal integer; the resolver normalises it."""
    resolves_to("127.0.0.1")

    with pytest.raises(UnsafeUrlError, match="internal address"):
        await ensure_safe_url("http://2130706433/formular")


async def test_literal_internal_address_is_rejected(resolves_to) -> None:
    resolves_to("127.0.0.1")

    with pytest.raises(UnsafeUrlError, match="internal address"):
        await ensure_safe_url("http://127.0.0.1:8000/formular")


async def test_unresolvable_host_is_rejected(monkeypatch) -> None:
    _fail_resolution(monkeypatch)

    with pytest.raises(UnsafeUrlError, match="does not resolve"):
        await ensure_safe_url("https://nope.invalid/formular")


async def test_scheme_check_is_case_insensitive(resolves_to) -> None:
    resolves_to("93.184.216.34")

    await ensure_safe_url("HTTPS://example.org/formular")
