"""C9: a remote image is downloaded once per request, not once per read of the message."""
import base64
import copy
import io

import pytest

import validate

URL = "http://93.184.216.34/a.png"
OTHER = "http://93.184.216.34/b.png"
CFG = {"strict_models": True, "model": "claude-opus-5-5"}
CHAT = {"model": "claude-opus-5-5", "messages": [
    {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": URL}}]}]}
RESPONSES = {"model": "claude-opus-5-5", "input": [
    {"role": "user", "content": [{"type": "input_text", "text": "look"}, {"type": "input_image", "image_url": URL}]}]}
ANTHROPIC = {"model": "claude-opus-5-5", "max_tokens": 10, "messages": [
    {"role": "user", "content": [{"type": "text", "text": "look"},
                                 {"type": "image_url", "image_url": {"url": URL}}]}]}
# adapters-3: the Messages API names a picture by URL as well. Only source.data was read, so this block failed with 422
# "missing image attachments data" and nothing was downloaded, while the same link as an image_url was.
MESSAGES_URL = {"model": "claude-opus-5-5", "max_tokens": 10, "messages": [
    {"role": "user", "content": [{"type": "text", "text": "look"},
                                 {"type": "image", "source": {"type": "url", "url": URL}}]}]}


class Download(io.BytesIO):
    headers = {"content-type": "image/png"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def downloads(monkeypatch):
    """Every URL fetched; each fetch of one link answers with other bytes, as a picture that keeps changing does."""
    urls = []

    def fake_open(request, timeout=None):
        urls.append(request.full_url)
        return Download(("picture %d of %s" % (len(urls), request.full_url)).encode())

    monkeypatch.setattr(validate._IMAGE_OPENER, "open", fake_open)
    monkeypatch.setattr(validate, "_assert_public_http_url", lambda url, where="image": None)
    return urls


def pictures(chunks):
    return [base64.b64decode(block["imageData"]).decode() for chunk in chunks if isinstance(chunk, list)
            for block in chunk if block.get("type") == "image"]


@pytest.mark.parametrize("parse,body", [(validate.parse_chat, CHAT), (validate.parse_responses, RESPONSES),
                                        (validate.parse_anthropic, ANTHROPIC), (validate.parse_anthropic, MESSAGES_URL)])
def test_one_parse_downloads_a_picture_once(downloads, parse, body):
    parsed = parse(copy.deepcopy(body), CFG)
    assert downloads == [URL]
    # the text bridge and the native history carry the very bytes that were downloaded
    native = [base64.b64decode(block["imageData"]).decode() for message in parsed.native_messages
              if isinstance(message["content"], list) for block in message["content"] if block.get("type") == "image"]
    assert pictures(parsed.message_chunks) == native == ["picture 1 of " + URL]


def test_the_same_link_in_several_messages_is_downloaded_once(downloads):
    body = copy.deepcopy(CHAT)
    body["messages"] += [{"role": "assistant", "content": "seen"}, copy.deepcopy(CHAT["messages"][0])]
    parsed = validate.parse_chat(body, CFG)
    assert downloads == [URL]
    assert pictures(parsed.message_chunks) == ["picture 1 of " + URL] * 2


def test_two_links_are_two_downloads(downloads):
    body = copy.deepcopy(CHAT)
    body["messages"][0]["content"].append({"type": "image_url", "image_url": {"url": OTHER}})
    validate.parse_chat(body, CFG)
    assert downloads == [URL, OTHER]


def test_a_slow_link_that_fails_the_first_time_still_fails_the_request_once(monkeypatch):
    calls = []

    def fake_open(request, timeout=None):
        calls.append(request.full_url)
        raise OSError("timed out")

    monkeypatch.setattr(validate._IMAGE_OPENER, "open", fake_open)
    monkeypatch.setattr(validate, "_assert_public_http_url", lambda url, where="image": None)
    with pytest.raises(validate.ApiError) as error:
        validate.parse_chat(copy.deepcopy(CHAT), CFG)
    assert error.value.status == 422 and calls == [URL]


@pytest.mark.parametrize("body", [ANTHROPIC, MESSAGES_URL], ids=["image_url", "url source"])
def test_count_tokens_downloads_a_picture_once(relay, downloads, body):
    status, answer = relay.json("/v1/messages/count_tokens", copy.deepcopy(body))
    assert status == 200 and answer["input_tokens"] > 0
    assert downloads == [URL]


def test_a_messages_request_with_a_picture_named_by_url_reaches_the_model(relay, downloads):
    # the request was answered 422 before the runner was called
    status, _ = relay.json("/v1/messages", copy.deepcopy(MESSAGES_URL))
    assert status == 200 and downloads == [URL]
    assert pictures(relay.fake.seen[-1].message_chunks) == ["picture 1 of " + URL]


def test_a_picture_named_by_url_in_a_tool_result_is_downloaded_once(downloads):
    # A tool may answer with a picture by URL: the tool result took the same 422 as a user turn.
    body = {"model": "claude-opus-5-5", "max_tokens": 10, "messages": [
        {"role": "user", "content": "draw"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "render", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                      "content": [{"type": "image", "source": {"type": "url", "url": URL}}]}]}]}
    parsed = validate.parse_anthropic(body, CFG)
    assert downloads == [URL]
    assert pictures(parsed.message_chunks) == ["picture 1 of " + URL]
    result = parsed.native_messages[-1]["content"]
    assert result["toolCallId"] == "toolu_1"
    assert [base64.b64decode(block["imageData"]).decode() for block in result["toolResult"]] == ["picture 1 of " + URL]


def test_one_link_as_an_image_url_and_as_a_url_source_is_one_download(downloads):
    body = copy.deepcopy(MESSAGES_URL)
    body["messages"][0]["content"].append({"type": "image_url", "image_url": {"url": URL}})
    parsed = validate.parse_anthropic(body, CFG)
    assert downloads == [URL]
    assert pictures(parsed.message_chunks) == ["picture 1 of " + URL] * 2


def test_a_messages_image_from_the_files_api_is_refused_with_its_source_type_named(downloads):
    # A file_id names a picture kept at Anthropic, which the relay has no bytes of. The 422 said the data was missing,
    # which tells the client nothing it can change.
    body = copy.deepcopy(MESSAGES_URL)
    body["messages"][0]["content"][1]["source"] = {"type": "file", "file_id": "file_011CNha8iCJcU1wXNR6q4V8w"}
    with pytest.raises(validate.ApiError) as error:
        validate.parse_anthropic(body, CFG)
    assert (error.value.status, error.value.param) == (422, "messages[0].content[1]")
    assert "image source type 'file' is not supported" in error.value.message
    assert downloads == []
