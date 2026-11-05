"""The prompt control admin API, part 2: PUT /admin/api/prompts (plan 4.1): revisions, limits, errors, the journal event.

runtime.prompts is a fake that follows the PromptStore contract (admin_prompts_harness). The tests at the end use the
real store and skip while its engine is not part of the branch.
"""
import copy
import http.client
import json
import threading
import time

import pytest

import admin
import apierr
import journal
import prompt_policy
from admin_prompts_harness import (CSRF_CASES, DOC, KEY, MARKER, api, check_csrf, check_needs_the_key, deep, events,  # noqa: F401
                                   get, guard, put, raw, real_engine_or_skip)
from prompt_policy import PromptConfigError
from test_admin import admin_relay  # noqa: F401 - the relay the api fixture builds on


# -- who may save ---------------------------------------------------------------------------------
@pytest.mark.parametrize("key", [None, "incorrect-key"])
def test_saving_needs_the_relay_key(api, key):
    check_needs_the_key(api, "PUT", "/admin/api/prompts", {"document": DOC, "base_rev": None}, key)


@pytest.mark.parametrize("headers,status", CSRF_CASES)
def test_saving_needs_the_csrf_header_the_same_origin_and_json(api, headers, status):
    check_csrf(api, "PUT", "/admin/api/prompts", {"document": DOC, "base_rev": None}, headers, status)


def test_a_same_origin_admin_request_is_accepted_with_or_without_an_origin_header(api):
    assert put(api, DOC, headers={"x-admin-request": "1"})[0] == 200  # no Origin: a non-browser client
    assert put(api, DOC, 1, headers={"x-admin-request": "1", "origin": api.url,
                                     "sec-fetch-site": "same-origin"})[0] == 200


def test_the_answer_carries_the_admin_security_headers_and_no_cors(api):
    status, _, headers = put(api, DOC)
    assert status == 200
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY" and "access-control-allow-origin" not in headers


# -- the save itself ------------------------------------------------------------------------------
def test_a_valid_document_is_saved_and_answered_with_its_revision(api):
    status, body, _ = put(api, DOC)
    assert status == 200 and body == {"rev": 1, "warnings": []}
    assert api.store.called("save") == [(DOC, None)]
    changed = copy.deepcopy(DOC)
    changed["texts"]["my_rules"] = "Другое правило"
    status, body, _ = put(api, changed, 1)
    assert status == 200 and body["rev"] == 2
    assert api.store.called("save")[-1] == (changed, 1)
    state = get(api)[1]
    assert state["rev"] == 2 and state["document"] == changed


def test_saving_looks_for_a_file_changed_by_hand_before_it_compares_revisions(api):
    assert put(api, DOC)[0] == 200
    assert api.store.calls[0] == ("maybe_reload",) and api.store.calls[1][0] == "save"


def test_a_stale_base_revision_is_a_409_that_saves_nothing(api):
    assert put(api, DOC)[0] == 200
    for base in (None, 0, 5):
        status, body, _ = put(api, DOC, base)
        assert status == 409 and body["error"]["code"] == "conflict"
        assert "revision" in body["error"]["message"] and "now: 1" in body["error"]["message"]
    assert len(api.store.called("save")) == 1 and api.store.snapshot().rev == 1
    assert len(events("admin.prompts")) == 1


def test_a_base_revision_for_a_document_that_does_not_exist_is_stale(api):
    status, body, _ = put(api, DOC, 1)
    assert status == 409 and "now: none" in body["error"]["message"]
    assert api.store.called("save") == [] and api.store.snapshot() is None


def test_a_revision_taken_between_the_check_and_the_save_is_a_409_too(api):
    api.store.save_error = apierr.conflict("another save won")
    status, body, _ = put(api, DOC)
    assert status == 409 and body["error"]["message"] == "another save won"
    assert events("admin.prompts") == []


