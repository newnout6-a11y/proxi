"""Per-account curl_cffi Session/CurlShare pool and browser-consistent HTTP transport.

Used by:
  - Minara (`minara.py`, `accounts.py`, `minara_probe.py`) for aligned TLS/HTTP2 impersonation,
    Fetch/CORS headers (`default_headers=False`), connection reuse, and per-account `proxy_url`.
  - Retool (`direct.py`, `upstream_refresh.py`, `credits.py`, `session.py`, `retool_refresh.py`)
    as a drop-in replacement for `urllib.request.urlopen` that keeps BoringSSL JA4/HTTP2,
    `Accept-Encoding: gzip, deflate, br, zstd`, keep-alive pooling, and per-account proxies,
    while delegating automatically when a test monkeypatches `urllib.request.urlopen`.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import queue
import re
import threading
import urllib.error
import urllib.request

from drivers import http_pool

DEFAULT_IMPERSONATE = "chrome152"
DEFAULT_ACCEPT_ENCODING = "gzip, deflate, br, zstd"
MAX_SLOTS_PER_BUCKET = 8
MAX_ERROR_BYTES = 64 * 1024

_ORIG_URLOPEN = urllib.request.urlopen
_CHROME_MAJOR_RE = re.compile(r"\bChrome/(\d+)")
_FIREFOX_MAJOR_RE = re.compile(r"\bFirefox/(\d+)")
_PROFILE_RE = re.compile(r"^(chrome|edge|firefox|safari)(\d+)(?:a)?(_macos|_android|_ios)?$")


def _supported_profiles() -> frozenset[str]:
    from curl_cffi.requests import BrowserType

    return frozenset(member.value for member in BrowserType)


def _newest_chrome(macos: bool = False) -> str:
    profiles = _supported_profiles()
    suffix = "_macos" if macos else ""
    best_ver = -1
    best_name = DEFAULT_IMPERSONATE
    for name in profiles:
        m = _PROFILE_RE.fullmatch(name)
        if not m or m.group(1) != "chrome" or (m.group(3) or "") != suffix:
            continue
        ver = int(m.group(2))
        if ver > best_ver:
            best_ver = ver
            best_name = name
    return best_name if best_ver >= 0 else DEFAULT_IMPERSONATE


def validate_impersonate(value: str | None) -> str | None:
    """Validate an explicit `impersonate` value (`'auto'` or a `curl_cffi` `BrowserType`)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("impersonate must be a string")
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned == "auto":
        return "auto"
    if cleaned not in _supported_profiles():
        raise ValueError(f"Unsupported impersonate profile '{cleaned}'")
    return cleaned


def resolve_profile(cfg: dict | None) -> str:
    """Resolve the concrete `curl_cffi` impersonation profile for `cfg`."""
    cfg = cfg or {}
    raw = cfg.get("impersonate")
    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        if cleaned and cleaned != "auto":
            if cleaned in _supported_profiles():
                return cleaned
            return DEFAULT_IMPERSONATE

    if isinstance(raw, str) and raw.strip().lower() == "auto":
        ua = str(cfg.get("ua") or "")
        profiles = _supported_profiles()
        macos = "Macintosh" in ua or "Mac OS X" in ua
        m = _CHROME_MAJOR_RE.search(ua)
        if m:
            candidate = f"chrome{m.group(1)}{'_macos' if macos else ''}"
            if candidate in profiles:
                return candidate
            if macos and f"chrome{m.group(1)}" in profiles:
                return f"chrome{m.group(1)}"
            return _newest_chrome(macos=macos)
        fm = _FIREFOX_MAJOR_RE.search(ua)
        if fm:
            candidate = f"firefox{fm.group(1)}"
            if candidate in profiles:
                return candidate
            ff = sorted((p for p in profiles if p.startswith("firefox")), key=lambda s: int(re.sub(r"\D", "", s) or 0))
            if ff:
                return ff[-1]
    return DEFAULT_IMPERSONATE


def profile_user_agent(profile: str) -> str:
    """Return a browser User-Agent string consistent with `profile`."""
    m = _PROFILE_RE.fullmatch((profile or "").lower())
    if not m:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
        )
    family, ver, suffix = m.group(1), m.group(2), m.group(3) or ""
    if family == "firefox":
        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{ver}.0) Gecko/20100101 Firefox/{ver}.0"
    if family == "safari":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"
        )
    if family == "edge":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36 Edg/{ver}.0.0.0"
        )
    if suffix == "_macos":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
        )
    if suffix == "_android":
        return (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Mobile Safari/537.36"
        )
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
    )


