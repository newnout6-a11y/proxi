"""Admin settings and pool conversion: provider access, role budget, journal hygiene, honest route reasons."""
import json

import pytest

import admin
import config
import models
from session import Backend, SessionPool
from test_admin import admin_relay  # noqa: F401 - single-account fixture
from test_admin_pool import NEW, pool  # noqa: F401 - two-account relay fixture

RETENTION = {"anthropic": {"resource_name": "private-resource", "data_retention_enabled": True}}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RETOOL_THREAD_ID",
                 "RELAY_SESSION_MODE", "RELAY_API_KEY", "RELAY_HOST", "RELAY_PORT", "RELAY_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)


def post_account(relay):
    return relay.request("/admin/api/accounts", NEW, method="POST",
                         headers={"x-admin-request": "1", "origin": relay.url})


def offline_pool(cfg):
    return SessionPool(Backend(cfg, http_post=lambda *a, **k: None, ws_connect=lambda *a, **k: None))


# -- provider_access stays with its account ------------------------------------------
def test_single_account_gate_moves_into_the_first_pool_record_and_keeps_gated_models(admin_relay):
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    saved["provider_access"] = RETENTION
    admin_relay.path.write_text(json.dumps(saved), encoding="utf-8")
    fable = models.resolve("claude-fable-5")[0]
    single = config.load(str(admin_relay.path))
    assert models.catalog_routable(fable, single, offline_pool(single).backends)

    assert "provider_access" in admin.ACCOUNT_RECORD_KEYS
    status, _, _ = post_account(admin_relay)
    assert status == 200
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    first, second = saved["accounts"]
    assert first["provider_access"] == RETENTION and "provider_access" not in second
    # The pool reads the descriptor only from the account record: the gated model must survive the conversion.
    reloaded = config.load(str(admin_relay.path))
    backends = offline_pool(reloaded).backends
    assert [SessionPool._routable(backend, fable) for backend in backends] == [True, False]
    assert models.catalog_routable(fable, reloaded, backends)


@pytest.mark.parametrize("second_gate", [{"anthropic": {"resource_name": "resource-acc-b", "byok": True}}, None])
def test_deleting_the_first_account_hands_the_root_descriptor_to_the_new_first_account(pool, second_gate):
    first_gate = {"anthropic": {"resource_name": "resource-acc-a", "data_retention_enabled": True}}
    saved = pool.saved()
    saved["accounts"][0]["provider_access"] = first_gate
    saved["provider_access"] = first_gate  # the root mirrors accounts[0], as after a single -> pool conversion
    if second_gate:
        saved["accounts"][1]["provider_access"] = second_gate
    pool.path.write_text(json.dumps(saved), encoding="utf-8")
    assert pool.call("/admin/api/accounts/" + pool.tags[0], {}, method="DELETE")[0] == 200
    saved = pool.saved()
    assert [item.get("provider_access") for item in saved["accounts"]] == [second_gate]
    assert saved.get("provider_access") == second_gate
    assert "resource-acc-a" not in json.dumps(saved.get("provider_access"))
    assert config.load(str(pool.path))["accounts"][0].get("provider_access") == second_gate
