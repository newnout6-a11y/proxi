"""The prompt control admin API, part 3: preview, recent heads and pins (plan 4.1).

runtime.prompts is a fake that follows the PromptStore contract (admin_prompts_harness). The tests at the end use the
real store.
"""
import json

import pytest

import admin
import apierr
import journal
import prompt_policy
from admin_prompts_harness import (CSRF_CASES, DOC, REQUEST, api, check_csrf, check_needs_the_key,  # noqa: F401
                                   deep, events, get, guard, post, put, raw, real_engine_or_skip, send)
from prompt_policy import PromptConfigError
from test_admin import admin_relay  # noqa: F401 - the relay the api fixture builds on

ROUTES = [("GET", "/admin/api/prompts/recent"), ("GET", "/admin/api/prompts/pins"),
          ("POST", "/admin/api/prompts/preview"), ("POST", "/admin/api/prompts/pins")]
BODIES = {"GET": None, "POST": {"request": {"messages": []}, "dialect": "chat"}}


# -- who may call them ----------------------------------------------------------------------------
@pytest.mark.parametrize("key", [None, "incorrect-key"])
@pytest.mark.parametrize("method,route", ROUTES)
def test_each_route_needs_the_relay_key(api, method, route, key):
    check_needs_the_key(api, method, route, BODIES[method], key)


def test_the_key_in_the_url_is_not_a_login(api):
    assert get(api, "/recent?key=private-relay-key", key=None)[0] == 401
    assert get(api, "/pins?key=private-relay-key", key=None)[0] == 401


@pytest.mark.parametrize("method,route", [item for item in ROUTES if item[0] == "POST"])
@pytest.mark.parametrize("headers,status", CSRF_CASES)
def test_posting_needs_the_csrf_header_the_same_origin_and_json(api, method, route, headers, status):
    check_csrf(api, method, route, BODIES[method], headers, status)


@pytest.mark.parametrize("method,route", [
    ("PUT", "/admin/api/prompts/recent"), ("POST", "/admin/api/prompts/recent"), ("GET", "/admin/api/prompts/preview"),
    ("PUT", "/admin/api/prompts/preview"), ("PUT", "/admin/api/prompts/pins"), ("DELETE", "/admin/api/prompts/pins"),
    ("PATCH", "/admin/api/prompts/pins"),
])
def test_each_route_answers_only_its_own_methods(api, method, route):
    status, body, _ = send(api, route, {"x": 1} if method not in ("GET", "DELETE") else None, method=method,
                           headers=guard(api), attempts=4)
    assert status == 405 and body["error"]["code"] == "method_not_allowed"
    assert api.store.calls == []


def test_the_answers_carry_the_admin_security_headers_and_no_cors(api):
    for status, _, headers in (get(api, "/recent"), get(api, "/pins"),
                               post(api, "preview", {"request": REQUEST, "dialect": "chat"}),
                               post(api, "pins", {"action": "apply_all"})):
        assert status == 200
        assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY" and "access-control-allow-origin" not in headers


# -- POST /admin/api/prompts/preview --------------------------------------------------------------
def test_a_pasted_request_is_previewed_by_the_store_under_the_draft_document(api):
    api.store.preview_answer = {"rule": "r", "profile": "p", "head": "THE HEAD", "parts": [], "warnings": ["w"]}
    status, body, _ = post(api, "preview", {"request": REQUEST, "dialect": "anthropic",
                                            "headers": {"User-Agent": "claude-cli/2"}, "document": DOC})
    assert status == 200 and body == api.store.preview_answer
    assert api.store.called("preview") == [(REQUEST, "anthropic", {"User-Agent": "claude-cli/2"}, DOC)]
    assert api.store.called("save") == [] and api.store.snapshot() is None  # a preview changes nothing
    assert api.store.called("request") == []  # a pasted request needs no recorded one


def test_a_preview_without_headers_or_a_draft_runs_the_active_document(api):
    assert post(api, "preview", {"request": REQUEST, "dialect": "chat"})[0] == 200
    assert post(api, "preview", {"request": REQUEST, "dialect": "responses", "document": None, "headers": {}})[0] == 200
    assert api.store.called("preview") == [(REQUEST, "chat", {}, None), (REQUEST, "responses", {}, None)]


def test_a_preview_without_a_request_takes_the_newest_recorded_one(api):
    api.store.kept[None] = {"body": REQUEST, "dialect": "chat", "headers": {"user-agent": "opencode/1"}}
    status, body, _ = post(api, "preview", {"document": DOC})
    assert status == 200 and body == api.store.preview_answer
    assert api.store.called("request") == [(None,)]
    assert api.store.called("preview") == [(REQUEST, "chat", {"user-agent": "opencode/1"}, DOC)]


