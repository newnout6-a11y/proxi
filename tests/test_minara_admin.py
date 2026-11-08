"""Bearer account administration does not persist resolved environment secrets."""
import json

import pytest

import admin
import apierr
from test_admin_pool import pool, clean_environment  # noqa: F401 - isolated HTTP/admin fixtures


def record(**overrides):
    return {"upstream": "minara", "bearer_token_env": "MINARA_ADMIN_TEST_TOKEN",
            "minara_models": [{"id": "openai/gpt-oss-20b", "tools": True}], **overrides}


def test_add_bearer_account_keeps_env_reference_without_resolving_to_disk(pool, monkeypatch):
    token = "private-admin-test-token-value"
    monkeypatch.setenv("MINARA_ADMIN_TEST_TOKEN", token)
    status, body = pool.call("/admin/api/accounts", record(), method="POST")
    assert status == 200
    saved = pool.saved()["accounts"][-1]
    assert saved["upstream"] == "minara"
    assert saved["base"] == "https://api.minara.ai/v1"
    assert saved["bearer_token_env"] == "MINARA_ADMIN_TEST_TOKEN"
    assert "bearer_token" not in saved
    assert token not in json.dumps(body)
    assert body["account"]["renewal_supported"] is False
    assert body["account"]["balance_supported"] is False


def test_bearer_accounts_share_an_api_origin_not_an_identity(pool, monkeypatch):
    monkeypatch.setenv("MINARA_ADMIN_TEST_TOKEN", "private-test-token-value")
    first = pool.call("/admin/api/accounts", record(), method="POST")
    second = pool.call("/admin/api/accounts", record(label="second"), method="POST")
    assert first[0] == second[0] == 200
    assert first[1]["account"]["account"] != second[1]["account"]["account"]


@pytest.mark.parametrize("changes", [
    {"cookie": "must-not-be-accepted"}, {"minara_models": []},
    {"base": "http://api.minara.ai/v1"}, {"bearer_token_env": "bad\nname"},
    {"label": "x" * 41},
])
def test_invalid_bearer_account_is_rejected_without_persisting(pool, monkeypatch, changes):
    monkeypatch.setenv("MINARA_ADMIN_TEST_TOKEN", "private-test-token-value")
    before = pool.saved()
    status, _ = pool.call("/admin/api/accounts", record(**changes), method="POST")
    assert status == 400
    assert pool.saved() == before


def test_identity_fallback_never_conflates_bearer_accounts():
    assert not admin.Admin._same_account({"upstream": "minara", "base": "https://api.minara.ai/v1"},
                                        {"upstream": "minara", "base": "https://api.minara.ai/v1"})
    with pytest.raises(apierr.ApiError):
        admin.Admin._new_account({"upstream": "unrecognized"})

@pytest.mark.parametrize("label", ["bad\nlabel", "bad\ud800label"])
def test_bearer_label_validation_precedes_write(pool, monkeypatch, label):
    monkeypatch.setenv("MINARA_ADMIN_TEST_TOKEN", "private-test-token-value")
    before = pool.saved()
    status, _ = pool.call("/admin/api/accounts", record(label=label), method="POST")
    assert status == 400
    assert pool.saved() == before


def test_conflicting_bearer_model_metadata_cannot_break_restart(pool, monkeypatch):
    monkeypatch.setenv("MINARA_ADMIN_TEST_TOKEN", "private-test-token-value")
    assert pool.call("/admin/api/accounts", record(), method="POST")[0] == 200
    before = pool.saved()
    status, _ = pool.call("/admin/api/accounts", record(minara_models=[{"id": "openai/gpt-oss-20b", "tools": False}]), method="POST")
    assert status == 400
    assert pool.saved() == before

def test_pending_bearer_secret_is_masked_in_responses_and_journal(pool, monkeypatch):
    import journal
    token = 'pending-private-token-012345678901234567'
    status, body = pool.call('/admin/api/accounts', {
        'upstream': 'minara', 'bearer_token': token, 'label': token,
        'minara_models': ['openai/gpt-oss-20b']}, method='POST')
    assert status == 200
    assert token not in json.dumps(body)
    status, events = pool.call('/admin/api/events')
    assert status == 200
    assert token not in json.dumps(events)
    assert pool.saved()['accounts'][-1]['bearer_token'] == token
    assert token not in json.dumps(list(journal.JOURNAL._events))
