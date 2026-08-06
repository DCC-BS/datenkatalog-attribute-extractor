"""Refuses to scrape URLs that point back into our own infrastructure.

The URL comes from whoever is using the app, and the request is made from inside the
cluster. Without a check, `http://169.254.169.254/latest/meta-data/` or a cluster-internal
service name is as scrapeable as a public form, and the result comes back in the response.

The check happens before the browser is called at all. The browser runs in its own container and
may well have protections of its own, but relying on those means trusting a component's
configuration to enforce our policy.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

import anyio.to_thread
from dcc_backend_common.logger import get_logger

logger = get_logger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrlError(ValueError):
    """Raised when a URL may not be scraped."""

    def __init__(self, url: str, reason: str) -> None:
        """Initialise the error.

        Args:
            url: The URL that was rejected.
            reason: Why it was rejected, phrased for an API caller.
        """
        super().__init__(f"The URL '{url}' cannot be scraped: {reason}")
        self.url = url
        self.reason = reason


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Report whether an address is somewhere we refuse to send a scraper.

    IPv4-mapped IPv6 addresses are unwrapped first: `::ffff:127.0.0.1` is loopback, but only
    the wrapped IPv4 address says so.
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped

    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _resolve(host: str) -> list[str]:
    """Resolve a hostname to every address it currently answers with.

    Returns:
        The resolved addresses as strings.

    Raises:
        socket.gaierror: If the name does not resolve.
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    # sockaddr is (address, port) for IPv4 and (address, port, flowinfo, scope_id) for IPv6,
    # so the tuple types as str | int overall even though element 0 is always the address.
    return [str(info[4][0]) for info in infos]


async def ensure_safe_url(url: str) -> None:
    """Reject a URL that is not a public web address.

    Every address the hostname resolves to is checked, not just the literal in the URL: a
    public name whose A record points at 127.0.0.1 is the ordinary way around a check that
    only reads the string. Numeric host forms — `http://2130706433/` and the like — are
    covered by the same resolution step, since the resolver normalises them.

    This cannot see where a redirect leads. The browser follows redirects itself, so a URL that
    passes here may still end up fetching somewhere else; that residual risk is why the browser
    should not be able to reach anything sensitive in the first place.

    Args:
        url: The URL the caller asked to scrape.

    Raises:
        UnsafeUrlError: If the scheme is not http(s), the URL has no host, the host does not
            resolve, or any resolved address is private, loopback, link-local or reserved.
    """
    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(url, f"only http and https are supported, not '{parts.scheme}'")

    host = parts.hostname
    if not host:
        raise UnsafeUrlError(url, "no host in the URL")

    try:
        addresses = await anyio.to_thread.run_sync(_resolve, host)
    except socket.gaierror as error:
        raise UnsafeUrlError(url, f"the host '{host}' does not resolve") from error

    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:  # pragma: no cover - getaddrinfo returns parseable addresses
            continue
        if _is_blocked_address(address):
            logger.warning("url_rejected_as_internal", url=url, host=host, address=raw)
            raise UnsafeUrlError(url, f"the host '{host}' resolves to the internal address {raw}")