def test_a_preview_can_name_the_recorded_request_and_override_its_dialect_and_headers(api):
    api.store.kept["abc123"] = {"body": REQUEST, "dialect": "chat", "headers": {"user-agent": "opencode/1"}}
    assert post(api, "preview", {"request_id": "abc123"})[0] == 200
    assert post(api, "preview", {"request_id": "abc123", "dialect": "responses",
                                 "headers": {"x-proxi-client": "codex"}})[0] == 200
    assert api.store.called("request") == [("abc123",), ("abc123",)]
    assert api.store.called("preview") == [(REQUEST, "chat", {"user-agent": "opencode/1"}, None),
                                           (REQUEST, "responses", {"x-proxi-client": "codex"}, None)]


def test_a_pasted_request_wins_over_a_request_id(api):
    api.store.kept["abc123"] = {"body": {"messages": []}, "dialect": "chat", "headers": {}}
    assert post(api, "preview", {"request": REQUEST, "dialect": "chat", "request_id": "abc123"})[0] == 200
    assert api.store.called("request") == [] and api.store.called("preview")[0][0] == REQUEST


def test_a_preview_of_a_request_that_is_not_kept_is_a_404_that_says_so(api):
    status, body, _ = post(api, "preview", {})
    assert status == 404 and body["error"]["code"] == "not_found_error"
    assert "No request has been recorded" in body["error"]["message"]
    status, body, _ = post(api, "preview", {"request_id": "gone"})
    assert status == 404 and "gone" in body["error"]["message"] and "no longer kept" in body["error"]["message"]
    assert api.store.called("preview") == []


@pytest.mark.parametrize("payload,param", [
    ({"request": []}, "request"), ({"request": "text"}, "request"), ({"request": 5, "dialect": "chat"}, "request"),
    ({"request": {}}, "dialect"), ({"request": {}, "dialect": None}, "dialect"),
    ({"request": {}, "dialect": "openai"}, "dialect"), ({"request": {}, "dialect": ["chat"]}, "dialect"),
    ({"request": {}, "dialect": "chat", "headers": []}, "headers"),
    ({"request": {}, "dialect": "chat", "headers": {"a": 1}}, "headers"),
    ({"request": {}, "dialect": "chat", "headers": {"a": None}}, "headers"),
    ({"request": {}, "dialect": "chat", "headers": {"k%d" % i: "v" for i in range(129)}}, "headers"),
    ({"request": {}, "dialect": "chat", "headers": {"a": "x" * 8193}}, "headers"),
    ({"request_id": 5}, "request_id"), ({"request_id": ""}, "request_id"), ({"request_id": "x" * 129}, "request_id"),
    ({"request": {}, "dialect": "chat", "document": []}, "document"),
    ({"request": {}, "dialect": "chat", "document": "text"}, "document"),
    ({"request": {}, "dialect": "chat", "unknown": 1}, "unknown"),
])
def test_preview_input_is_validated_before_the_store_is_asked(api, payload, param):
    status, body, _ = post(api, "preview", payload)
    assert status == 400 and body["error"]["param"] == param
    assert api.store.called("preview") == []


def test_a_preview_draft_is_checked_like_a_saved_document(api, monkeypatch):
    status, answer = raw(api, "POST", "/admin/api/prompts/preview", json.dumps(
        {"request": {}, "dialect": "chat", "document": {"texts": {"t": "\ud800"}}}).encode("ascii"))
    assert status == 400 and answer["error"]["param"] == "texts.t"
    assert post(api, "preview", {"request": {}, "dialect": "chat", "document": deep(admin.PROMPTS_MAX_DEPTH)})[0] == 400
    monkeypatch.setattr(prompt_policy, "MAX_DOCUMENT_BYTES", 500)
    assert post(api, "preview", {"request": {}, "dialect": "chat", "document": {"x": "y" * 600}})[0] == 413
    assert api.store.called("preview") == []


def test_max_body_bytes_still_caps_the_preview(api):
    api.cfg["max_body_bytes"] = 1000
    status, body, _ = post(api, "preview", {"request": {"messages": [{"role": "user", "content": "x" * 2000}]},
                                            "dialect": "chat"}, attempts=4)
    assert status == 413 and "1000" in body["error"]["message"]
    assert api.store.calls == []


def test_a_draft_the_engine_refuses_is_a_400_with_the_path_of_the_field(api):
    api.store.preview_error = PromptConfigError("profiles.mine.client_system.edits[0].pattern", "the pattern is nested")
    status, body, _ = post(api, "preview", {"request": REQUEST, "dialect": "chat", "document": DOC})
    assert status == 400 and body["error"]["param"] == "profiles.mine.client_system.edits[0].pattern"
    assert body["error"]["message"] == "the pattern is nested"