def profile_client_hints(profile: str) -> dict[str, str]:
    """Return Chromium `sec-ch-ua*` headers matching `profile`, or `{}` for Firefox/Safari."""
    m = _PROFILE_RE.fullmatch((profile or "").lower())
    if not m or m.group(1) not in ("chrome", "edge"):
        return {}
    family, ver, suffix = m.group(1), m.group(2), m.group(3) or ""
    platform = '"macOS"' if suffix == "_macos" else ('"Android"' if suffix == "_android" else '"Windows"')
    mobile = "?1" if suffix == "_android" else "?0"
    brand = "Microsoft Edge" if family == "edge" else "Google Chrome"
    return {
        "sec-ch-ua": f'"Chromium";v="{ver}", "Not?A_Brand";v="24", "{brand}";v="{ver}"',
        "sec-ch-ua-mobile": mobile,
        "sec-ch-ua-platform": platform,
    }


def minara_headers(
    cfg: dict | None,
    *,
    accept: str = "application/json",
    content_type: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build Fetch/CORS headers aligned with `resolve_profile(cfg)` for Minara endpoints."""
    profile = resolve_profile(cfg)
    headers: dict[str, str] = {}
    headers.update(profile_client_hints(profile))
    headers["user-agent"] = profile_user_agent(profile)
    headers["accept"] = accept
    if content_type:
        headers["content-type"] = content_type
    headers["sec-fetch-site"] = "same-site"
    headers["sec-fetch-mode"] = "cors"
    headers["sec-fetch-dest"] = "empty"
    headers["accept-language"] = "en-US,en;q=0.9"
    headers["priority"] = "u=1, i"
    if extra:
        for k, v in extra.items():
            # Preserve exact casing expected by upstream/tests (e.g. Authorization, anthropic-version)
            # while avoiding duplicate case-variants alongside the defaults above.
            lowered = k.lower()
            for existing in list(headers):
                if existing.lower() == lowered:
                    del headers[existing]
            headers[k] = v
    return headers


def account_scope(cfg: dict | None) -> str:
    """Stable per-account bucket identifier so distinct accounts never share sockets or TLS tickets."""
    if not isinstance(cfg, dict):
        return ""
    explicit = cfg.get("account_id") or cfg.get("page_uuid")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    secret = cfg.get("bearer_token") or cfg.get("cookie") or cfg.get("api_key")
    if isinstance(secret, str) and secret.strip():
        return "tok:" + hashlib.sha256(secret.strip().encode("utf-8", "replace")).hexdigest()[:16]
    label = cfg.get("label")
    return f"lbl:{label}" if isinstance(label, str) and label.strip() else ""


class _Slot:
    __slots__ = ("session", "share", "key", "generation", "closed")

    def __init__(self, session, share, key, generation: int = 0):
        self.session = session
        self.share = share
        self.key = key
        self.generation = generation
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            self.session.close()
        with contextlib.suppress(Exception):
            self.share.close()


class _Lease:
    __slots__ = ("pool", "slot", "reusable", "released")

    def __init__(self, pool: "AccountSessionPool", slot: _Slot):
        self.pool = pool
        self.slot = slot
        self.reusable = True
        self.released = False

    @property
    def session(self):
        return self.slot.session

    def discard(self):
        self.reusable = False

    def release(self, reusable: bool | None = None):
        if self.released:
            return
        self.released = True
        keep = self.reusable if reusable is None else (self.reusable and reusable)
        self.pool._release(self.slot, reusable=keep)


class AccountSessionPool:
    """Thread-safe checkout pool of `(curl_cffi.Session, CurlShare)` isolated per account and proxy."""

    def __init__(self, max_per_bucket: int = MAX_SLOTS_PER_BUCKET):
        self._max_per_bucket = max(1, int(max_per_bucket))
        self._lock = threading.Lock()
        self._buckets: dict[tuple, queue.LifoQueue] = {}
        self._generation = 0

    def _bucket_key(self, cfg: dict | None) -> tuple:
        cfg = cfg or {}
        proxy_url = http_pool.validate_proxy_url(cfg.get("proxy_url"))
        profile = resolve_profile(cfg)
        verify_tls = bool(cfg.get("verify_tls", True))
        trust_env = bool(cfg.get("use_system_proxy", True)) and not proxy_url
        return (account_scope(cfg), proxy_url or "", profile, verify_tls, trust_env)

    def _make_slot(self, key: tuple, generation: int) -> _Slot:
        from curl_cffi import CurlShare, requests

        _, proxy_url, profile, verify_tls, trust_env = key
        # Each slot is held by at most one thread at a time via checkout, so
        # CurlShare(connect=True) safely caches keep-alive / H2 sockets across turns.
        share = CurlShare(connect=True, dns=True, ssl_session=True)
        try:
            session = requests.Session(
                impersonate=profile,
                default_headers=False,
                discard_cookies=True,
                use_thread_local_curl=False,
                curl_share=share,
                proxy=proxy_url or None,
                verify=verify_tls,
                trust_env=trust_env,
                allow_redirects=False,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                share.close()
            raise
        return _Slot(session, share, key, generation)

    def acquire(self, cfg: dict | None = None) -> _Lease:
        key = self._bucket_key(cfg)
        with self._lock:
            gen = self._generation
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = queue.LifoQueue(maxsize=self._max_per_bucket)
                self._buckets[key] = bucket
        while True:
            try:
                slot = bucket.get_nowait()
            except queue.Empty:
                return _Lease(self, self._make_slot(key, gen))
            if not slot.closed and slot.generation == gen:
                return _Lease(self, slot)
            slot.close()

    def _release(self, slot: _Slot, *, reusable: bool) -> None:
        if not reusable or slot.closed:
            slot.close()
            return
        with self._lock:
            bucket = self._buckets.get(slot.key) if slot.generation == self._generation else None
        if bucket is None:
            slot.close()
            return
        try:
            bucket.put_nowait(slot)
        except queue.Full:
            slot.close()

    @contextlib.contextmanager
    def checkout(self, cfg: dict | None = None):
        lease = self.acquire(cfg)
        try:
            yield lease
        except BaseException:
            lease.discard()
            raise
        finally:
            lease.release()

    def close_account(self, scope: str) -> None:
        if not scope:
            return
        drained = []
        with self._lock:
            keys = [k for k in self._buckets if k[0] == scope]
            for key in keys:
                bucket = self._buckets.pop(key)
                while True:
                    try:
                        drained.append(bucket.get_nowait())
                    except queue.Empty:
                        break
        for slot in drained:
            slot.close()

    def close_all(self) -> None:
        """Drain and close all idle slots and invalidate in-flight leases without disabling future acquires."""
        drained = []
        with self._lock:
            self._generation += 1
            for bucket in self._buckets.values():
                while True:
                    try:
                        drained.append(bucket.get_nowait())
                    except queue.Empty:
                        break
            self._buckets.clear()
        for slot in drained:
            slot.close()


POOL = AccountSessionPool()


def close_account(cfg_or_scope) -> None:
    scope = cfg_or_scope if isinstance(cfg_or_scope, str) else account_scope(cfg_or_scope)
    POOL.close_account(scope)


def close_all() -> None:
    POOL.close_all()


def _to_http_message(raw_headers) -> http.client.HTTPMessage:
    msg = http.client.HTTPMessage()
    items = raw_headers.multi_items() if hasattr(raw_headers, "multi_items") else raw_headers.items()
    for name, value in items:
        msg.add_header(str(name), str(value))
    return msg


def _is_timeout_exc(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError) or getattr(exc, "code", None) == 28:
        return True
    try:
        from curl_cffi.requests.exceptions import Timeout as CurlTimeout
        if isinstance(exc, CurlTimeout):
            return True
    except ImportError:
        pass
    return False


class _BufferedResponse:
    """Buffered non-streaming response adapter matching `http.client.HTTPResponse`."""

    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes):
        self.status = status
        self.headers = headers
        self._bio = io.BytesIO(body)

    def read(self, amt: int | None = None) -> bytes:
        return self._bio.read() if amt is None else self._bio.read(amt)

    def __iter__(self):
        return iter(self._bio)

    def close(self) -> None:
        self._bio.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class _StreamResponse:
    """Streaming line/byte adapter over `curl_cffi` `Response` with pooled lease return."""

    def __init__(self, response, headers: http.client.HTTPMessage, lease: _Lease, *, read_timeout: float | None = None):
        self._response = response
        self.status = response.status_code
        self.headers = headers
        self.quit_now = getattr(response, "quit_now", None)
        self._lease = lease
        self._buf = bytearray()
        self._queue = getattr(response, "queue", None)
        self._read_timeout = float(read_timeout) if (read_timeout is not None and float(read_timeout) > 0) else None
        self._chunks = None if self._queue is not None else response.iter_content(chunk_size=None)
        self._eof = False
        self._errored = False
        self._aborted = False
        self._closed = False
        self._close_lock = threading.Lock()

    def _fill_chunk(self) -> bool:
        if self._eof:
            return False
        if self._queue is not None:
            from curl_cffi.requests.models import STREAM_END

            try:
                chunk = self._queue.get(timeout=self._read_timeout)
            except queue.Empty:
                self._errored = True
                self.abort()
                raise TimeoutError("upstream stream read timed out") from None
            if chunk is STREAM_END:
                self._eof = True
                return False
            if isinstance(chunk, BaseException):
                self._errored = True
                if _is_timeout_exc(chunk):
                    raise TimeoutError("upstream stream read timed out") from None
                raise OSError(f"upstream stream read failed: {type(chunk).__name__}") from None
            if chunk:
                self._buf.extend(chunk)
            return True
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._eof = True
            return False
        except BaseException as exc:
            self._errored = True
            if _is_timeout_exc(exc):
                raise TimeoutError("upstream stream read timed out") from None
            raise OSError(f"upstream stream read failed: {type(exc).__name__}") from None
        if chunk:
            self._buf.extend(chunk)
        return True

    def __iter__(self):
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[: nl + 1])
                del self._buf[: nl + 1]
                yield line
                continue
            if self._eof:
                if self._buf:
                    tail = bytes(self._buf)
                    self._buf.clear()
                    yield tail
                return
            self._fill_chunk()

    def read(self, amt: int | None = None) -> bytes:
        if amt is None or amt < 0:
            while self._fill_chunk():
                pass
            data = bytes(self._buf)
            self._buf.clear()
            return data
        while len(self._buf) < amt and not self._eof:
            self._fill_chunk()
        data = bytes(self._buf[:amt])
        del self._buf[:amt]
        return data

    def abort(self) -> None:
        self._aborted = True
        quit_now = self.quit_now
        if quit_now is not None:
            quit_now.set()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        reusable = bool(self._eof and not self._errored and not self._aborted)
        stream_task = getattr(self._response, "stream_task", None)
        if not reusable and stream_task is not None and not stream_task.done():
            self.abort()
            with contextlib.suppress(Exception):
                self._response._stream_closed = True
            resp = self._response
            slot = self._lease.slot
            self._lease.discard()
            self._lease.released = True

            def _cleanup_after_stream(fut) -> None:
                with contextlib.suppress(Exception):
                    fut.result()
                with contextlib.suppress(Exception):
                    curl_handle = getattr(resp, "curl", None)
                    if curl_handle is not None:
                        curl_handle.close()
                slot.close()

            stream_task.add_done_callback(_cleanup_after_stream)
            return
        try:
            chunks_close = getattr(self._chunks, "close", None)
            if callable(chunks_close):
                chunks_close()
            self._response.close()
        except Exception:
            reusable = False
        finally:
            self._lease.release(reusable=reusable)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def retool_open(
    cfg: dict | None,
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    timeout: float = 30.0,
    stream: bool = False,
):
    """Send an HTTP request for Retool via `curl_cffi` (or `urllib` when monkeypatched or configured)."""
    cfg = cfg or {}
    if urllib.request.urlopen is not _ORIG_URLOPEN or cfg.get("retool_http_backend") == "urllib":
        req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
        return urllib.request.urlopen(req, timeout=timeout)

    lease = POOL.acquire(cfg)
    transferred = False
    try:
        try:
            req_timeout = (min(float(timeout), 20.0), float(timeout)) if stream else float(timeout)
            resp = lease.session.request(
                method,
                url,
                data=data,
                headers=headers,
                timeout=req_timeout,
                stream=stream,
                allow_redirects=False,
                default_headers=False,
                discard_cookies=True,
                accept_encoding=DEFAULT_ACCEPT_ENCODING,
            )
        except Exception as exc:
            lease.discard()
            if _is_timeout_exc(exc):
                raise TimeoutError("upstream request timed out") from None
            raise urllib.error.URLError(f"upstream connection failed: {type(exc).__name__}") from None

        http_headers = _to_http_message(resp.headers)
        if resp.status_code >= 400:
            err_buf = bytearray()
            reusable = True
            try:
                if stream:
                    for chunk in resp.iter_content(chunk_size=None):
                        if chunk:
                            need = MAX_ERROR_BYTES - len(err_buf)
                            if need <= 0:
                                reusable = False
                                break
                            err_buf.extend(chunk[:need])
                            if len(err_buf) >= MAX_ERROR_BYTES:
                                reusable = False
                                break
                else:
                    err_buf.extend((resp.content or b"")[:MAX_ERROR_BYTES])
            except Exception:
                reusable = False
            finally:
                with contextlib.suppress(Exception):
                    resp.close()
                lease.release(reusable=reusable)
            transferred = True
            raise urllib.error.HTTPError(
                url, resp.status_code, f"HTTP {resp.status_code}", http_headers, io.BytesIO(bytes(err_buf))
            )

        if not stream:
            try:
                body = resp.content or b""
            except Exception as exc:
                lease.discard()
                with contextlib.suppress(Exception):
                    resp.close()
                if _is_timeout_exc(exc):
                    raise TimeoutError("upstream response read timed out") from None
                raise OSError(f"upstream response read failed: {type(exc).__name__}") from None
            with contextlib.suppress(Exception):
                resp.close()
            lease.release(reusable=True)
            transferred = True
            return _BufferedResponse(resp.status_code, http_headers, body)

        transferred = True
        return _StreamResponse(resp, http_headers, lease, read_timeout=float(timeout))
    finally:
        if not transferred:
            lease.release(reusable=False)