def test_a_document_the_engine_refuses_is_a_400_with_the_path_of_the_field(api):
    api.store.save_error = PromptConfigError("profiles.mine.head", "unknown variable {{nope}}")
    status, body, _ = put(api, DOC)
    assert status == 400 and body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "profiles.mine.head" and body["error"]["message"] == "unknown variable {{nope}}"
    assert api.store.snapshot() is None and get(api)[1]["document"] is None
    assert events("admin.prompts") == []


def test_an_error_without_a_path_names_no_field(api):
    api.store.save_error = PromptConfigError("", "the document must be an object with a version")
    status, body, _ = put(api, DOC)
    assert status == 400 and body["error"]["param"] is None
    assert body["error"]["message"] == "the document must be an object with a version"


def test_a_disk_failure_is_a_503_and_keeps_the_revision(api):
    assert put(api, DOC)[0] == 200
    api.store.save_error = OSError("disk is full")
    status, body, _ = put(api, DOC, 1)
    assert status == 503 and "disk is full" not in json.dumps(body)
    api.store.save_error = None
    assert get(api)[1]["rev"] == 1 and len(events("admin.prompts")) == 1


@pytest.mark.parametrize("payload,param", [
    ({}, "document"),
    ({"document": None}, "document"),
    ({"document": []}, "document"),
    ({"document": "text"}, "document"),
    ({"document": 5}, "document"),
    ({"document": {}, "extra": 1}, "extra"),
    ({"document": {}, "base_rev": "1"}, "base_rev"),
    ({"document": {}, "base_rev": True}, "base_rev"),
    ({"document": {}, "base_rev": -1}, "base_rev"),
    ({"document": {}, "base_rev": 1.5}, "base_rev"),
    ({"document": {}, "base_rev": [1]}, "base_rev"),
])
def test_a_malformed_save_is_a_400_that_names_the_field_and_touches_nothing(api, payload, param):
    status, body, _ = api.request("/admin/api/prompts", payload, method="PUT", headers=guard(api))
    assert status == 400 and body["error"]["param"] == param
    assert api.store.called("save") == []


@pytest.mark.parametrize("body,fragment", [
    pytest.param(b"[1, 2]", "JSON object", id="not-an-object"),
    pytest.param(b"{not json", "invalid JSON", id="broken"),
    pytest.param(b'{"document": {"a": 1,}}', "invalid JSON", id="trailing-comma"),
    pytest.param(b'{"document": "\xff\xfe"}', "UTF-8", id="not-utf8"),
    pytest.param(b"1" + b"0" * 5000, "JSON", id="integer-over-the-digit-limit"),
    pytest.param(b'{"document": ' + b"[" * 100_000 + b"]" * 100_000 + b"}", "nested too deeply", id="recursion"),
])
def test_a_body_that_is_not_json_is_a_400_never_a_crash(api, body, fragment):
    status, answer = raw(api, "PUT", "/admin/api/prompts", body)
    assert status == 400 and fragment in answer["error"]["message"]
    assert get(api)[0] == 200  # the relay is still answering


def test_an_empty_body_is_a_400(api):
    status, answer = raw(api, "PUT", "/admin/api/prompts", b"")
    assert status == 400 and answer["error"]["code"] == "invalid_request_error"


def test_a_document_nested_beyond_the_limit_is_a_400_at_the_level_it_gives_up(api):
    ok = {"profiles": {"mine": {"history": {"user_blocks": [{"op": "drop_tag"}]}}}}  # the deepest valid path
    assert put(api, ok)[0] == 200
    limit = admin.PROMPTS_MAX_DEPTH
    status, body, _ = put(api, deep(limit), 1)  # the innermost object is the level after the last allowed one
    assert status == 400 and str(limit) in body["error"]["message"]
    assert body["error"]["param"] == ".".join("n" * limit) and len(body["error"]["param"]) == 2 * limit - 1
    status, body, _ = put(api, deep(limit - 1), 1)  # all its objects fit: its text is the first thing too deep
    assert status == 400 and body["error"]["param"] == ".".join("n" * (limit - 1)) + ".head"
    assert put(api, deep(limit - 2), 1)[0] == 200  # a text exactly at the limit still fits


