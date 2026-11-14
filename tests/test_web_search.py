"""WebSearch: the relay runs the searches of Claude Code's web_search server tool that Retool cannot run.

The provider is faked (no network, no key): the Exa client is exercised against a local HTTP server, and the loop against a fake
upstream and a fake search object. Nothing here reads or prints a real key."""
import http.server
import json
import threading

import pytest

import apierr
import config
import direct
import validate
import web_search
from test_client_contracts import CFG, _body
from web_search import Result, SearchError

KEY = "exa-test-key-0123456789"
PAGE = Result("Printing press - Wikipedia", "https://en.wikipedia.org/wiki/Printing_press", "April 30, 2025",
              "The printing press is a mechanical device for applying pressure to an inked surface.")
OTHER = Result("A blog", "https://blog.example.net/post/1", None, "A post about presses.")
SERVER = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}


class FakeSearch:
    """The provider the relay talks to: records every call, answers from a list or raises."""

    def __init__(self, results=(PAGE,), error=None):
        self.results, self.error, self.calls = list(results), error, []

    def search(self, query, spec):
        self.calls.append((query, dict(spec)))
        if self.error:
            raise self.error
        return list(self.results)


def anthropic(**changes):
    return _body("anthropic", **{"tools": [dict(SERVER)], **changes})


def parse(search=None, **changes):
    return validate.parse_anthropic(anthropic(**changes), dict(CFG, **({"_web_search": search} if search else {})))


# -- the request ---------------------------------------------------------------------------------------------------------

def test_with_a_provider_the_server_tool_becomes_a_function_the_model_can_call():
    parsed = parse(FakeSearch())
    assert parsed.web_search == {"name": "web_search", "max_uses": 8, "allowed_domains": [], "blocked_domains": [],
                                 "country": None}
    (tool,) = parsed.tools
    assert tool["type"] == "function" and tool["function"]["name"] == "web_search"
    assert tool["function"]["parameters"]["required"] == ["query"]
    (retool,) = direct.tools_to_retool(parsed.tools)
    assert retool["name"] == "web_search" and retool["params"]["query"]["type"] == "string"
    assert retool["params"]["query"]["required"] is True


def test_without_a_provider_the_refusal_stays_and_names_the_way_to_turn_it_on():
    with pytest.raises(apierr.ApiError) as error:
        parse()
    assert error.value.status == 422 and error.value.param == "tools[0].type"
    assert "server tool of the provider ('web_search_20250305')" in error.value.message
    assert "web_search_provider" in error.value.message and "WebFetch" in error.value.message


@pytest.mark.parametrize("kind", ["web_fetch_20250910", "code_execution_20250825", "text_editor_20250124"])
def test_another_server_tool_is_still_refused_with_a_provider(kind):
    body = _body("anthropic", tools=[dict(SERVER, type=kind, name="other")])
    with pytest.raises(apierr.ApiError) as error:
        validate.parse_anthropic(body, dict(CFG, _web_search=FakeSearch()))
    assert error.value.status == 422 and "server tool of the provider" in error.value.message
    assert "web_search_provider" not in error.value.message  # the relay cannot run this one


@pytest.mark.parametrize("version", ["web_search_20250305", "web_search_20260209", "web_search_20260318"])
def test_every_version_of_the_tool_is_the_same_tool(version):
    body = _body("anthropic", tools=[dict(SERVER, type=version)])
    assert validate.parse_anthropic(body, dict(CFG, _web_search=FakeSearch())).web_search["name"] == "web_search"


def test_chat_and_responses_keep_refusing_it():
    for dialect in ("chat", "responses"):
        data = _body(dialect, tools=[dict(SERVER)])
        with pytest.raises(apierr.ApiError) as error:
            getattr(validate, "parse_" + dialect)(data, dict(CFG, _web_search=FakeSearch()))
        assert error.value.status == 422


