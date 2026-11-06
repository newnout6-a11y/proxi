"""The model catalogs look at the prompts file themselves (R2-03): a client that only lists models never sends a completion."""
import http.client
import json

import pytest

import journal
import prompt_policy
from prompt_support import KEY, prompt_relay  # noqa: F401 - prompt_relay is a fixture

MODEL = "claude-opus-5-5"
BASE_TEXT = "BASE TEXT FROM THE FILE"


class Clock:
    """The store's clock, moved by hand: the store looks at the disk at most once per RELOAD_INTERVAL."""

    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=prompt_policy.RELOAD_INTERVAL):
        self.now += seconds


def document(profile="mine", codex_base=None):
    doc = {"version": 1, "profiles": {profile: {"head": "{{client_system}}", "publish_as_model": [MODEL]}}}
    if codex_base:
        doc["relay_texts"] = {"codex_base": codex_base}
    return doc


def listing(relay, path):
    connection = http.client.HTTPConnection("127.0.0.1", relay.port, timeout=30)
    try:
        connection.request("GET", path, headers={"authorization": "Bearer " + KEY})
        reply = connection.getresponse()
        data = json.loads(reply.read().decode())
        assert reply.status == 200, data
    finally:
        connection.close()
    return data["data"] if "data" in data else data["models"]


def virtual(relay, path, key):
    return [row[key] for row in listing(relay, path) if "@" in row[key]]


@pytest.fixture
def clocked(prompt_relay):
    relay = prompt_relay()
    clock = Clock()
    relay.runtime.prompts.clock = clock
    relay.runtime.prompts._checked = clock.now - 10
    return relay, clock


@pytest.mark.parametrize("path,key", [("/v1/models", "id"), ("/codex/models", "slug")])
def test_a_catalog_follows_the_prompts_file_without_any_completion(clocked, tmp_path, path, key):
    relay, clock = clocked
    file = tmp_path / "prompts.json"
    assert virtual(relay, path, key) == []
    file.write_text(json.dumps(document("mine")), encoding="utf-8")
    # the store stats the disk once per interval: the listing a moment later does not see the file yet
    assert virtual(relay, path, key) == []
    clock.advance()
    assert virtual(relay, path, key) == [MODEL + "@mine"]
    file.write_text(json.dumps(document("other")), encoding="utf-8")
    clock.advance()
    assert virtual(relay, path, key) == [MODEL + "@other"]
    file.unlink()
    # a removal counts only when two looks RELOAD_INTERVAL apart both miss the file (an editor deletes and recreates it)
    clock.advance()
    assert virtual(relay, path, key) == [MODEL + "@other"]
    clock.advance()
    assert virtual(relay, path, key) == []
    assert not [event for event in journal.JOURNAL.query(limit=5000) if event["kind"] == "prompt.runtime_error"]


def test_the_codex_catalog_carries_the_base_text_of_a_file_that_appeared_after_the_start(clocked, tmp_path):
    relay, clock = clocked
    built_in = listing(relay, "/codex/models")[0]["base_instructions"]
    assert built_in and built_in != BASE_TEXT
    file = tmp_path / "prompts.json"
    file.write_text(json.dumps(document(codex_base=BASE_TEXT)), encoding="utf-8")
    clock.advance()
    assert {row["base_instructions"] for row in listing(relay, "/codex/models")} == {BASE_TEXT}
    file.unlink()
    clock.advance()
    listing(relay, "/codex/models")  # the first look that misses the file only notes it
    clock.advance()
    assert {row["base_instructions"] for row in listing(relay, "/codex/models")} == {built_in}


@pytest.mark.parametrize("path", ["/v1/models", "/codex/models"])
def test_a_reload_that_fails_does_not_fail_the_listing_and_is_journaled(clocked, monkeypatch, path):
    relay, _ = clocked

    def broken():
        raise RuntimeError("the prompts file cannot be looked at")

    monkeypatch.setattr(relay.runtime.prompts, "maybe_reload", broken)
    mark = journal.JOURNAL.last_seq
    assert listing(relay, path)
    (event,) = [e for e in journal.JOURNAL.query(since=mark, limit=500) if e["kind"] == "prompt.runtime_error"]
    assert event["level"] == "error" and event["data"]["stage"] == "reload"
    assert "cannot be looked at" in event["message"]