@pytest.mark.parametrize("error", [
    apierr.ApiError(404, "not_found_error", "The model asks for the profile 'ghost', which does not exist",
                    param="model"),
    apierr.bad_request("x-proxi-profile names the profile 'ghost', which does not exist", param="x-proxi-profile"),
])
def test_what_the_engine_refuses_with_an_api_error_reaches_the_screen_as_it_is(api, error):
    api.store.preview_error = error
    status, body, _ = post(api, "preview", {"request": REQUEST, "dialect": "chat"})
    assert status == error.status and body["error"]["param"] == error.param
    assert body["error"]["message"] == error.message


def test_preview_looks_for_a_file_changed_by_hand_first(api):
    post(api, "preview", {"request": REQUEST, "dialect": "chat"})
    assert api.store.calls[0] == ("maybe_reload",) and api.store.calls[1][0] == "preview"


# -- GET /admin/api/prompts/recent ----------------------------------------------------------------
def heads(count):
    return [{"ts": 1000.0 + index, "request_id": "r%d" % index, "client": "opencode", "agent": "build",
             "model": "claude-opus-5-5", "purpose": "client-tools", "rule": "opencode", "profile": "mine", "rev": 1,
             "pinned": False, "head": "HEAD %d" % index, "parts": [], "history_changes": [], "tools_changes": []}
            for index in range(count)]


@pytest.mark.parametrize("query,asked,status", [
    ("", 50, 200), ("?limit=3", 3, 200), ("?limit=100", 100, 200), ("?limit=1000", 100, 200), ("?limit=0", 1, 200),
    ("?limit=-4", 1, 200), ("?limit=abc", None, 400), ("?limit=1.5", None, 400),
])
def test_recent_asks_for_a_bounded_number_of_heads(api, query, asked, status):
    api.store.heads = heads(120)
    reply = get(api, "/recent" + query)
    assert reply[0] == status
    if status == 200:
        assert api.store.called("recent") == [(asked,)] and len(reply[1]["items"]) == asked
        assert reply[1]["items"][0]["head"] == "HEAD 0"
    else:
        assert reply[1]["error"]["param"] == "limit" and api.store.called("recent") == []


def test_recent_hands_out_what_the_store_keeps_and_nothing_more(api):
    items = heads(2)
    items[0]["request"] = {"body": {"messages": ["PRIVATE"]}, "dialect": "chat", "headers": {"authorization": "x"}}
    items[0]["has_request"] = True
    api.store.heads = items
    status, body, _ = get(api, "/recent")
    assert status == 200 and set(body) == {"items"}
    assert "request" not in body["items"][0] and body["items"][0]["has_request"] is True
    assert body["items"][1] == items[1] and "PRIVATE" not in json.dumps(body)


def test_recent_of_an_empty_ring_is_an_empty_list(api):
    assert get(api, "/recent")[1] == {"items": []}


# -- /admin/api/prompts/pins ----------------------------------------------------------------------
PIN = {"conversation": "a1b2c3d4", "purpose": "client-tools", "rev": 3, "profile": "mine", "age_s": 42.5}


def test_pins_are_listed_as_the_store_reports_them_by_tag(api):
    assert get(api, "/pins")[1] == {"items": []}
    api.store.pinned = [PIN]
    assert get(api, "/pins")[1] == {"items": [PIN]}


def test_one_conversation_is_unpinned_by_the_tag_the_screen_shows(api):
    api.store.unpin_counts["a1b2c3d4"] = 2
    status, body, _ = post(api, "pins", {"action": "unpin", "conversation": "a1b2c3d4"})
    assert status == 200 and body == {"unpinned": 2}
    assert api.store.called("unpin") == [("a1b2c3d4",)]
    event = events("admin.prompt_pins")[0]
    assert event["level"] == "info" and event["conversation"] == "a1b2c3d4"
    assert event["data"] == {"action": "unpin", "unpinned": 2}


def test_a_store_that_knows_only_keys_is_asked_with_the_keys_of_the_open_conversations(api):
    pool = api.runtime.pool
    pool._conversations.update({"conv-key-one": object(), ("conv-key-two", "text"): object(),
                                (("parent", "subagent"), "text"): object()})
    wanted = {name: journal.tag(name) for name in ("conv-key-one", "conv-key-two", "parent")}
    api.store.unpin_counts.update({"conv-key-one": 1, "conv-key-two": 3, "parent": 2})
    for name, tag in wanted.items():
        status, body, _ = post(api, "pins", {"action": "unpin", "conversation": tag})
        assert status == 200 and body == {"unpinned": api.store.unpin_counts[name]}
    # The tag first, then the key it stands for.
    assert api.store.called("unpin") == [(wanted["conv-key-one"],), ("conv-key-one",), (wanted["conv-key-two"],),
                                         ("conv-key-two",), (wanted["parent"],), ("parent",)]
    # The journal knows the conversations by tag only, as everywhere else.
    encoded = json.dumps(journal.JOURNAL.query())
    assert "conv-key-one" not in encoded and "conv-key-two" not in encoded
    assert [event["conversation"] for event in events("admin.prompt_pins")] == list(wanted.values())
    pool._conversations.clear()