def test_the_tool_options_are_read():
    parsed = parse(FakeSearch(), tools=[dict(SERVER, max_uses=3, allowed_domains=["https://Example.com/", "docs.python.org/3"],
                                             user_location={"type": "approximate", "country": "de"})])
    assert parsed.web_search["max_uses"] == 3 and parsed.web_search["country"] == "DE"
    assert parsed.web_search["allowed_domains"] == ["Example.com", "docs.python.org/3"]
    plain = validate.parse_anthropic(_body("anthropic", tools=[{"type": "web_search_20250305", "name": "web_search"}]),
                                     dict(CFG, _web_search=FakeSearch()))
    assert plain.web_search["max_uses"] == web_search.DEFAULT_MAX_USES


@pytest.mark.parametrize("fields, param", [
    ({"max_uses": 0}, "max_uses"), ({"max_uses": "5"}, "max_uses"), ({"max_uses": True}, "max_uses"),
    ({"allowed_domains": "example.com"}, "allowed_domains"), ({"blocked_domains": [1]}, "blocked_domains"),
    ({"allowed_domains": ["a.com"], "blocked_domains": ["b.com"]}, "tools[0]")])
def test_a_bad_tool_option_is_a_422_before_any_upstream_call(fields, param):
    body = _body("anthropic", tools=[dict(SERVER, **fields)])
    with pytest.raises(apierr.ApiError) as error:
        validate.parse_anthropic(body, dict(CFG, _web_search=FakeSearch()))
    assert error.value.status == 422 and param in error.value.param


# -- what the model reads and the client keeps ---------------------------------------------------------------------------

def test_the_results_travel_in_the_opaque_field_and_come_back_as_what_the_model_read():
    blocks = web_search.result_blocks([PAGE, OTHER])
    assert [block["type"] for block in blocks] == ["web_search_result"] * 2
    assert blocks[0]["url"] == PAGE.url and blocks[0]["title"] == PAGE.title and blocks[0]["page_age"] == "April 30, 2025"
    assert blocks[1]["page_age"] is None
    text = web_search.render_block_content(blocks)
    assert text.startswith("[1] Printing press - Wikipedia\nhttps://en.wikipedia.org/wiki/Printing_press\nPublished: April 30, 2025\n")
    assert PAGE.text in text and "[2] A blog" in text and "Published" not in text.split("[2]")[1]
    # the client echoes the blocks it got; reading them again gives the same text
    assert web_search.render_block_content(json.loads(json.dumps(blocks))) == text


def test_an_error_and_an_empty_result_have_their_own_text():
    assert web_search.render_block_content({"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}) \
        == "web_search error: max_uses_exceeded"
    assert web_search.render_block_content([]) == "No results."
    assert web_search.render_block_content(None) == "No results."


def test_a_block_that_is_not_ours_still_gives_its_title_and_url():
    foreign = [{"type": "web_search_result", "url": "https://x.example/a", "title": "Foreign", "page_age": None,
                "encrypted_content": "not ours"}]
    assert web_search.render_block_content(foreign) == "[1] Foreign\nhttps://x.example/a"


@pytest.mark.parametrize("url, spec, kept", [
    ("https://docs.example.com/a", {"allowed_domains": ["example.com"]}, True),
    ("https://example.com/a", {"allowed_domains": ["example.com"]}, True),
    ("https://notexample.com/a", {"allowed_domains": ["example.com"]}, False),
    ("https://example.com.evil.net/a", {"allowed_domains": ["example.com"]}, False),
    ("https://example.com/blog/x", {"allowed_domains": ["example.com/blog"]}, True),
    ("https://example.com/shop/x", {"allowed_domains": ["example.com/blog"]}, False),
    ("https://example.com/a", {"blocked_domains": ["example.com"]}, False),
    ("https://other.org/a", {"blocked_domains": ["example.com"]}, True),
    ("https://other.org/a", {}, True)])
def test_the_relay_applies_the_domain_lists_whatever_the_provider_did(url, spec, kept):
    assert bool(web_search.within_domains([Result("t", url, None, "")], spec)) is kept


# -- the Exa client ------------------------------------------------------------------------------------------------------

class Exa:
    """A local server that answers like api.exa.ai: records what it was sent."""

    def __init__(self, status=200, body=None):
        self.seen, self.status, self.body = [], status, body
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["content-length"]))
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": json.loads(raw)})
                payload = outer.body if isinstance(outer.body, bytes) else json.dumps(outer.body or {"results": []}).encode()
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def client(self, results=5, timeout=5):
        return web_search.Exa(KEY, self.url + "/", results, timeout)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def exa():
    servers = []

    def make(**kwargs):
        servers.append(Exa(**kwargs))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


