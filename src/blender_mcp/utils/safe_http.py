"""Outbound HTTP client with SSRF protection."""

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MiB
_MAX_ADDON_BYTES = 100 * 1024 * 1024     # 100 MiB

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::ffff:0:0/96"),
]


def _check_url(url: str, *, require_https: bool = True) -> None:
    """Raise ValueError if the URL is unsafe (private IP, wrong scheme, etc.)."""
    parsed = urlparse(url)
    if require_https and parsed.scheme != "https":
        raise ValueError(f"Only HTTPS URLs are allowed, got scheme: {parsed.scheme!r}")
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    try:
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for {host!r}: {exc}") from exc
    for _fam, _type, _proto, _canon, sockaddr in addrs:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        for net in _PRIVATE_NETS:
            if ip in net:
                raise ValueError(
                    f"URL {url!r} resolves to private/reserved IP {ip} — SSRF blocked"
                )


async def safe_get(
    url: str,
    *,
    require_https: bool = True,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    timeout: float = 30.0,
) -> httpx.Response:
    """GET url with SSRF protection and optional redirect validation."""
    _check_url(url, require_https=require_https)
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        resp = await client.get(url)
    if resp.is_redirect:
        redirect_url = resp.headers.get("location", "")
        _check_url(redirect_url, require_https=require_https)
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            resp = await client.get(redirect_url)
    content = resp.content
    if len(content) > max_bytes:
        raise ValueError(f"Response too large: {len(content)} > {max_bytes} bytes")
    return resp


async def safe_download_stream(
    url: str,
    dest_path: str,
    *,
    require_https: bool = True,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    timeout: float = 300.0,
) -> int:
    """Stream-download *url* to *dest_path*; return bytes written.

    Raises ValueError on SSRF, oversized response, or redirect to private IP.
    """
    _check_url(url, require_https=require_https)
    total = 0
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        async with client.stream("GET", url) as resp:
            if resp.is_redirect:
                redirect_url = resp.headers.get("location", "")
                _check_url(redirect_url, require_https=require_https)
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(
                            f"Download of {url!r} exceeded {max_bytes} bytes limit"
                        )
                    f.write(chunk)
    return total
