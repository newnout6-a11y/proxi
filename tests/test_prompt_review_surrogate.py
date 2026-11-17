"""R1-07: half of a surrogate pair ("\\ud83d") in prompts.json is refused when the revision is compiled.

JSON can carry it, but no UTF-8 text can: a document that was accepted made every request that used its text fail with a 500
UnicodeEncodeError while the head was encoded (and /codex/models too, for codex_base).
"""
import json

import pytest

import prompt_policy as pp
from prompt_support import client_request, prompt_relay  # noqa: F401
from test_prompt_policy_common import document, make_store

HALF = "\ud83d"


def refused(doc, tmp_path):
    with pytest.raises(pp.PromptConfigError) as raised:
        pp.compile_document(doc, tmp_path, rev=1)
    return raised.value


def test_the_reviewers_document_is_refused_at_the_field(tmp_path):
    doc = json.loads('{"version": 1, "texts": {"t": "Be concise \\ud83d"}, "profiles": {"p": {"head": '
                     '"{{text:t}}\\n\\n{{client_system}}"}}, "default_profile": "p"}')
    error = refused(doc, tmp_path)
    assert error.path == "texts.t" and "surrogate" in error.message


@pytest.mark.parametrize("fragment", ["\ud83d", "\ude00", "\ud800", "\udfff", "a\ud83d", "\ud83dx", "\ude00\ud83d"])
def test_any_half_of_a_pair_is_refused(tmp_path, fragment):
    assert refused(document(texts={"t": "x " + fragment + " y"}), tmp_path).path == "texts.t"


def test_a_whole_pair_is_an_ordinary_character(tmp_path):
    doc = json.loads('{"version": 1, "texts": {"t": "ok \\ud83d\\ude00 and \\ud83d\\ude80"}, "profiles": {"p": '
                     '{"head": "{{text:t}}"}}}')
    assert doc["texts"]["t"] == "ok \U0001F600 and \U0001F680"
    snapshot = pp.compile_document(doc, tmp_path, rev=1)
    assert snapshot.document["texts"]["t"].encode("utf-8")


FIELDS = {
    "texts.t": lambda s: document(texts={"t": s}),
    "profiles.p.description": lambda s: document(profiles={"p": {"description": s}}),
    "profiles.p.head": lambda s: document(profiles={"p": {"head": s}}),
    "profiles.p.client_system.edits[0].text": lambda s: document(profiles={"p": {"client_system": {"edits": [
        {"op": "append", "text": s}]}}}),
    "profiles.p.client_system.edits[0].find": lambda s: document(profiles={"p": {"client_system": {"edits": [
        {"op": "replace_text", "find": s, "with": "x"}]}}}),
    "profiles.p.client_system.edits[0].with": lambda s: document(profiles={"p": {"client_system": {"edits": [
        {"op": "replace_text", "find": "a", "with": s}]}}}),
    "profiles.p.client_system.edits[0].heading": lambda s: document(profiles={"p": {"client_system": {"edits": [
        {"op": "remove_section", "heading": "# " + s}]}}}),
    "profiles.p.client_system.edits[0].pattern": lambda s: document(profiles={"p": {"client_system": {"edits": [
        {"op": "remove_regex", "pattern": "x" + s}]}}}),
    "profiles.p.history.user_blocks[0].contains": lambda s: document(profiles={"p": {"history": {"user_blocks": [
        {"op": "drop_tag", "tag": "t", "contains": s}]}}}),
    "profiles.p.tools.edits[0].tool": lambda s: document(profiles={"p": {"tools": {"edits": [
        {"tool": s, "hide": True}]}}}),
    "profiles.p.tools.edits[0].text": lambda s: document(profiles={"p": {"tools": {"edits": [
        {"tool": "t", "op": "append", "text": s}]}}}),
    "profiles.p.publish_as_model[0]": lambda s: document(profiles={"p": {"publish_as_model": [s]}}),
    "relay_texts.role": lambda s: document(relay_texts={"role": s}),
    "relay_texts.native_role": lambda s: document(relay_texts={"native_role": s}),
    "relay_texts.reminder": lambda s: document(relay_texts={"reminder": "{{text}}" + s}),
    "relay_texts.structured": lambda s: document(relay_texts={"structured": "{{default}}" + s}),
    "relay_texts.codex_base": lambda s: document(relay_texts={"codex_base": s}),
    "relay_texts.codex_base.gpt-*": lambda s: document(relay_texts={"codex_base": {"gpt-*": s}}),
    "codex.models.gpt-*.persistent_instructions": lambda s: document(
        codex={"models": {"gpt-*": {"persistent_instructions": s}}}),
    "rules[0].name": lambda s: document(rules=[{"name": s, "use": "passthrough"}]),
    "rules[0].if.agent": lambda s: document(rules=[{"name": "r", "use": "passthrough", "if": {"agent": s}}]),
    "rules[0].if.model[1]": lambda s: document(rules=[{"name": "r", "use": "passthrough",
                                                        "if": {"model": ["gpt-*", s]}}]),
}


@pytest.mark.parametrize("field", list(FIELDS))
def test_every_text_field_is_checked_and_named(tmp_path, field):
    error = refused(FIELDS[field](HALF), tmp_path)
    assert error.path == field, (error.path, error.message)
    assert "surrogate" in error.message