def test_text_that_utf8_cannot_store_is_a_400_naming_the_field_and_never_echoing_it(api):
    half = "half of a pair \ud83d"
    status, answer = raw(api, "PUT", "/admin/api/prompts",
                         json.dumps({"document": {"texts": {"t": half}}, "base_rev": None}).encode("ascii"))
    assert status == 400 and answer["error"]["param"] == "texts.t" and "surrogate" in answer["error"]["message"]
    assert "half of a pair" not in json.dumps(answer)
    # In a list, and as a key: the key's text is not repeated, the parent is named.
    status, answer = raw(api, "PUT", "/admin/api/prompts",
                         json.dumps({"document": {"rules": [{"name": "\udfff"}]}, "base_rev": None}).encode("ascii"))
    assert status == 400 and answer["error"]["param"] == "rules[0].name"
    status, answer = raw(api, "PUT", "/admin/api/prompts",
                         json.dumps({"document": {"texts": {"bad\ud800key": "x"}}, "base_rev": None}).encode("ascii"))
    assert status == 400 and answer["error"]["param"] == "texts" and "bad" not in json.dumps(answer)
    # A complete pair written as two escapes is an ordinary character.
    assert raw(api, "PUT", "/admin/api/prompts", json.dumps({"document": {"texts": {"t": "ok \U0001F600"}},
                                                             "base_rev": None}).encode("ascii"))[0] == 200
    assert len(api.store.called("save")) == 1


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_a_number_json_cannot_carry_is_a_400(api, literal):
    status, answer = raw(api, "PUT", "/admin/api/prompts", b'{"document": {"profiles": {"p": {"w": ' + literal + b"}}}}")
    assert status == 400 and answer["error"]["param"] == "profiles.p.w" and "not finite" in answer["error"]["message"]
    assert api.store.called("save") == []


# -- size limits ----------------------------------------------------------------------------------
def test_a_body_over_the_limit_of_the_save_route_is_a_413_whatever_max_body_bytes_says(api, monkeypatch):
    assert api.cfg["max_body_bytes"] >= admin.PROMPTS_BODY_LIMIT  # the default would have let it in
    monkeypatch.setattr(admin, "PROMPTS_BODY_LIMIT", 3000)
    big = {"texts": {"t": "x" * 5000}}
    status, body, _ = put(api, big)
    assert status == 413 and body["error"]["code"] == "payload_too_large" and "3000" in body["error"]["message"]
    assert api.store.calls == []
    assert put(api, {"texts": {"t": "x" * 100}})[0] == 200  # under the limit it is as before


def test_the_limit_is_judged_on_the_declared_length_before_a_byte_is_read(api):
    host, port = api.url.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        connection.putrequest("PUT", "/admin/api/prompts")
        for name, value in {"authorization": "Bearer " + KEY, "content-type": "application/json",
                            "content-length": str(admin.PROMPTS_BODY_LIMIT + 1), **guard(api)}.items():
            connection.putheader(name, value)
        connection.endheaders()  # the promised body never comes
        reply = connection.getresponse()
        assert reply.status == 413 and json.loads(reply.read())["error"]["code"] == "payload_too_large"
        # The unread body would be taken for the next request: the connection is closed behind the answer.
        with pytest.raises((http.client.HTTPException, OSError)):
            connection.request("GET", "/health")
            connection.getresponse().read()
    finally:
        connection.close()
    assert api.store.calls == []


def test_max_body_bytes_still_caps_the_save(api):
    api.cfg["max_body_bytes"] = 1000
    status, body, _ = put(api, {"texts": {"t": "x" * 2000}})
    assert status == 413 and "1000" in body["error"]["message"]
    assert api.store.calls == []