EXA_ANSWER = {"requestId": "r1", "results": [
    {"title": "Printing press - Wikipedia", "url": "https://en.wikipedia.org/wiki/Printing_press",
     "publishedDate": "2025-04-30T00:00:00.000Z", "highlights": ["The printing press is a mechanical device.", "  Gutenberg  "]},
    {"title": "No date", "url": "https://x.example/a", "text": "full\n text   here"},
    {"title": "Broken row without a url"}, "not a row"]}


def test_the_client_posts_the_query_with_the_key_in_the_header_and_reads_the_results(exa):
    server = exa(body=EXA_ANSWER)
    found = server.client(results=3).search("printing press", {"allowed_domains": ["example.com", "docs.x.org/api"],
                                                               "blocked_domains": [], "country": "DE"})
    (seen,) = server.seen
    assert seen["path"] == "/search" and seen["headers"]["x-api-key"] == KEY
    body = seen["body"]
    assert body["query"] == "printing press" and body["numResults"] == 3 and body["type"] == "auto"
    assert body["contents"] == {"highlights": {"maxCharacters": web_search.SNIPPET_LIMIT}}
    assert body["includeDomains"] == ["example.com", "*.example.com", "docs.x.org/api"] and "excludeDomains" not in body
    assert body["userLocation"] == "DE"
    assert [(r.title, r.url, r.page_age) for r in found] == [
        ("Printing press - Wikipedia", "https://en.wikipedia.org/wiki/Printing_press", "April 30, 2025"),
        ("No date", "https://x.example/a", None)]
    assert found[0].text == "The printing press is a mechanical device. Gutenberg" and found[1].text == "full text here"


def test_blocked_domains_go_to_excludeDomains(exa):
    server = exa(body=EXA_ANSWER)
    server.client().search("q", {"allowed_domains": [], "blocked_domains": ["bad.org"], "country": None})
    assert server.seen[0]["body"]["excludeDomains"] == ["bad.org", "*.bad.org"]
    assert "includeDomains" not in server.seen[0]["body"] and "userLocation" not in server.seen[0]["body"]


@pytest.mark.parametrize("status, code", [(429, "too_many_requests"), (401, "unavailable"), (402, "unavailable"),
                                          (400, "invalid_tool_input"), (500, "unavailable"), (503, "unavailable")])
def test_a_refusal_of_the_provider_is_one_of_anthropics_error_codes_and_never_shows_the_key(exa, status, code):
    server = exa(status=status, body={"requestId": "r", "error": "secret detail for " + KEY, "tag": "SOME_TAG"})
    with pytest.raises(SearchError) as error:
        server.client().search("q", {})
    assert error.value.code == code and KEY not in error.value.detail and "secret detail" not in error.value.detail
    assert "HTTP %d SOME_TAG" % status == error.value.detail


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"results": "no"}'])
def test_an_answer_that_is_not_a_result_list_is_unavailable(exa, body):
    with pytest.raises(SearchError) as error:
        exa(body=body).client().search("q", {})
    assert error.value.code == "unavailable"


def test_a_server_that_is_not_there_is_unavailable():
    with pytest.raises(SearchError) as error:
        web_search.Exa(KEY, "http://127.0.0.1:9", 5, 2).search("q", {})
    assert error.value.code == "unavailable" and KEY not in error.value.detail


def test_the_key_is_not_in_the_object_s_text():
    assert KEY not in repr(web_search.Exa(KEY, "https://api.exa.ai", 5, 20)) and KEY not in str(vars(SearchError("unavailable")))


# -- the configuration ---------------------------------------------------------------------------------------------------

