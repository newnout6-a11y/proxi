"""Persistent async HTTP/1.1 + HTTP/2 connection pool for universal upstream drivers.

Key safety invariants:
  - One cached httpx.AsyncClient pool per event loop and `(proxy_url, verify_tls, http2)` tuple,
    reusing TCP/TLS sockets across turns and keepwarm pings (saving 150-400 ms per request).
  - Redirects are NEVER followed (`follow_redirects=False`), preventing credential exfiltration on 3xx.
  - Link-local / cloud metadata addresses (169.254.0.0/16, fe80::/10, fd00:ec2::254) are blocked.
  - Plain HTTP is permitted only for loopback / private LAN addresses (Ollama, vLLM, LM Studio, LAN GPU hosts)
    unless `allow_insecure_http=True` is explicitly set on the provider.
  - Supports deterministic offline testing via `cfg["_http_transport"]` (an httpx.AsyncBaseTransport).
"""
from __future__ import annotations

import asyncio
import ipaddress
import urllib.parse
import weakref

import httpx

import apierr

DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_WRITE_TIMEOUT = 60.0
DEFAULT_POOL_TIMEOUT = 15.0
MAX_CONNECTIONS = 128
MAX_KEEPALIVE_CONNECTIONS = 64
KEEPALIVE_EXPIRY = 60.0

_CLOUD_METADATA_V6 = ipaddress.ip_network("fd00:ec2::254/128")
_LOOP_CLIENTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple, httpx.AsyncClient]] = (
    weakref.WeakKeyDictionary()
)


def is_local_or_private_host(host: str | None) -> bool:
    """True for localhost, .local/.lan, loopback, or RFC1918/RFC4193 private LAN addresses (never link-local metadata)."""
    if not host:
        return False
    lowered = host.strip().lower().strip("[]")
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".lan", ".internal")):
        return True
    try:
        addr = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    if addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr in _CLOUD_METADATA_V6:
        return False
    return addr.is_loopback or addr.is_private


def is_blocked_metadata_host(host: str | None) -> bool:
    """True for cloud metadata and link-local / unspecified literal IP addresses."""
    if not host:
        return True
    lowered = host.strip().lower().strip("[]")
    if lowered in ("metadata.google.internal", "metadata", "instance-data"):
        return True
    try:
        addr = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    if addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr in _CLOUD_METADATA_V6:
        return True
    return False


def validate_endpoint_url(url: str, *, allow_insecure_http: bool = False) -> str:
    """Validate an upstream HTTP(S) URL before sending credentials or requests."""
    if not isinstance(url, str) or not url.strip():
        raise apierr.service_unavailable("Upstream base URL is missing")
    cleaned = url.strip()
    if len(cleaned) > 2048 or any(ord(ch) < 33 or ord(ch) == 127 for ch in cleaned):
        raise apierr.service_unavailable("Upstream base URL contains invalid characters")
    try:
        parts = urllib.parse.urlsplit(cleaned)
        _ = parts.port
    except ValueError:
        raise apierr.service_unavailable("Upstream base URL or port is malformed") from None
    host = parts.hostname
    if not host or parts.scheme not in ("https", "http"):
        raise apierr.service_unavailable("Upstream base URL must use https:// or http://")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise apierr.service_unavailable("Upstream base URL must not contain credentials or fragments")
    if is_blocked_metadata_host(host):
        raise apierr.service_unavailable("Upstream base URL points to a restricted metadata/link-local address")
    if parts.scheme == "http" and not allow_insecure_http and not is_local_or_private_host(host):
        raise apierr.service_unavailable(
            "Plain http:// is only allowed for localhost/LAN providers (set allow_insecure_http=true to override)"
        )
    return cleaned


def validate_proxy_url(proxy_url: str | None) -> str | None:
    """Validate an optional HTTP/HTTPS/SOCKS5 proxy URL."""
    if not proxy_url:
        return None
    if not isinstance(proxy_url, str):
        raise ValueError("proxy_url must be a string")
    cleaned = proxy_url.strip()
    if not cleaned:
        return None
    if len(cleaned) > 1024 or any(ord(ch) < 33 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("proxy_url contains invalid characters")
    try:
        parts = urllib.parse.urlsplit(cleaned)
        _ = parts.port
    except ValueError:
        raise ValueError("proxy_url is malformed") from None
    if parts.scheme not in ("http", "https", "socks5", "socks5h") or not parts.hostname:
        raise ValueError("proxy_url must use http://, https://, socks5://, or socks5h://")
    if is_blocked_metadata_host(parts.hostname):
        raise ValueError("proxy_url points to a restricted address")
    return cleaned


def make_timeout(cfg: dict, *, read_timeout: float | None = None) -> httpx.Timeout:
    """Build per-request httpx.Timeout where read_timeout covers silence between chunks."""
    connect = float(cfg.get("connect_timeout") or DEFAULT_CONNECT_TIMEOUT)
    read = float(read_timeout) if read_timeout and read_timeout > 0 else float(cfg.get("turn_timeout") or 900.0)
    return httpx.Timeout(
        connect=max(1.0, connect),
        read=max(1.0, read),
        write=DEFAULT_WRITE_TIMEOUT,
        pool=DEFAULT_POOL_TIMEOUT,
    )


def get_async_client(cfg: dict | None = None) -> httpx.AsyncClient:
    """Return a shared, persistent httpx.AsyncClient bound to the current asyncio loop."""
    cfg = cfg or {}
    custom_transport = cfg.get("_http_transport")
    loop = asyncio.get_running_loop()
    table = _LOOP_CLIENTS.get(loop)
    if table is None:
        table = {}
        _LOOP_CLIENTS[loop] = table

    proxy_url = validate_proxy_url(cfg.get("proxy_url"))
    verify_tls = bool(cfg.get("verify_tls", True))
    http2 = bool(cfg.get("http2", False))
    key = ("transport", id(custom_transport)) if custom_transport is not None else (proxy_url, verify_tls, http2)

    client = table.get(key)
    if client is not None and not client.is_closed:
        return client

    limits = httpx.Limits(
        max_connections=max(16, int(cfg.get("max_workers", 32)) * 2),
        max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=KEEPALIVE_EXPIRY,
    )
    kwargs: dict = {
        "follow_redirects": False,
        "limits": limits,
        "trust_env": bool(cfg.get("use_system_proxy", False)) and not proxy_url and custom_transport is None,
    }
    if custom_transport is not None:
        kwargs["transport"] = custom_transport
    else:
        kwargs["verify"] = verify_tls
        kwargs["http2"] = http2
        if proxy_url:
            kwargs["proxy"] = proxy_url

    client = httpx.AsyncClient(**kwargs)
    table[key] = client
    return client


async def close_loop_clients():
    """Close all pooled httpx.AsyncClient instances registered on the current asyncio event loop."""
    loop = asyncio.get_running_loop()
    table = _LOOP_CLIENTS.pop(loop, None)
    if not table:
        return
    clients = [client for client in table.values() if not client.is_closed]
    if clients:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