def test_a_tag_nobody_pinned_unpins_nothing(api):
    status, body, _ = post(api, "pins", {"action": "unpin", "conversation": "00000000"})
    assert status == 200 and body == {"unpinned": 0}
    assert api.store.called("unpin") == [("00000000",)]


def test_a_store_that_matches_the_tag_itself_is_asked_once(api):
    api.store.unpin_counts["a1b2c3d4"] = 1
    api.runtime.pool._conversations["some-key"] = object()
    assert post(api, "pins", {"action": "unpin", "conversation": "a1b2c3d4"})[1] == {"unpinned": 1}
    assert api.store.called("unpin") == [("a1b2c3d4",)]
    api.runtime.pool._conversations.clear()


@pytest.mark.parametrize("conversation", [None, "", "   ", 5, True, ["a1b2c3d4"], "x" * 257, "tag\nwith newline"])
def test_unpinning_one_conversation_needs_its_tag(api, conversation):
    status, body, _ = post(api, "pins", {"action": "unpin", "conversation": conversation})
    assert status == 400 and body["error"]["param"] == "conversation"
    assert api.store.called("unpin") == []
    status, body, _ = post(api, "pins", {"action": "unpin"})
    assert status == 400 and body["error"]["param"] == "conversation"


def test_apply_all_releases_every_pin_at_once(api):
    api.store.unpin_counts[None] = 4
    status, body, _ = post(api, "pins", {"action": "apply_all"})
    assert status == 200 and body == {"unpinned": 4}
    assert api.store.called("unpin") == [(None,)]
    event = events("admin.prompt_pins")[0]
    assert event["level"] == "warn" and event["data"] == {"action": "apply_all", "unpinned": 4}
    for conversation in (None, ""):
        assert post(api, "pins", {"action": "apply_all", "conversation": conversation})[1] == {"unpinned": 4}


def test_apply_all_is_not_narrowed_by_a_conversation(api):
    status, body, _ = post(api, "pins", {"action": "apply_all", "conversation": "a1b2c3d4"})
    assert status == 400 and body["error"]["param"] == "conversation"
    assert api.store.called("unpin") == []


@pytest.mark.parametrize("payload,param", [
    ({}, "action"), ({"action": None}, "action"), ({"action": "clear"}, "action"), ({"action": ["unpin"]}, "action"),
    ({"action": "apply_all", "all": True}, "all"), ({"action": "unpin", "conversation": "a", "reason": "x"}, "reason"),
])
def test_unknown_pin_actions_and_fields_are_400s(api, payload, param):
    status, body, _ = post(api, "pins", payload)
    assert status == 400 and body["error"]["param"] == param
    assert api.store.called("unpin") == []


# -- the real store -------------------------------------------------------------------------------
def test_the_real_store_has_nothing_recorded_pinned_or_previewable_at_first(admin_relay):
    assert get(admin_relay, "/recent")[1] == {"items": []} and get(admin_relay, "/pins")[1] == {"items": []}
    assert post(admin_relay, "pins", {"action": "apply_all"})[1] == {"unpinned": 0}
    assert post(admin_relay, "pins", {"action": "unpin", "conversation": "00000000"})[1] == {"unpinned": 0}
    assert post(admin_relay, "preview", {})[0] == 404  # nothing recorded yet


def test_the_real_store_previews_a_pasted_request_under_a_draft(admin_relay):
    real_engine_or_skip()
    request = {"model": "claude-opus-5-5", "messages": [{"role": "system", "content": "CLIENT SYSTEM"},
                                                        {"role": "user", "content": "hi"}]}
    draft = {"version": 1, "profiles": {"mine": {"head": "MINE\n\n{{client_system}}"}},
             "rules": [{"name": "all", "if": {}, "use": "mine"}]}
    status, body, _ = post(admin_relay, "preview", {"request": request, "dialect": "chat", "document": draft})
    assert status == 200 and body["profile"] == "mine" and body["head"].startswith("MINE")
    assert get(admin_relay)[1]["document"] is None  # previewing saved nothing
    status, body, _ = put(admin_relay, draft)
    assert status == 200
    # An invalid draft is refused with the path of its field, as a save would.
    bad = {"version": 1, "profiles": {"mine": {"head": "{{no_such_variable}}"}}}
    status, body, _ = post(admin_relay, "preview", {"request": request, "dialect": "chat", "document": bad})
    assert status == 400 and body["error"]["param"].startswith("profiles.mine")
