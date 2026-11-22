"""Persistent async HTTP/1.1 + HTTP/2 connection pool for universal upstream drivers.

Key safety invariants:
  - One cached httpx.AsyncClient pool per event loop and `(proxy_url, verify_tls, http2, trust_env)` tuple,
    reusing TCP/TLS sockets across turns and keepwarm pings (saving 150-400 ms per request).
  - Redirects are NEVER followed (`follow_redirects=False`), preventing credential exfiltration on 3xx.
  - Upstream Set-Cookie is not shared across credentials. Explicit per-request Cookie headers still pass through.
  - Link-local / cloud metadata addresses (169.254.0.0/16, fe80::/10, fd00:ec2::254) are blocked.
  - Plain HTTP is permitted only for loopback / private LAN addresses (Ollama, vLLM, LM Studio, LAN GPU hosts)
    unless `allow_insecure_http=True` is explicitly set on the provider.
  - Supports deterministic offline testing via `cfg["_http_transport"]` (an httpx.AsyncBaseTransport).
"""
from __future__ import annotations

import asyncio
import contextlib
from http.cookiejar import CookieJar, DefaultCookiePolicy
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
_PER_ACCOUNT_KEEPALIVE = 32

_CLOUD_METADATA_V6 = ipaddress.ip_network("fd00:ec2::254/128")
_LOOP_CLIENTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple, httpx.AsyncClient]] = (
    weakref.WeakKeyDictionary()
)
_BG_TASKS: set[asyncio.Task] = set()


class _NoResponseCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


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
    if len(cleaned) > 1024 or any(ord(ch) < 33 or ord(ch) >= 127 for ch in cleaned):
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