@pytest.mark.parametrize("field", list(FIELDS))
def test_the_same_fields_with_a_whole_emoji_are_fine(tmp_path, field):
    value = "\U0001F600"
    try:
        pp.compile_document(FIELDS[field](value), tmp_path, rev=1)
    except pp.PromptConfigError as error:
        assert "surrogate" not in error.message


def test_a_key_with_half_a_pair_is_refused_without_quoting_it(tmp_path):
    error = refused(document(texts={"name" + HALF: "x"}), tmp_path)
    assert error.path == "texts" and "surrogate" in error.message and HALF not in str(error)
    error = refused({"version": 1, HALF: 1}, tmp_path)
    assert error.path == "" and HALF not in str(error)
    error = refused(document(profiles={"p": {"head": "x", HALF: 1}}), tmp_path)
    assert error.path == "profiles.p" and HALF not in str(error)


def test_the_error_of_a_document_with_half_a_pair_is_still_encodable(tmp_path):
    error = refused(document(texts={"t": HALF}), tmp_path)
    str(error).encode("utf-8")
    json.dumps({"param": error.path, "message": error.message}).encode("utf-8")


def test_a_hostile_nesting_with_a_surrogate_inside_does_not_crash_the_check(tmp_path):
    deep = HALF
    for _ in range(3000):
        deep = [deep]
    with pytest.raises(pp.PromptConfigError):
        pp.compile_document(document(texts={"a": deep}), tmp_path, rev=1)


def test_a_hand_edited_file_with_half_an_emoji_is_never_loaded(tmp_path):
    raw = ('{"version": 1, "texts": {"t": "Be concise \\ud83d"}, "profiles": {"p": {"head": "{{text:t}}\\n\\n'
           '{{client_system}}"}}, "default_profile": "p"}')
    (tmp_path / "prompts.json").write_text(raw, encoding="utf-8")
    store = pp.PromptStore({"prompts_file": str(tmp_path / "prompts.json")})
    assert store.snapshot() is None
    assert store.errors()[0]["param"] == "texts.t" and "surrogate" in store.errors()[0]["message"]


def test_a_good_revision_stays_when_the_file_gains_half_an_emoji(tmp_path):
    from test_prompt_policy_common import Clock
    clock = Clock()
    store = make_store(tmp_path, document(profiles={"p": {"head": "GOOD"}}, default_profile="p"), clock=clock)
    assert store.snapshot().rev == 1
    (tmp_path / "prompts.json").write_text(
        '{"version": 1, "profiles": {"p": {"head": "BAD \\ud83d"}}, "default_profile": "p"}', encoding="utf-8")
    import os
    info = (tmp_path / "prompts.json").stat()
    os.utime(tmp_path / "prompts.json", ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
    clock.advance(pp.RELOAD_INTERVAL + 0.01)
    store.maybe_reload()
    assert store.snapshot().rev == 1 and store.snapshot().profile("p")["head"] == "GOOD"
    assert store.errors()[0]["param"] == "profiles.p.head"


def test_save_refuses_half_an_emoji_and_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(pp.PromptConfigError) as raised:
        store.save(document(texts={"t": HALF}), None)
    assert raised.value.path == "texts.t" and not (tmp_path / "prompts.json").exists()


def test_a_draft_of_the_preview_with_half_an_emoji_is_refused(tmp_path):
    from test_prompt_policy_preview import chat
    store = make_store(tmp_path)
    with pytest.raises(pp.PromptConfigError) as raised:
        store.preview(chat(), "chat", {}, document=document(texts={"t": HALF}, profiles={"d": {"head": "{{text:t}}"}},
                                                           default_profile="d"))
    assert raised.value.path == "texts.t"


def test_a_request_is_served_by_the_legacy_path_when_the_file_has_half_an_emoji(prompt_relay, tmp_path):
    raw = ('{"version": 1, "texts": {"t": "Be concise \\ud83d"}, "profiles": {"p": {"head": "{{text:t}}\\n\\n'
           '{{client_system}}"}}, "default_profile": "p"}')
    (tmp_path / "prompts.json").write_text(raw, encoding="utf-8")
    relay = prompt_relay()
    assert relay.runtime.prompts.snapshot() is None
    status, payload, _headers = relay.post("chat", client_request(
        "chat", [{"role": "user", "content": "hi"}], system="S " * 400, tools=False, stream=False))
    assert status == 200, payload
    assert len(relay.posts) == 1
    assert relay.posts[0]["messages"][0]["content"][-1]["text"].startswith("S S")


def test_codex_models_does_not_fail_for_a_file_with_half_an_emoji(prompt_relay, tmp_path):
    import http.client
    from prompt_support import KEY
    (tmp_path / "prompts.json").write_text(
        '{"version": 1, "relay_texts": {"codex_base": "base \\ud83d"}}', encoding="utf-8")
    relay = prompt_relay()
    connection = http.client.HTTPConnection("127.0.0.1", relay.port, timeout=30)
    try:
        connection.request("GET", "/codex/models", headers={"authorization": "Bearer " + KEY})
        reply = connection.getresponse()
        reply.read()
        assert reply.status == 200
    finally:
        connection.close()