def test_a_provider_needs_its_key_in_the_environment_variable_the_config_names(monkeypatch):
    cfg = {"web_search_provider": "exa", "web_search_key_env": "RELAY_TEST_EXA_KEY"}
    monkeypatch.delenv("RELAY_TEST_EXA_KEY", raising=False)
    assert web_search.provider(cfg) is None and web_search.provider({}) is None
    monkeypatch.setenv("RELAY_TEST_EXA_KEY", KEY)
    client = web_search.provider(dict(cfg, web_search_results=7, web_search_timeout=9))
    assert isinstance(client, web_search.Exa) and (client.key, client.results, client.timeout) == (KEY, 7, 9.0)
    assert client.endpoint == "https://api.exa.ai"
    assert web_search.provider({"web_search_provider": ""}) is None
    assert web_search.provider({"web_search_provider": "other", "web_search_key_env": "RELAY_TEST_EXA_KEY"}) is None


def load(tmp_path, monkeypatch, **settings):
    path = tmp_path / "config.json"
    saved = {"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua", "cookie": "accessToken=fake-token",
             "xsrf": "fake-xsrf", "workspace_root": str(tmp_path), "provider_resource_name": "__managed__", **settings}
    path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID", "RELAY_TEST_EXA_KEY"):
        monkeypatch.delenv(name, raising=False)
    return config.load(str(path))


def test_the_defaults_leave_the_refusal_in_place(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch)
    assert (cfg["web_search_provider"], cfg["web_search_key_env"]) == ("", "EXA_API_KEY")
    assert (cfg["web_search_endpoint"], cfg["web_search_results"], cfg["web_search_timeout"]) == ("https://api.exa.ai", 5, 20)
    assert cfg["config_warnings"] == [] and web_search.provider(cfg) is None


def test_a_configured_provider_without_its_key_warns_and_stays_refused(tmp_path, monkeypatch, capsys):
    cfg = load(tmp_path, monkeypatch, web_search_provider="exa", web_search_key_env="RELAY_TEST_EXA_KEY")
    assert any("RELAY_TEST_EXA_KEY" in text and "stays refused" in text for text in cfg["config_warnings"])
    assert "RELAY_TEST_EXA_KEY" in capsys.readouterr().err and web_search.provider(cfg) is None


def test_with_the_key_it_is_on_and_the_key_is_masked_like_the_other_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_TEST_EXA_KEY", KEY)
    path = tmp_path / "config.json"
    cfg = load(tmp_path, monkeypatch, web_search_provider="exa", web_search_key_env="RELAY_TEST_EXA_KEY")
    monkeypatch.setenv("RELAY_TEST_EXA_KEY", KEY)  # load() removes the names it clears; the key is set again for the check
    cfg = config.load(str(path))
    assert cfg["config_warnings"] == [] and KEY in cfg["cookie_values"]
    assert KEY not in path.read_text(encoding="utf-8")
    assert isinstance(web_search.provider(cfg), web_search.Exa)


@pytest.mark.parametrize("settings, text", [
    ({"web_search_provider": "bing"}, "web_search_provider must be one of"),
    ({"web_search_provider": None}, "web_search_provider must be one of"),
    ({"web_search_key_env": "not a name"}, "environment variable"), ({"web_search_key_env": 5}, "environment variable"),
    ({"web_search_key_env": "exa-key-1234567890"}, "environment variable"),
    ({"web_search_endpoint": "ftp://x"}, r"http\(s\)"), ({"web_search_endpoint": ""}, r"http\(s\)"),
    ({"web_search_results": 0}, "web_search_results"), ({"web_search_results": 21}, "web_search_results"),
    ({"web_search_results": 2.5}, "web_search_results"), ({"web_search_results": True}, "web_search_results"),
    ({"web_search_timeout": 0}, "web_search_timeout"), ({"web_search_timeout": 121}, "web_search_timeout"),
    ({"web_search_timeout": "20"}, "web_search_timeout"), ({"web_search_timeout": True}, "web_search_timeout")])
def test_a_bad_setting_stops_the_start(tmp_path, monkeypatch, settings, text):
    with pytest.raises(apierr.ApiError, match=text):
        load(tmp_path, monkeypatch, **settings)


def test_the_example_config_carries_the_settings_but_no_key():
    from pathlib import Path
    raw = (Path(config.HERE) / "config.example.json").read_text(encoding="utf-8")
    example = json.loads(raw)
    assert example["web_search_provider"] == "" and example["web_search_key_env"] == "EXA_API_KEY"
    assert "EXA_API_KEY" in example["web_search_note"] and "x-api-key" not in raw
