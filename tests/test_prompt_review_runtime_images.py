"""R2-14: the second parse of a body a profile rewrote takes the remote images the first parse downloaded."""
import base64
import copy
import io
import json

import pytest

import validate
from prompt_support import head_of, prompt_relay  # noqa: F401 - prompt_relay is a fixture

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63fcffff3f0005"
                    "fe02fea7d5e8180000000049454e44ae426082")
PNG_B64 = base64.b64encode(PNG).decode("ascii")
URL = "http://93.184.216.34/a.png"
DOCUMENT = {"version": 1, "profiles": {
    "plain": {"head": "{{client_system}}"},
    "edits": {"head": "RULES\n\n{{client_system}}", "history": {"user_blocks": [{"op": "drop_tag", "tag": "x"}]}}},
    "rules": [{"name": "by header", "if": {"header_profile": "*"}, "use": "$header"}]}
BODY = {"model": "claude-opus-5-5", "messages": [
    {"role": "system", "content": "SYSTEM"},
    {"role": "user", "content": [{"type": "text", "text": "look\n<x>\ndrop\n</x>\nend"},
                                 {"type": "image_url", "image_url": {"url": URL}}]}]}


class Download(io.BytesIO):
    headers = {"content-type": "image/png"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def downloads(monkeypatch):
    """The URLs the relay downloaded; `alive` says how many downloads still answer (None: all of them)."""
    state = {"urls": [], "alive": None}

    def fake_open(request, timeout=None):
        state["urls"].append(request.full_url)
        if state["alive"] is not None and len(state["urls"]) > state["alive"]:
            raise OSError("the link stopped answering")
        return Download(PNG)

    monkeypatch.setattr(validate._IMAGE_OPENER, "open", fake_open)
    monkeypatch.setattr(validate, "_assert_public_http_url", lambda url, where="image": None)
    return state


def relay_with_profiles(prompt_relay, tmp_path):
    (tmp_path / "prompts.json").write_text(json.dumps(DOCUMENT), encoding="utf-8")
    return prompt_relay()


def sent_images(relay):
    return json.dumps(relay.posts[-1]).count(PNG_B64)


def test_the_second_parse_does_not_download_the_images_again(prompt_relay, tmp_path, downloads):
    relay = relay_with_profiles(prompt_relay, tmp_path)
    counts = {}
    for label, headers in (("legacy", {}), ("without body edits", {"x-proxi-profile": "plain"}),
                           ("with body edits", {"x-proxi-profile": "edits"})):
        del downloads["urls"][:]
        status, _, _ = relay.post("chat", copy.deepcopy(BODY), headers)
        assert status == 200, label
        counts[label] = len(downloads["urls"])
        assert sent_images(relay) >= 1, label  # the picture reaches Retool in every case
    # the legacy parse asks three times (it renders the text, then builds the native messages); a rewrite adds none
    assert counts["legacy"] == counts["without body edits"] == counts["with body edits"] > 0
    assert head_of(relay.posts[-1]).startswith("RULES")  # the last request did go through pass 2
    assert "drop" not in json.dumps(relay.posts[-1]["messages"])


def test_a_link_that_stops_answering_after_the_first_parse_does_not_cost_the_request_its_profile(
        prompt_relay, tmp_path, downloads):
    relay = relay_with_profiles(prompt_relay, tmp_path)
    assert relay.post("chat", copy.deepcopy(BODY), {})[0] == 200
    legacy_downloads = len(downloads["urls"])
    del downloads["urls"][:]
    downloads["alive"] = legacy_downloads  # the first parse's own downloads answer, a fourth would not
    status, _, _ = relay.post("chat", copy.deepcopy(BODY), {"x-proxi-profile": "edits"})
    assert status == 200 and len(downloads["urls"]) == legacy_downloads
    assert head_of(relay.posts[-1]).startswith("RULES") and PNG_B64 in json.dumps(relay.posts[-1])


def test_without_a_scope_every_parse_downloads_as_it_always_did(downloads):
    data = copy.deepcopy(BODY)
    validate.parse_chat(data, {"strict_models": True, "model": "claude-opus-5-5"})
    first = len(downloads["urls"])
    validate.parse_chat(data, {"strict_models": True, "model": "claude-opus-5-5"})
    assert first > 0 and len(downloads["urls"]) == 2 * first


def test_a_scope_records_first_and_serves_only_after_reuse_is_switched_on(downloads):
    cfg = {"strict_models": True, "model": "claude-opus-5-5"}
    fetches = validate.ImageFetches()
    with fetches:
        validate.parse_chat(copy.deepcopy(BODY), cfg)
        first = len(downloads["urls"])
        validate.parse_chat(copy.deepcopy(BODY), cfg)  # recording only: the same parse downloads again
        assert len(downloads["urls"]) == 2 * first
        assert set(fetches.found) == {URL}
        fetches.reuse = True
        parsed = validate.parse_chat(copy.deepcopy(BODY), cfg)
        assert len(downloads["urls"]) == 2 * first
    assert parsed.native_messages[-1]["content"][-1] == {"type": "image", "imageData": PNG_B64, "mimeType": "image/png"}
    validate.parse_chat(copy.deepcopy(BODY), cfg)  # the scope is over: downloads again
    assert len(downloads["urls"]) == 3 * first
