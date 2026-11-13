"""The admin pool adds Minara accounts from several Bearer tokens at once and shows when each token expires.

All tokens are synthetic JWTs; nothing reaches api.minara.ai (a balance read is answered by a fake backend._minara_get)."""
import json
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

import config
import journal
import proxy
from minara_catalog import CREDITS_PER_USD
from test_admin import admin_relay  # noqa: F401 - single-account fixture
from test_admin_pool import pool, clean_environment  # noqa: F401 - isolated HTTP/admin fixtures
from test_minara_accounts import DAY, jwt

ADD = "/admin/api/accounts"
T0 = int(time.time())  # every expiry of this module is relative to one moment, so equal values stay equal


def soon(days):
    return T0 + int(days * DAY)


def post(pool, payload):
    return pool.call(ADD, payload, method="POST")


def bulk(*tokens, **fields):
    return {"upstream": "minara", "bearer_tokens": list(tokens), **fields}


def assert_no_token(value, tokens):
    encoded = json.dumps(value, ensure_ascii=False)
    for token in tokens:
        assert token not in encoded


def test_several_tokens_become_numbered_accounts_and_each_is_reported(pool):
    tokens = [jwt("user-1", exp=soon(30)), jwt("user-2", exp=soon(29)), "not a token", jwt("user-1", exp=soon(31)),
              jwt("user-3", exp=soon(-1)), jwt("user-4", exp=soon(2))]
    before = journal.JOURNAL.last_seq
    status, body = post(pool, bulk(*tokens))
    assert status == 200, body
    assert body["added"] == 3 and body["skipped"] == 3 and body["restart_required"] is False
    results = body["results"]
    assert [item["status"] for item in results] == ["added", "added", "invalid", "duplicate", "expired", "added"]
    assert [item["token"] for item in results] == [1, 2, 3, 4, 5, 6]
    assert [item.get("label") for item in results] == ["Minara 1", "Minara 2", None, "Minara 1", None, "Minara 3"]
    assert results[0]["expires_at"] == soon(30) and results[4]["expires_at"] == soon(-1)
    assert results[3] == {"token": 4, "status": "duplicate", "reason": "same_user", "newer": True, "repeat_of": 1,
                          "expires_at": soon(31), "account": results[0]["account"], "label": "Minara 1"}
    assert results[2]["problem"] == "format"
    # The message passes through redact() whole: it never says "Bearer <word>", whose word would be masked
    assert results[2]["error"] == "a token is printable ASCII without spaces, at most 16384 characters"
    assert "[redacted]" not in json.dumps(body, ensure_ascii=False)
    assert_no_token(body, tokens)

    saved = pool.saved()["accounts"]
    assert len(saved) == 5
    for record, token, label in zip(saved[2:], (tokens[0], tokens[1], tokens[5]), ("Minara 1", "Minara 2", "Minara 3")):
        assert record["bearer_token"] == token and record["label"] == label and record["upstream"] == "minara"
        assert record["base"] == "https://api.minara.ai/v1" and "minara_models" not in record
        assert (record["enabled"], record["priority"]) == (True, 0) and len(record["account_id"]) == 32
        assert journal.tag(record["account_id"]) in [item.get("account") for item in results]
    assert config.load(str(pool.path))["accounts"][4]["bearer_token"] == tokens[5]

    listing = pool.call(ADD)[1]
    # In the pool at once: listed as loaded accounts (pending None), with the expiry each token names.
    added = [item for item in listing["accounts"] if item["label"] in ("Minara 1", "Minara 2", "Minara 3")]
    assert [(item["label"], item["pending"], item["expires_at"], item["balance_unit"]) for item in added] == [
        ("Minara 1", None, soon(30), "minara_credits"), ("Minara 2", None, soon(29), "minara_credits"),
        ("Minara 3", None, soon(2), "minara_credits")]
    assert listing["restart_required"] is False
    assert len(pool.runtime.pool.backends) == 5 and len(pool.runtime.accounts.backends) == 5
    assert_no_token(listing, tokens)
    events = [event for event in journal.JOURNAL.query(since=before) if event["kind"] == "admin.account"]
    assert len(events) == 1 and events[0]["data"]["accounts"] == [item["account"] for item in results if item["status"] == "added"]
    assert events[0]["data"]["skipped"] == 3
    assert_no_token(list(journal.JOURNAL._events), tokens)
    assert all(token in pool.runtime.cfg["cookie_values"] for token in (tokens[0], tokens[1], tokens[5]))


