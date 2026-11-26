"""Live USD balance & quota checkers for universal providers (OpenRouter, DeepSeek, Moonshot, SiliconFlow) + unmetered USD."""
from __future__ import annotations

import math
import threading
import time

import httpx

from accounts import UnmeteredAccount
from catalog import discovery
from drivers import http_pool

CACHE_SECONDS = 30.0
CNY_TO_USD_FALLBACK = 0.14  # used only when DeepSeek / Moonshot reports CNY without a USD entry


def _parse_balance_response(preset: str, url: str, payload: dict) -> dict:
    """Parse provider-specific JSON balance responses into `{available, monthly_allocation, currency, unit}`."""
    low_url = (url or "").lower()
    low_preset = (preset or "").lower()

    # 1. OpenRouter: GET https://openrouter.ai/api/v1/key -> {"data": {"usage": 1.23, "limit": 50.0, "limit_remaining": 48.77}}
    #    or GET https://openrouter.ai/api/v1/credits -> {"data": {"total_credits": 50.0, "total_usage": 1.23}}
    if low_preset == "openrouter" or "openrouter.ai" in low_url:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if "total_credits" in data and "total_usage" in data:
            total = float(data["total_credits"] or 0.0)
            used = float(data["total_usage"] or 0.0)
            return {"available": round(total - used, 4), "monthly_allocation": round(total, 4), "used_month": round(used, 4), "unit": "usd"}
        rem = data.get("limit_remaining")
        limit = data.get("limit")
        usage = float(data.get("usage") or 0.0)
        if rem is not None:
            return {
                "available": round(float(rem), 4),
                "monthly_allocation": round(float(limit), 4) if limit is not None else None,
                "used_month": round(usage, 4),
                "unit": "usd",
            }
        # Unlimited / postpaid OpenRouter key (`limit: null`)
        return {"available": None, "balance_supported": False, "used_month": round(usage, 4), "unit": "usd"}

    # 2. DeepSeek: GET https://api.deepseek.com/user/balance -> {"is_available": true, "balance_infos": [{"currency": "USD", "total_balance": "12.34"}]}
    if low_preset == "deepseek" or "deepseek.com" in low_url:
        infos = payload.get("balance_infos")
        if isinstance(infos, list) and infos:
            usd_row = next((r for r in infos if isinstance(r, dict) and str(r.get("currency")).upper() == "USD"), None)
            row = usd_row or (infos[0] if isinstance(infos[0], dict) else None)
            if row is not None and row.get("total_balance") is not None:
                raw_bal = float(row["total_balance"])
                cur = str(row.get("currency") or "USD").upper()
                usd_val = raw_bal if cur == "USD" else raw_bal * CNY_TO_USD_FALLBACK
                return {"available": round(usd_val, 4), "monthly_allocation": None, "unit": "usd", "raw_currency": cur}

    # 3. Moonshot (Kimi): GET https://api.moonshot.ai/v1/users/me/balance -> {"data": {"available_balance": 19.5}}
    if low_preset == "moonshot" or "moonshot." in low_url:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if data.get("available_balance") is not None:
            return {"available": round(float(data["available_balance"]), 4), "monthly_allocation": None, "unit": "usd"}

    # 4. SiliconFlow: GET https://api.siliconflow.com/v1/user/info -> {"data": {"totalBalance": "15.20"}}
    if low_preset == "siliconflow" or "siliconflow." in low_url:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        bal = data.get("totalBalance") if data.get("totalBalance") is not None else data.get("balance")
        if bal is not None:
            return {"available": round(float(bal), 4), "monthly_allocation": None, "unit": "usd"}

    # 5. Generic JSON convention (`{"available": ...}` or `{"balance": ...}` or `{"credits": ...}`)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("available", "available_balance", "balance", "remaining", "credits"):
        if isinstance(data, dict) and isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool):
            return {"available": round(float(data[key]), 4), "monthly_allocation": None, "unit": "usd"}

    return {"available": None, "balance_supported": False, "unit": "usd"}