def test_a_document_is_judged_in_utf8_bytes_and_the_limit_itself_is_accepted(api, monkeypatch):
    monkeypatch.setattr(prompt_policy, "MAX_DOCUMENT_BYTES", 4096)
    overhead = len(json.dumps({"x": ""}, separators=(",", ":")).encode())
    exact = {"x": "a" * (4096 - overhead)}
    assert put(api, exact)[0] == 200
    status, body, _ = put(api, {"x": "a" * (4096 - overhead + 1)}, 1)
    assert status == 413 and body["error"]["code"] == "payload_too_large" and "4096" in body["error"]["message"]
    # Two bytes a character: half as many characters hit the same limit.
    assert put(api, {"x": "ж" * ((4096 - overhead) // 2)}, 1)[0] == 200
    assert put(api, {"x": "ж" * ((4096 - overhead) // 2 + 1)}, 2)[0] == 413
    assert len(api.store.called("save")) == 2


def test_a_document_the_size_of_the_real_limit_fits_the_route_even_when_the_client_escapes_every_letter(api):
    # json.dumps escapes non-ASCII text (three bytes of body for each two of Cyrillic): the route must still take it.
    text = "ж" * ((prompt_policy.MAX_DOCUMENT_BYTES - 64) // 2)
    body = json.dumps({"document": {"x": text}, "base_rev": None}).encode("ascii")
    assert 2.9 < len(body) / len(text.encode()) < 3.1 and len(body) < admin.PROMPTS_BODY_LIMIT
    status, answer = raw(api, "PUT", "/admin/api/prompts", body)
    assert status == 200 and answer["rev"] == 1


# -- the journal ----------------------------------------------------------------------------------
def test_saving_journals_the_revision_the_changed_paths_and_a_fingerprint_without_the_text(api):
    assert put(api, DOC)[0] == 200
    changed = copy.deepcopy(DOC)
    changed["texts"]["my_rules"] = MARKER + " новое"
    assert put(api, changed, 1)[0] == 200
    first, second = events("admin.prompts")
    for event, rev, paths, document in ((first, 1, ["profiles", "rules", "texts", "version"], DOC),
                                        (second, 2, ["texts.my_rules"], changed)):
        assert event["level"] == "info" and event["category"] == "admin"
        assert event["data"] == {"rev": rev, "changed_paths": paths,
                                 "sha8": prompt_policy.sha8(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                                                       separators=(",", ":")))}
        assert len(event["data"]["sha8"]) == 8
    assert first["data"]["sha8"] != second["data"]["sha8"]
    assert MARKER not in json.dumps(journal.JOURNAL.query(), ensure_ascii=False)


def test_a_save_that_changes_nothing_still_leaves_its_event(api):
    assert put(api, DOC)[0] == 200
    assert put(api, DOC, 1)[0] == 200
    event = events("admin.prompts")[-1]
    assert event["data"]["changed_paths"] == [] and event["data"]["rev"] == 2
    assert events("admin.prompts")[0]["data"]["sha8"] == event["data"]["sha8"]


def test_no_text_of_a_refused_save_reaches_the_journal_either(api):
    api.store.save_error = PromptConfigError("texts.my_rules", "the text is not allowed")
    assert put(api, DOC)[0] == 400
    time.sleep(0.05)
    assert MARKER not in json.dumps(journal.JOURNAL.query(), ensure_ascii=False)


@pytest.mark.parametrize("old,new,paths", [
    ({}, {}, []),
    ({}, {"version": 1, "texts": {"a": "x"}}, ["texts", "version"]),
    ({"texts": {"a": "x"}}, {"texts": {"a": "y"}}, ["texts.a"]),
    ({"texts": {"a": "x", "b": "z"}}, {"texts": {"a": "x"}}, ["texts.b"]),
    ({"texts": {"a": "x"}}, {"texts": {"a": "x", "b": "z"}}, ["texts.b"]),
    ({"profiles": {"mine": {"head": "a", "tools": {"edits": []}}}},
     {"profiles": {"mine": {"head": "b", "tools": {"edits": [{"tool": "bash"}]}}}},
     ["profiles.mine.head", "profiles.mine.tools.edits"]),
    ({"profiles": {"a": {"head": "x"}}}, {"profiles": {"a": {"head": "x"}, "b": {"head": "y"}}}, ["profiles.b"]),
    ({"profiles": {"a": {"head": "x"}, "b": {"head": "y"}}}, {"profiles": {"a": {"head": "x"}}}, ["profiles.b"]),
    ({"rules": [{"use": "a"}, {"use": "b"}]}, {"rules": [{"use": "a"}, {"use": "c"}]}, ["rules"]),
    ({"rules": [{"use": "a"}]}, {"rules": [{"use": "a"}]}, []),
    ({"default_profile": None}, {"default_profile": "mine"}, ["default_profile"]),
    ({"protected": {"enabled": True}}, {"protected": {"enabled": 1}}, ["protected.enabled"]),  # true is not 1
    ({"x": 1}, {"x": 1.0}, ["x"]),
    ({"texts": {"a": "x"}}, {"texts": ["x"]}, ["texts"]),
    ({"a": {"b": {"c": 1}}}, {"a": {"b": {"c": 2}}}, ["a.b.c"]),
])
def test_changed_paths_name_what_differs_from_the_previous_document(old, new, paths):
    assert admin._changed_paths(old, new) == paths


def test_the_event_lists_at_most_as_many_paths_as_the_journal_keeps(api):
    many = {"texts": {"t%03d" % index: "x" for index in range(100)}}
    assert put(api, many)[0] == 200
    changed = {"texts": {"t%03d" % index: "y" for index in range(100)}}
    assert put(api, changed, 1)[0] == 200
    paths = events("admin.prompts")[-1]["data"]["changed_paths"]
    assert len(paths) == admin.CHANGED_PATHS_LIMIT == 64 and paths == sorted(paths)


def test_the_save_answers_with_the_warnings_of_the_document_and_those_of_the_engine(api):
    api.store.snapshot_warnings = ["профиль «mine» не используется ни одним правилом"]
    status, body, _ = put(api, {**DOC, "protected": {"enabled": False}})
    assert status == 200 and body["rev"] == 1 and len(body["warnings"]) == 2
    assert "protected.enabled" in body["warnings"][0] and body["warnings"][1] == api.store.snapshot_warnings[0]
    api.store.snapshot_warnings = [body["warnings"][0]]  # the same warning twice is shown once
    status, body, _ = put(api, {**DOC, "protected": {"enabled": False}}, 1)
    assert body["warnings"] == [body["warnings"][0]] and len(body["warnings"]) == 1


def test_saves_that_start_from_one_revision_are_serialised_and_only_one_wins(api):
    assert put(api, DOC)[0] == 200
    results = []

    def attempt(index):
        changed = copy.deepcopy(DOC)
        changed["texts"]["my_rules"] = "writer %d" % index
        results.append(put(api, changed, 1)[0])

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(results) == [200] + [409] * 5
    assert api.store.snapshot().rev == 2 and len(events("admin.prompts")) == 2
    # The event of the winner compares with the revision it replaced, never with a half-written state.
    assert events("admin.prompts")[-1]["data"]["changed_paths"] == ["texts.my_rules"]


# -- the real store -------------------------------------------------------------------------------
def test_the_real_store_saves_serves_and_refuses_documents(admin_relay):
    real_engine_or_skip()
    status, body, _ = put(admin_relay, DOC)
    assert status == 200 and isinstance(body["rev"], int) and body["warnings"] == []
    saved = admin_relay.path.parent / "prompts.json"
    assert saved.exists() and not list(saved.parent.glob(".admin-*.tmp"))
    state = get(admin_relay)[1]
    assert state["rev"] == body["rev"] and state["document"]["profiles"]["mine"] == DOC["profiles"]["mine"]
    assert put(admin_relay, DOC, None)[0] == 409  # a stale base: the document exists now
    status, answer, _ = put(admin_relay, {"version": 1, "profiles": {"mine": {"head": "{{no_such_variable}}"}}},
                            body["rev"])
    assert status == 400 and answer["error"]["param"].startswith("profiles.mine")
    assert get(admin_relay)[1]["rev"] == body["rev"]  # the refused edit changed nothing
    assert MARKER not in json.dumps(journal.JOURNAL.query(), ensure_ascii=False)
    assert events("admin.prompts")[-1]["data"]["rev"] == body["rev"]