def test_tokens_the_pool_already_has_change_nothing(pool, monkeypatch):
    stored, env_token = jwt("user-1", exp=soon(5)), jwt("user-env", exp=soon(20))
    monkeypatch.setenv("MINARA_BULK_TEST_TOKEN", env_token)
    assert post(pool, bulk(stored, label="Мой"))[0] == 200
    assert post(pool, {"upstream": "minara", "bearer_token_env": "MINARA_BULK_TEST_TOKEN",
                           "label": "Minara 7"})[0] == 200
    before = pool.path.read_bytes()
    status, body = post(pool, bulk(stored, jwt("user-1", exp=soon(1)), env_token))
    assert status == 200 and body["added"] == 0 and body["restart_required"] is False
    assert [(item["status"], item["reason"], item["label"]) for item in body["results"]] == [
        ("duplicate", "same_token", "Мой"), ("duplicate", "same_user", "Мой"), ("duplicate", "same_token", "Minara 7")]
    assert body["results"][1]["newer"] is False
    assert pool.path.read_bytes() == before
    # Numbering goes on from the highest "Minara N", the env-referenced account included.
    status, body = post(pool, bulk(jwt("user-9")))
    assert body["results"][0]["label"] == "Minara 8"


def test_a_newer_token_of_a_stored_user_renews_that_account(pool):
    stored, fresh = jwt("user-1", exp=soon(2)), jwt("user-1", exp=soon(30))
    assert post(pool, bulk(stored, label="Мой"))[0] == 200
    status, body = post(pool, bulk(fresh))
    # The running account sends the new token from its next request: nothing waits for a restart.
    assert status == 200 and body["added"] == 0 and body["renewed"] == 1 and body["restart_required"] is False
    live = next(backend for backend in pool.runtime.pool.backends if backend.cfg.get("label") == "Мой")
    assert live.cfg["bearer_token"] == fresh and live.credits.expires_at() == soon(30)
    assert [(item["status"], item["label"]) for item in body["results"]] == [("renewed", "Мой")]
    assert body["results"][0]["was_expires_at"] == soon(2) and body["results"][0]["expires_at"] == soon(30)
    assert fresh not in json.dumps(body)
    saved = [record for record in json.loads(pool.path.read_text(encoding="utf-8"))["accounts"]
             if record.get("upstream") == "minara"]
    assert [(record["label"], record["bearer_token"]) for record in saved] == [("Мой", fresh)]


def test_a_label_and_a_model_list_apply_to_every_new_account(pool):
    status, body = post(pool, bulk(jwt("a"), jwt("b"), label="Команда",
                                       minara_models=["anthropic/claude-opus-5.5"], base="https://api.minara.ai/v1/"))
    assert status == 200, body
    added = pool.saved()["accounts"][2:]
    assert [record["label"] for record in added] == ["Команда 1", "Команда 2"]
    assert all(record["minara_models"] == ["anthropic/claude-opus-5.5"] for record in added)
    assert all(record["base"] == "https://api.minara.ai/v1" for record in added)


@pytest.mark.parametrize("payload", [
    bulk(), {"upstream": "minara", "bearer_tokens": "eyJ.a.b"}, bulk(jwt("a"), cookie="x"),
    bulk(jwt("a"), bearer_token_env="MINARA_X"), bulk(jwt("a"), label="x" * 41), bulk(jwt("a"), label="a\nb"),
    bulk(jwt("a"), minara_models={"id": "x"}), bulk(jwt("a"), base="http://api.minara.ai/v1"),
    bulk(*[jwt(str(n)) for n in range(201)]),
])
def test_a_malformed_request_writes_nothing(pool, payload):
    before = pool.path.read_bytes()
    status, body = post(pool, payload)
    assert status == 400, body
    assert pool.path.read_bytes() == before