class ProviderBalanceAccount(UnmeteredAccount):
    """USD usage & optional live-balance account manager for universal providers."""

    def __init__(self, cfg: dict, *, fetch=None, backend=None):
        super().__init__()
        self.cfg = cfg
        self.backend = backend
        self._fetch = fetch
        self._endpoint = cfg.get("balance_endpoint")
        self.supports_balance = bool(self._endpoint)
        self.keeps_local_debit = bool(self._endpoint)
        self._fetch_lock = threading.Lock()
        self._cached_info: dict | None = None
        self._fetched_at = float("-inf")
        self._failed_at = float("-inf")
        self.last_error: str | None = None
        self._spent_since_fetch = 0.0

    def record_provider_usage(self, *args, **kwargs):
        before = self._currencies.get("USD", 0.0)
        super().record_provider_usage(*args, **kwargs)
        after = self._currencies.get("USD", 0.0)
        delta = max(0.0, after - before)
        if delta > 0:
            with self._lock:
                self._spent_since_fetch += delta

    def _http_get_balance(self) -> dict:
        url = str(self._endpoint or "").strip()
        http_pool.validate_endpoint_url(url, allow_insecure_http=bool(self.cfg.get("allow_insecure_http", False)))
        headers = discovery.build_auth_headers(self.cfg)
        custom_transport = self.cfg.get("_http_transport")
        kwargs: dict = {
            "follow_redirects": False,
            "timeout": 12.0,
            "trust_env": bool(self.cfg.get("use_system_proxy", False)) and not self.cfg.get("proxy_url") and custom_transport is None,
        }
        if custom_transport is not None:
            # Support sync or async MockTransport in tests via Client/AsyncClient
            if isinstance(custom_transport, httpx.BaseTransport):
                kwargs["transport"] = custom_transport
                with httpx.Client(**kwargs) as client:
                    resp = client.get(url, headers=headers)
            else:
                import asyncio

                async def _async_get():
                    async with httpx.AsyncClient(transport=custom_transport, follow_redirects=False, timeout=12.0) as client:
                        return await client.get(url, headers=headers)

                resp = asyncio.run(_async_get())
        else:
            proxy_url = http_pool.validate_proxy_url(self.cfg.get("proxy_url"))
            if proxy_url:
                kwargs["proxy"] = proxy_url
            kwargs["verify"] = bool(self.cfg.get("verify_tls", True))
            with httpx.Client(**kwargs) as client:
                resp = client.get(url, headers=headers)

        if resp.status_code in (401, 403):
            raise PermissionError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid JSON balance response")
        return _parse_balance_response(str(self.cfg.get("preset") or ""), url, payload)

    def balance_snapshot(self) -> dict:
        usage_summary = self.usage()
        base_out = {
            "session_used": usage_summary.get("provider_cost"),
            "monthly_allocation": None,
            "used_month": None,
            "reset_date": None,
            "is_low": False,
            "stale": False,
            "unreported_usage": usage_summary.get("unreported_usage", False),
            "unit": "usd",
        }
        if not self._endpoint:
            return {**base_out, "available": None, "balance_supported": False}

        with self._lock:
            cached = dict(self._cached_info) if self._cached_info is not None else None
            spent = self._spent_since_fetch
            err = self.last_error

        if cached is None:
            return {**base_out, "available": None, "balance_supported": True, **({"error": err, "stale": True} if err else {})}

        if cached.get("balance_supported") is False:
            return {**base_out, **cached, "available": None, "balance_supported": False}

        avail = cached.get("available")
        if isinstance(avail, (int, float)) and math.isfinite(avail):
            avail = round(avail - spent, 4)
        min_usd = float(self.cfg.get("min_balance_usd") or 0.0)
        return {
            **base_out,
            **cached,
            "available": avail,
            "balance_supported": True,
            "is_low": isinstance(avail, (int, float)) and avail <= min_usd,
            **({"error": err, "stale": True} if err else {}),
        }

    def get_balance(self, force_refresh: bool = False) -> dict:
        if not self._endpoint:
            return self.balance_snapshot()

        now = time.monotonic()
        if not force_refresh and self._cached_info is not None and (now - self._fetched_at) < CACHE_SECONDS:
            return self.balance_snapshot()
        if not force_refresh and self.last_error and (now - self._failed_at) < CACHE_SECONDS:
            return self.balance_snapshot()

        with self._fetch_lock:
            now = time.monotonic()
            if not force_refresh and self._cached_info is not None and (now - self._fetched_at) < CACHE_SECONDS:
                return self.balance_snapshot()
            try:
                parsed = self._fetch(self.cfg) if callable(self._fetch) else self._http_get_balance()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.last_error = f"balance check failed ({type(exc).__name__})"
                    self._failed_at = time.monotonic()
                return self.balance_snapshot()

            with self._lock:
                self._cached_info = dict(parsed)
                self._fetched_at = time.monotonic()
                self._spent_since_fetch = 0.0
                self.last_error = None
            return self.balance_snapshot()
