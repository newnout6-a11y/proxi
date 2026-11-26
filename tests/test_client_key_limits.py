"""A client key's request limits count each client request once."""
import pytest

import apierr
import auth
import client_context

KEY = {"id": "ide", "label": "IDE", "key": "sk-proxi-test-key-0123456789abcdef", "enabled": True,
       "allowed_models": None, "rpm_limit": 2, "max_budget_usd": None}


def manager():
    keys = auth.ClientKeyManager({"client_keys": [dict(KEY)]})
    return keys, dict(KEY, master=False)


# A07-05: a Claude Code prompt suggestion checks each of its candidate models with enforce_request after the request's own
# check, and every call took an RPM slot and counted a request: one suggestion used two slots or more of rpm_limit.
def test_one_request_that_checks_several_models_takes_one_slot():
    keys, profile = manager()
    token = client_context.bind({"request_id": "req-1"})
    try:
        keys.enforce_request(profile, "claude-opus-5-5")
        keys.enforce_request(profile, "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001")
        keys.enforce_request(profile, "gpt-5.5", "gpt-5.5")
    finally:
        client_context.reset(token)
    stats = keys.list_public()[0]
    assert stats["requests"] == 1
    token = client_context.bind({"request_id": "req-2"})
    try:
        keys.enforce_request(profile, "claude-opus-5-5")  # the second request of the minute fits the limit of 2
    finally:
        client_context.reset(token)
    token = client_context.bind({"request_id": "req-3"})
    try:
        with pytest.raises(apierr.ApiError) as refused:
            keys.enforce_request(profile, "claude-opus-5-5")
    finally:
        client_context.reset(token)
    assert refused.value.status == 429
    assert keys.list_public()[0]["requests"] == 2


def test_without_a_request_id_every_check_counts_as_before():
    keys, profile = manager()
    keys.enforce_request(profile, "claude-opus-5-5")
    keys.enforce_request(profile, "claude-opus-5-5")
    assert keys.list_public()[0]["requests"] == 2