def test_the_single_account_shape_still_needs_no_model_list(pool, monkeypatch):
    token = jwt("env-user", exp=soon(9))
    monkeypatch.setenv("MINARA_BULK_TEST_TOKEN", token)
    status, body = post(pool, {"upstream": "minara", "bearer_token_env": "MINARA_BULK_TEST_TOKEN",
                                   "minara_models": [], "label": ""})
    assert status == 200 and body["account"]["pending"] is None and body["restart_required"] is False
    record = pool.saved()["accounts"][-1]
    assert "minara_models" not in record and "bearer_token" not in record
    assert body["account"]["expires_at"] == soon(9) and body["account"]["balance_unit"] == "minara_credits"
    assert_no_token(body, [token])


def test_a_single_account_file_becomes_a_pool_with_the_new_tokens(admin_relay):
    headers = {"x-admin-request": "1", "origin": admin_relay.url}
    status, body, _ = admin_relay.request(ADD, bulk(jwt("a"), jwt("b")), method="POST", headers=headers)
    assert status == 200 and body["added"] == 2
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    first, *added = saved["accounts"]
    assert first["cookie"] == "accessToken=private-cookie-value" and "cookie" not in saved
    assert [record["label"] for record in added] == ["Minara 1", "Minara 2"]
    assert config._account_record(saved, admin_relay.runtime.backend.cfg) is first


# -- a loaded Minara account: credits in both units and the token's expiry --------------------------------------------
@pytest.fixture
def minara_relay(tmp_path, monkeypatch):
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID", "RELAY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    tokens = [jwt("live-user", exp=soon(12)), jwt("dead-user", exp=soon(-1)), "opaque-bearer-token-value"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "model": "minara/claude-opus-5-5", "relay_api_key": "minara-admin-key-123", "workspace_root": str(tmp_path),
        "conversation_state": False, "journal_path": str(tmp_path / "journal.jsonl"),
        "accounts": [{"label": "Minara %d" % n, "upstream": "minara", "bearer_token": token}
                     for n, token in enumerate(tokens, 1)]}), encoding="utf-8")
    cfg = config.load(str(path))
    cfg["host"], cfg["port"] = "127.0.0.1", 0
    runtime = proxy.Runtime(cfg)
    reads = []

    def auth_me(url, **kwargs):
        reads.append(kwargs["headers"]["Authorization"])
        return types.SimpleNamespace(status_code=200, json=lambda: {
            "creditBalanceBreakdown": {"plan": "120.5", "extra": "99.5"}})
    for backend in runtime.accounts.backends:
        backend._minara_get = auth_me
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(8))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(route):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (server.server_address[1], route))
        request.add_header("authorization", "Bearer minara-admin-key-123")
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read().decode() or "{}")
    yield types.SimpleNamespace(runtime=runtime, call=call, tokens=tokens, reads=reads)
    server.shutdown()
    server.server_close()
    runtime.close()


def test_the_pool_shows_credits_in_both_units_and_the_token_expiry(minara_relay):
    minara_relay.runtime.accounts.check_expiry()
    status, body = minara_relay.call(ADD)
    assert status == 200
    live, dead, opaque = body["accounts"]
    assert (live["balance_unit"], live["available"], live["usd"]) == ("minara_credits", 220, round(220 / CREDITS_PER_USD, 4))
    assert live["expires_at"] == soon(12) and live["state"] == "active"
    assert (dead["state"], dead["error"], dead["expires_at"], dead["available"]) == ("auth_dead", "token_expired", soon(-1), None)
    assert dead["usd"] is None and dead["routable"] is False
    assert opaque["expires_at"] is None and opaque["state"] == "active" and opaque["usd"] == live["usd"]
    # The expired token was never sent; the other two were.
    assert len(minara_relay.reads) == 2 and "Bearer " + minara_relay.tokens[1] not in minara_relay.reads
    status, overview = minara_relay.call("/admin/api/overview")
    assert [item["expires_at"] for item in overview["accounts"]] == [soon(12), soon(-1), None]
    assert_no_token((body, overview), minara_relay.tokens)
