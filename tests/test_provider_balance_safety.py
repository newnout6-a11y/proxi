"""Balances of universal providers: where their API key may go, in which unit they are judged, and the local debit."""
from __future__ import annotations

import httpx
import pytest

from drivers import balance_checkers, presets


def _provider_account(provider, handler, **extra):
    _meta, records, _secrets = presets.expand_provider_to_accounts(provider, "provider")
    cfg = dict(records[0], _http_transport=httpx.MockTransport(handler), **extra)
    return cfg, balance_checkers.ProviderBalanceAccount(cfg)


# A11-01: the new-provider form starts on the OpenRouter preset. A provider whose base_url was changed to another host kept
# the preset's balance endpoint, and every balance check (about once a minute) sent that provider's key to openrouter.ai.
def test_a_provider_key_never_goes_to_a_balance_host_other_than_its_own_base_url():
    sent = []

    def handler(request):
        sent.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(200, json={"data": {"usage": 1.0, "limit": 50.0, "limit_remaining": 49.0}})

    cfg, account = _provider_account({"id": "zai", "preset": "openrouter", "base_url": "https://api.z.ai/api/paas/v4",
                                      "api_key": "zai-FAKE-KEY-0123456789abcdef"}, handler)
    assert cfg["balance_endpoint"] == "https://openrouter.ai/api/v1/key"  # what the preset hands on
    assert account.supports_balance is False
    info = account.get_balance(force_refresh=True)
    assert sent == []
    assert info["available"] is None and info["balance_supported"] is False

    # Even an endpoint put on the account later is checked again before the key leaves.
    account._endpoint = "https://openrouter.ai/api/v1/key"
    with pytest.raises(PermissionError):
        account._http_get_balance()
    assert sent == []


def test_a_preset_balance_endpoint_on_the_providers_own_host_is_still_read():
    sent = []

    def handler(request):
        sent.append(request.url.host)
        return httpx.Response(200, json={"data": {"usage": 1.0, "limit": 50.0, "limit_remaining": 49.0}})

    _cfg, account = _provider_account({"id": "openrouter", "preset": "openrouter",
                                       "api_key": "sk-or-v1-FAKE-0123456789abcdef"}, handler)
    assert account.supports_balance is True
    assert account.get_balance(force_refresh=True)["available"] == 49.0
    assert sent == ["openrouter.ai"]


@pytest.mark.parametrize("endpoint, base, own", [
    ("https://api.deepseek.com/user/balance", "https://api.deepseek.com", True),
    ("https://API.DeepSeek.com./user/balance", "https://api.deepseek.com/v1", True),
    ("https://api.deepseek.com:443/user/balance", "https://api.deepseek.com", True),
    ("http://api.deepseek.com/user/balance", "https://api.deepseek.com", False),
    ("https://api.deepseek.com:8443/user/balance", "https://api.deepseek.com", False),
    ("https://evil.example/api.deepseek.com/user/balance", "https://api.deepseek.com", False),
    ("https://openrouter.ai/api/v1/key", "https://api.z.ai/api/paas/v4", False),
    ("", "https://api.deepseek.com", False),
    ("https://api.deepseek.com/user/balance", "", False),
])
def test_only_an_endpoint_on_the_same_scheme_host_and_port_is_the_providers_own(endpoint, base, own):
    assert (balance_checkers.own_balance_endpoint(endpoint, base) is not None) is own
