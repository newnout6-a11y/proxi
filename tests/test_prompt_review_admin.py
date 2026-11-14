"""The review fixes seen from the admin API: the places an operator can reach them from (PUT and the preview of a draft)."""
import json
import time

import pytest

from test_admin import admin_relay  # noqa: F401

REQUEST = {"model": "claude-opus-5-5", "messages": [{"role": "system", "content": "SYS"},
                                                     {"role": "user", "content": "hi"}]}
SECRETS = ("private-cookie-value", "private-relay-key", "private-xsrf-token")


def guard(api):
    return {"x-admin-request": "1", "origin": api.url}


def put(api, document, base_rev=None):
    return api.request("/admin/api/prompts", {"document": document, "base_rev": base_rev}, method="PUT",
                       headers=guard(api))


def preview(api, document=None, request=REQUEST):
    payload = {"request": request, "dialect": "chat", "headers": {}}
    if document is not None:
        payload["document"] = document
    return api.request("/admin/api/prompts/preview", payload, method="POST", headers=guard(api))


def leaks(body):
    text = json.dumps(body)
    return [secret for secret in SECRETS if secret in text]


def naming(file):
    return {"version": 1, "texts": {"x": {"file": file}}, "profiles": {"p": {"head": "{{text:x}}"}},
            "default_profile": "p"}


def test_a_draft_that_names_config_json_is_a_400_and_the_answer_holds_no_secret(admin_relay):
    status, body, _ = preview(admin_relay, naming("config.json"))
    assert status == 400 and body["error"]["param"] == "texts.x.file" and leaks(body) == []
    assert "prompts/" in body["error"]["message"]


def test_saving_a_document_that_names_config_json_is_a_400_and_nothing_is_written(admin_relay):
    status, body, _ = put(admin_relay, naming("config.json"))
    assert status == 400 and body["error"]["param"] == "texts.x.file" and leaks(body) == []
    assert not (admin_relay.path.parent / "prompts.json").exists()
    status, body, _ = preview(admin_relay)
    assert leaks(body) == [] and body["head"] == "SYS"


@pytest.mark.parametrize("name", ["config.json", "../config.json", "prompts/../config.json", "journal.jsonl", "prompts.json",
                                  "prompts/data.json", "C:/Windows/win.ini", "prompts/a.md:stream"])
def test_no_other_way_to_name_a_file_outside_prompts_is_accepted_either(admin_relay, name):
    for status, body, _ in (put(admin_relay, naming(name)), preview(admin_relay, naming(name))):
        assert status == 400 and body["error"]["param"] == "texts.x.file" and leaks(body) == []


def test_a_text_in_the_prompts_folder_works_through_the_api(admin_relay):
    folder = admin_relay.path.parent / "prompts"
    folder.mkdir()
    (folder / "rules.md").write_text("MY RULES\n", encoding="utf-8")
    status, body, _ = preview(admin_relay, naming("prompts/rules.md"))
    assert status == 200 and body["head"] == "MY RULES\n"
    status, body, _ = put(admin_relay, naming("prompts/rules.md"))
    assert status == 200 and body["rev"] == 1
    assert preview(admin_relay)[1]["head"] == "MY RULES\n"


def test_the_reviewers_266_kb_document_is_a_400_and_not_a_100_mib_head(admin_relay):
    document = {"version": 1, "texts": {"t": "x" * (256 * 1024 - 1)}, "profiles": {"p": {"head": "{{text:t}}" * 1000}},
                "default_profile": "p"}
    started = time.perf_counter()
    status, body, _ = put(admin_relay, document)
    assert status == 400 and body["error"]["param"] == "profiles.p.head"
    assert "without the client's system prompt" in body["error"]["message"]
    assert preview(admin_relay, document)[0] == 400
    assert time.perf_counter() - started < 5.0
    assert not (admin_relay.path.parent / "prompts.json").exists()


@pytest.mark.parametrize("pattern", ["a{99999999}", "(?:a{1000}){1000}", "a{e<=1}", "(a+)+$"])
def test_a_pattern_that_would_cost_too_much_or_mean_something_else_is_a_400(admin_relay, pattern):
    document = {"version": 1, "profiles": {"p": {"client_system": {"edits": [{"op": "remove_regex", "pattern": pattern}]}}}}
    started = time.perf_counter()
    status, body, _ = put(admin_relay, document)
    assert status == 400 and body["error"]["param"] == "profiles.p.client_system.edits[0].pattern"
    assert time.perf_counter() - started < 2.0


def test_the_error_of_a_pattern_is_shown_to_the_operator_and_the_prompt_events_do_not_echo_it(admin_relay):
    import journal
    marker = "SECRET_GROUP_NAME_XYZ"
    document = {"version": 1, "profiles": {"p": {"client_system": {"edits": [{"op": "remove_regex",
                                                                               "pattern": "(?P=%s)" % marker}]}}}}
    mark = journal.JOURNAL.last_seq
    status, body, _ = put(admin_relay, document)
    assert status == 400 and marker in body["error"]["message"]          # the operator who typed it sees what is wrong
    # The events of the prompt layer carry none of it. (The HTTP layer journals the message of any 400 it answers, the
    # one to this request too; that is its own, general, behaviour.)
    own = [event for event in journal.JOURNAL.query(since=mark, limit=5000, order="asc")
           if event["kind"].startswith(("prompt.", "admin."))]
    assert marker not in json.dumps(own)
