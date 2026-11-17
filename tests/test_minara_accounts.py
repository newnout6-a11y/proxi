"""minara_accounts: what a Minara Bearer token says about itself, and the rule both ways of adding accounts follow.

Every token here is a synthetic JWT-shaped string; no test reaches api.minara.ai."""
import base64
import json

import pytest

import apierr
import minara_accounts

NOW = 1_800_000_000.0
DAY = 86400


def _b64(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def jwt(sub="user-1", exp=NOW + 30 * DAY, iat=None, signature="c3ludGhldGljLXNpZ25hdHVyZQ", **claims):
    """A synthetic token shaped like Minara's: HS256 header, {sub, iat, exp} payload, a made-up signature."""
    payload = {"sub": sub, "iat": NOW - DAY if iat is None else iat, **claims}
    if exp is not None:
        payload["exp"] = exp
    return "%s.%s.%s" % (_b64({"alg": "HS256", "typ": "JWT"}), _b64(payload), signature)


def minara(label=None, **fields):
    record = {"upstream": "minara", "base": "https://api.minara.ai/v1", **fields}
    if label is not None:
        record["label"] = label
    return record


def plan(records, tokens, **options):
    return minara_accounts.plan(records, tokens, now=NOW, **options)


# -- what a token says ---------------------------------------------------------------------------------------------
def test_expiry_and_user_are_read_from_the_payload_without_the_signature():
    token = jwt(sub="minara-user-7", exp=NOW + 5 * DAY)
    assert token.startswith("eyJ")
    assert minara_accounts.expires_at(token) == NOW + 5 * DAY
    assert minara_accounts.subject(token) == "minara-user-7"
    assert minara_accounts.subject(jwt(sub=12345)) == "12345"
    # The signature is never checked: another one says the same.
    assert minara_accounts.expires_at(jwt(exp=NOW, signature="other")) == NOW


@pytest.mark.parametrize("token", [
    None, 42, "", "opaque-bearer-token", "a.b", "a.b.c.d", "eyJ.%%%.sig", "eyJhbGciOiJIUzI1NiJ9.e.sig",
    "eyJhbGciOiJIUzI1NiJ9.%s.sig" % _b64([1, 2]), "eyJhbGciOiJIUzI1NiJ9.%s.sig" % _b64("text"),
    "eyJhbGciOiJIUzI1NiJ9.%s.sig" % base64.urlsafe_b64encode(b"\xff\xfe{").decode(),
    "eyJhbGciOiJIUzI1NiJ9.%s.sig" % base64.urlsafe_b64encode(b"[" * 5000).decode(),
    "eyJhbGciOiJIUzI1NiJ9.%s.sig" % _b64({"exp": "1800000000"}), "x" * 20000 + ".a.b",
])
def test_a_token_that_is_not_a_readable_jwt_has_no_expiry_and_no_user(token):
    assert minara_accounts.expires_at(token) is None
    assert minara_accounts.subject(token) is None


@pytest.mark.parametrize("exp", [True, None, "soon", 0, -5, float("nan"), float("inf"), [1]])
def test_an_unusable_exp_is_unknown(exp):
    assert minara_accounts.expires_at(jwt(exp=exp)) is None


def test_a_records_token_is_resolved_as_the_relay_resolves_it(monkeypatch):
    monkeypatch.setenv("MINARA_TEST_TOKEN_ENV", "from-environment")
    assert minara_accounts.record_token(minara(bearer_token="inline")) == "inline"
    assert minara_accounts.record_token(minara(bearer_token="inline", bearer_token_env="MINARA_TEST_TOKEN_ENV")) \
        == "from-environment"
    assert minara_accounts.record_token(minara(bearer_token_env="MINARA_TEST_TOKEN_UNSET")) is None
    assert minara_accounts.record_token({"bearer_token": "retool-has-none"}) is None


# -- labels --------------------------------------------------------------------------------------------------------
def test_new_accounts_continue_the_largest_minara_number():
    records = [minara("Minara 1", bearer_token=jwt("a")), minara("minara 7", bearer_token=jwt("b")),
               minara("Minara 3", bearer_token=jwt("c")), minara("Minara 12b", bearer_token=jwt("d")),
               {"label": "Minara 40", "cookie": "a=b", "xsrf": "x", "base": "https://t.retool.com", "page_uuid": "p"}]
    results, added = plan(records, [jwt("new-1"), jwt("new-2")])
    # The Retool account's label counts too: two accounts are never shown under one name.
    assert [record["label"] for record in added] == ["Minara 41", "Minara 42"]
    assert [result["status"] for result in results] == ["added", "added"]
    assert plan([], [jwt("first")])[1][0]["label"] == "Minara 1"


def test_a_label_names_one_account_as_it_is_and_numbers_several():
    records = [minara("Team 2", bearer_token=jwt("a"))]
    assert plan(records, [jwt("b")], label="  Личный  ")[1][0]["label"] == "Личный"
    assert [record["label"] for record in plan(records, [jwt("b"), jwt("c")], label="Team")[1]] == ["Team 3", "Team 4"]
    with pytest.raises(apierr.ApiError) as refused:
        plan(records, [jwt("b"), jwt("c")], label="x" * 39)
    assert refused.value.status == 400 and refused.value.param == "label"


# -- duplicates, expiry, invalid tokens ------------------------------------------------------------------------------
def test_a_token_or_a_user_the_pool_already_has_is_skipped(monkeypatch):
    stored = jwt("user-a", exp=NOW + 2 * DAY)
    monkeypatch.setenv("MINARA_TEST_TOKEN_ENV", jwt("user-env"))
    records = [minara("Minara 1", bearer_token=stored, account_id="acc-1"),
               minara("Minara 2", bearer_token_env="MINARA_TEST_TOKEN_ENV", account_id="acc-2")]
    fresh_a = jwt("user-a", exp=NOW + 30 * DAY)
    results, added = plan(records, [stored, fresh_a, jwt("user-env", exp=NOW + DAY), jwt("user-new")])
    # A newer token of a stored user renews that account (Minara gives a new 30-day token at each sign-in).
    assert [result["status"] for result in results] == ["duplicate", "renewed", "duplicate", "added"]
    assert results[0]["reason"] == "same_token" and results[0]["of"] is records[0]
    assert results[1]["of"] is records[0] and results[1]["was_expires_at"] == NOW + 2 * DAY
    assert results[2]["reason"] == "same_user" and results[2]["of"] is records[1] and results[2]["newer"] is False
    assert [record["label"] for record in added] == ["Minara 3"]
    assert records[0]["bearer_token"] == stored  # plan() changes nothing...
    assert minara_accounts.apply_renewals(results) == [fresh_a]  # ...apply_renewals stores the new token
    assert records[0]["bearer_token"] == fresh_a and records[0]["label"] == "Minara 1" and records[0]["account_id"] == "acc-1"


def test_an_older_or_expired_token_never_renews_and_an_env_account_is_never_rewritten(monkeypatch):
    stored = jwt("user-a", exp=NOW + 20 * DAY)
    monkeypatch.setenv("MINARA_TEST_TOKEN_ENV", jwt("user-env", exp=NOW + DAY))
    records = [minara("Minara 1", bearer_token=stored), minara("Minara 2", bearer_token_env="MINARA_TEST_TOKEN_ENV")]
    results, _ = plan(records, [jwt("user-a", exp=NOW + 5 * DAY), jwt("user-env", exp=NOW + 30 * DAY)])
    assert [result["status"] for result in results] == ["duplicate", "duplicate"]
    assert results[0]["newer"] is False and results[1]["newer"] is True
    assert minara_accounts.apply_renewals(results) == []


def test_a_token_repeated_in_one_request_is_added_once():
    first, again = jwt("user-a"), jwt("user-a", exp=NOW + 40 * DAY)
    results, added = plan([], [first, " %s " % first, again, jwt("user-b")])
    assert [result["status"] for result in results] == ["added", "duplicate", "duplicate", "added"]
    assert results[1]["reason"] == "same_token" and results[1]["repeat_of"] == 1 and results[1]["of"] is added[0]
    assert results[2]["reason"] == "same_user" and results[2]["repeat_of"] == 1 and results[2]["newer"] is True
    assert [record["label"] for record in added] == ["Minara 1", "Minara 2"]


def test_an_expired_token_is_skipped_and_an_unreadable_expiry_is_not_judged():
    results, added = plan([], [jwt("old", exp=NOW - 1), jwt("edge", exp=NOW), "opaque-bearer-token-value"])
    assert [result["status"] for result in results] == ["expired", "expired", "added"]
    assert results[0]["expires_at"] == NOW - 1
    assert added[0]["bearer_token"] == "opaque-bearer-token-value" and results[2]["expires_at"] is None


@pytest.mark.parametrize("token, problem", [
    (None, "empty"), (7, "empty"), ("", "empty"), ("  ", "empty"), ("two words", "format"), ("токен", "format"),
    ("x" * 16385, "format"), ("bad\x00token", "format"), ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2Vy", "cut"),
    (jwt("cut")[:-30] + "!", "cut"), ("eyJbroken", "cut"),
])
def test_an_invalid_token_is_reported_without_being_echoed(token, problem):
    results, added = plan([], [token, jwt("ok")])
    assert results[0]["status"] == "invalid" and results[0]["problem"] == problem and added and len(added) == 1
    if isinstance(token, str) and token.strip():
        assert token.strip() not in results[0]["error"]


def test_a_jwt_cut_off_when_copied_is_named_as_such():
    cut = jwt("user")[:40]
    assert "cut off" in plan([], [cut])[0][0]["error"]


def test_new_records_carry_the_shared_fields_and_nothing_resolved():
    models = [{"id": "anthropic/claude-opus-5.5"}]
    _, added = plan([], [jwt("a")], base="https://api.minara.ai/v1/", minara_models=models)
    assert added[0] == {"label": "Minara 1", "upstream": "minara", "base": "https://api.minara.ai/v1",
                        "minara_models": models, "bearer_token": jwt("a"), "enabled": True, "priority": 0}
    assert added[0]["minara_models"] is not models
    _, every = plan([], [jwt("b")], minara_models=[])
    assert "minara_models" not in every[0]


@pytest.mark.parametrize("options, tokens, param", [
    ({}, [], "bearer_tokens"), ({}, "eyJ.a.b", "bearer_tokens"), ({}, [jwt(str(n)) for n in range(201)], "bearer_tokens"),
    ({"label": 5}, None, "label"), ({"label": "bad\nlabel"}, None, "label"), ({"label": "x" * 41}, None, "label"),
    ({"base": "http://api.minara.ai/v1"}, None, None), ({"minara_models": {"id": "x"}}, None, None),
    ({"minara_models": ["no-vendor-prefix"]}, None, None),
])
def test_a_bad_request_is_refused_as_a_whole(options, tokens, param):
    with pytest.raises(apierr.ApiError) as refused:
        plan([], [jwt("a")] if tokens is None else tokens, **options)
    assert refused.value.status == 400
    if param:
        assert refused.value.param == param


def test_a_model_described_otherwise_by_another_account_refuses_the_request():
    records = [minara("Minara 1", bearer_token=jwt("a"),
                      minara_models=[{"id": "anthropic/claude-opus-5.5", "label": "Opus по-своему"}])]
    with pytest.raises(apierr.ApiError) as refused:
        plan(records, [jwt("b")])  # every published model, Opus described as measured
    assert refused.value.status == 400 and "conflicting" in refused.value.message
    # Nothing to add, nothing to judge: duplicates alone never fail.
    assert plan(records, [records[0]["bearer_token"]])[1] == []