def _account_scope(cfg: dict) -> str:
    """Stable per-account/provider pool key so distinct API keys do not share TCP/TLS/H2 sockets."""
    explicit = cfg.get("account_id") or cfg.get("provider_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return ""


def get_async_client(cfg: dict | None = None) -> httpx.AsyncClient:
    """Return a persistent httpx.AsyncClient bound to the current asyncio loop and account scope."""
    cfg = cfg or {}
    custom_transport = cfg.get("_http_transport")
    loop = asyncio.get_running_loop()
    table = _LOOP_CLIENTS.get(loop)
    if table is None:
        table = {}
        _LOOP_CLIENTS[loop] = table

    proxy_url = validate_proxy_url(cfg.get("proxy_url"))
    verify_tls = bool(cfg.get("verify_tls", True))
    http2 = bool(cfg.get("http2", True))
    trust_env = bool(cfg.get("use_system_proxy", False)) and not proxy_url and custom_transport is None
    scope = _account_scope(cfg)
    key = (
        ("transport", id(custom_transport))
        if custom_transport is not None
        else (scope, proxy_url, verify_tls, http2, trust_env)
    )

    client = table.get(key)
    if client is not None and not client.is_closed:
        return client

    limits = httpx.Limits(
        max_connections=max(16, int(cfg.get("max_workers", 32)) * (1 if scope else 2)),
        max_keepalive_connections=_PER_ACCOUNT_KEEPALIVE if scope else MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=KEEPALIVE_EXPIRY,
    )
    kwargs: dict = {
        "follow_redirects": False,
        "cookies": CookieJar(policy=_NoResponseCookies()),
        "limits": limits,
        "trust_env": trust_env,
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


def _is_http2_response(resp) -> bool:
    """True when `resp` shares a multiplexed HTTP/2 transport stream."""
    if getattr(resp, "http_version", None) == "HTTP/2":
        return True
    ext = getattr(resp, "extensions", None)
    return isinstance(ext, dict) and (ext.get("http_version") == b"HTTP/2" or "stream_id" in ext)


async def _close_stream_response(resp) -> None:
    if not _is_http2_response(resp):
        with contextlib.suppress(Exception):
            ext = getattr(resp, "extensions", None)
            ns = ext.get("network_stream") if isinstance(ext, dict) else None
            if ns is not None:
                await ns.aclose()
    with contextlib.suppress(Exception):
        await resp.aclose()


def _schedule_bg(loop: asyncio.AbstractEventLoop, coro_fn) -> None:
    if loop.is_closed():
        return

    def _spawn():
        task = loop.create_task(coro_fn())
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)

    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(_spawn)


def make_stream_abort(session, active_resp_holder: dict, loop: asyncio.AbstractEventLoop | None = None):
    """Build a thread-safe abort callback that closes an in-flight httpx stream immediately."""
    active_loop = loop or asyncio.get_running_loop()
    active_resp_holder["loop"] = active_loop
    active_resp_holder["aborted"] = False
    active_resp_holder["abort_event"] = asyncio.Event()

    def abort():
        active_resp_holder["aborted"] = True
        abort_event = active_resp_holder.get("abort_event")
        if abort_event is not None and not active_loop.is_closed():
            with contextlib.suppress(RuntimeError):
                active_loop.call_soon_threadsafe(abort_event.set)
        resp = active_resp_holder.get("resp")
        if resp is None:
            return
        _schedule_bg(active_loop, lambda r=resp: _close_stream_response(r))

    if session is not None:
        session._direct_reply = None
        session._active_direct_abort = abort
    return abort


def bind_stream_response(active_resp_holder: dict, resp) -> None:
    """Attach the opened httpx stream response and close it immediately if abort raced ahead."""
    active_resp_holder["resp"] = resp
    if active_resp_holder.get("aborted"):
        loop = active_resp_holder.get("loop")
        if loop is not None:
            _schedule_bg(loop, lambda r=resp: _close_stream_response(r))


def clear_stream_abort(session, abort) -> None:
    if session is not None and getattr(session, "_active_direct_abort", None) is abort:
        session._active_direct_abort = None


@contextlib.asynccontextmanager
async def abortable_stream(client: httpx.AsyncClient, method: str, url: str, *, active_resp_holder: dict, **kwargs):
    """Open an `httpx` stream that cancels promptly on `abort` without closing shared HTTP/2 sockets."""
    abort_event = active_resp_holder.get("abort_event")
    ctx = client.stream(method, url, **kwargs)
    if abort_event is None:
        async with ctx as resp:
            bind_stream_response(active_resp_holder, resp)
            yield resp
        return
    if abort_event.is_set() or active_resp_holder.get("aborted"):
        raise httpx.ConnectError("Upstream stream aborted before connect")
    enter_task = asyncio.ensure_future(ctx.__aenter__())
    abort_task = asyncio.ensure_future(abort_event.wait())
    entered = False
    try:
        try:
            done, _ = await asyncio.wait((enter_task, abort_task), return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            abort_task.cancel()
            enter_task.cancel()
            await asyncio.gather(enter_task, abort_task, return_exceptions=True)
            if enter_task.done() and not enter_task.cancelled() and enter_task.exception() is None:
                entered = True
            raise
        if abort_task in done and (abort_event.is_set() or active_resp_holder.get("aborted")):
            enter_task.cancel()
            await asyncio.gather(enter_task, return_exceptions=True)
            if enter_task.done() and not enter_task.cancelled() and enter_task.exception() is None:
                entered = True
            raise httpx.ReadError("Upstream stream aborted")
        abort_task.cancel()
        await asyncio.gather(abort_task, return_exceptions=True)
        resp = enter_task.result()
        entered = True
        bind_stream_response(active_resp_holder, resp)
        if abort_event.is_set() or active_resp_holder.get("aborted"):
            raise httpx.ReadError("Upstream stream aborted")
        yield resp
    except BaseException as exc:
        if entered:
            await ctx.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        await ctx.__aexit__(None, None, None)


async def iter_abortable_bytes(resp, active_resp_holder: dict):
    """Yield response byte chunks while waking immediately when `abort` sets `abort_event`."""
    abort_event = active_resp_holder.get("abort_event")
    if abort_event is None:
        async for chunk in resp.aiter_bytes():
            yield chunk
        return
    it = resp.aiter_bytes().__aiter__()
    while True:
        if abort_event.is_set() or active_resp_holder.get("aborted"):
            raise httpx.ReadError("Upstream stream aborted")
        chunk_task = asyncio.ensure_future(it.__anext__())
        abort_task = asyncio.ensure_future(abort_event.wait())
        try:
            done, _ = await asyncio.wait((chunk_task, abort_task), return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            chunk_task.cancel()
            abort_task.cancel()
            await asyncio.gather(chunk_task, abort_task, return_exceptions=True)
            raise
        if abort_task in done and (abort_event.is_set() or active_resp_holder.get("aborted")):
            chunk_task.cancel()
            await asyncio.gather(chunk_task, return_exceptions=True)
            raise httpx.ReadError("Upstream stream aborted")
        abort_task.cancel()
        await asyncio.gather(abort_task, return_exceptions=True)
        try:
            yield chunk_task.result()
        except StopAsyncIteration:
            break


def is_presend_httpx_error(exc: BaseException) -> bool:
    """True only for httpx errors that provably occurred before sending the request."""
    return isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError, httpx.ProxyError))


def map_transport_error(exc: BaseException, label: str) -> apierr.ApiError:
    """Map httpx transport timeouts/errors to `ApiError` with conservative `unbilled` marking."""
    if isinstance(exc, httpx.TimeoutException):
        err = apierr.upstream_timeout(f"{label} request timed out")
    else:
        err = apierr.upstream_error(f"{label} connection error: {type(exc).__name__}")
    err.unbilled = is_presend_httpx_error(exc)
    return err


def close_account_clients(account_id: str | None) -> None:
    """Close pooled httpx.AsyncClient instances belonging to `account_id` across active loops."""
    if not isinstance(account_id, str) or not account_id.strip():
        return
    scope = account_id.strip()
    for loop, table in list(_LOOP_CLIENTS.items()):
        keys = [k for k in list(table) if len(k) >= 5 and k[0] == scope]
        clients = []
        for k in keys:
            client = table.pop(k, None)
            if client is not None and not client.is_closed:
                clients.append(client)
        if clients and not loop.is_closed():
            for client in clients:
                _schedule_bg(loop, client.aclose)


async def close_loop_clients():
    """Close all pooled httpx.AsyncClient instances registered on the current asyncio event loop."""
    loop = asyncio.get_running_loop()
    table = _LOOP_CLIENTS.pop(loop, None)
    if not table:
        return
    clients = [client for client in table.values() if not client.is_closed]
    if clients:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
