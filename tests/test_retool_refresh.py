"""The relay keeps its Retool catalog current by itself (retool_refresh): a chat model Retool's own registry has and the
relay's catalog lacks is asked one question through an account and published when it answers, without a restart."""
import io
import json
import types

import pytest

import config
import journal
import models
import retool_catalog
import retool_refresh

ENTRY = "main-Abc123.js"
CHUNK = "index-Reg456.js"


def registry(**extra):
    document = {
        "anthropic": {"defaultModel": "claude-sonnet-4-6", "models": {
            "claude-fable-5-2": {"id": "claude-fable-5-2", "displayName": "Claude Fable 5.2 'Preview'",
                                 "provider": "anthropic", "capabilities": {
                                     "textGeneration": True, "multimodal": True, "functionCalling": True,
                                     "maxTokens": 1000000, "maxCompletionTokens": 128000,
                                     "reasoningEffortLevels": ["low", "medium", "high", "max"]}},
            "claude-opus-5-5": {"id": "claude-opus-5-5", "displayName": "Claude Opus 5.5", "capabilities": {
                "textGeneration": True, "maxTokens": 1}},  # audited: never replaced
            "claude-3-haiku": {"id": "claude-3-haiku", "lifecycle": {"deprecatedOn": "2026-07-01"},
                               "capabilities": {"textGeneration": True}}}},
        "openAI": {"models": {
            "gpt-7-mini": {"id": "gpt-7-mini", "displayName": "GPT 7 Mini", "capabilities": {
                "textGeneration": True, "maxTokens": 400000, "maxCompletionTokens": 128000,
                "reasoningEffortLevels": ["none", "low", "medium", "high", "xhigh", "max"]}},
            "gpt-image-9": {"id": "gpt-image-9", "capabilities": {"textGeneration": False, "imageGeneration": True}},
            "ft:gpt-4o:retool:x": {"id": "ft:gpt-4o:retool:x", "capabilities": {"textGeneration": True}}}},
        "google": {"models": {"gemini-9-pro": {"id": "gemini-9-pro", "capabilities": {"textGeneration": True}}}},
        "cohere": {"models": {"command-z": {"id": "command-z", "capabilities": {"textGeneration": True}}}},
    }
    document.update(extra)
    return document


def chunk_text(document):
    """How Retool's frontend carries it: a JSON document inside a single-quoted JavaScript string."""
    literal = json.dumps(document).replace("\\", "\\\\").replace("'", "\\'")
    return "const a=1;const RT=JSON.parse('%s');export{RT};" % literal


@pytest.fixture(autouse=True)
def isolated_catalog():
    catalog, by_id, aliases = list(models.CATALOG), dict(models._BY_ID), dict(models.ALIASES)
    yield
    models.CATALOG[:] = catalog
    models._BY_ID.clear()
    models._BY_ID.update(by_id)
    models.ALIASES.clear()
    models.ALIASES.update(aliases)


def test_the_registry_is_read_from_its_frontend_chunk_with_javascript_escapes():
    parsed = retool_catalog.parse_registry(chunk_text(registry()))
    assert parsed["anthropic"]["models"]["claude-fable-5-2"]["displayName"] == "Claude Fable 5.2 'Preview'"
    assert retool_catalog.parse_registry("const nothing = 1;") is None
    with pytest.raises(ValueError):
        retool_catalog.parse_registry("JSON.parse('{\"anthropic\":{\"models\":")


def test_only_routable_chat_models_are_taken():
    found = retool_catalog.chat_models(retool_catalog.parse_registry(chunk_text(registry())))
    assert sorted(found) == ["claude-fable-5-2", "claude-opus-5-5", "gemini-9-pro", "gpt-7-mini"]
    fable = found["claude-fable-5-2"]
    assert (fable["provider"], fable["context"], fable["max_output"], fable["vision"]) == ("anthropic", 1000000, 128000, True)
    assert found["gpt-7-mini"]["provider"] == "openai" and found["gemini-9-pro"]["provider"] == "google"
    assert sorted(retool_catalog.new_models(found)) == ["claude-fable-5-2", "gemini-9-pro", "gpt-7-mini"]


class Account:
    def __init__(self, **cfg):
        self.cfg = dict({"upstream": "retool", "base": "https://tenant.retool.com", "enabled": True,
                         "provider_resource_name": "__managed__", "openai_provider_resource_name": "openai-res"}, **cfg)
        self.unavailable = None
        self.credits = types.SimpleNamespace(turns=[], record_turn=lambda turn, cost: self.credits.turns.append(cost))


class Frontend:
    def __init__(self, document):
        self.pages = {"https://tenant.retool.com/": '<script src="https://retool-edge.com/assets_vjs/%s"></script>' % ENTRY,
                      retool_refresh.EDGE + ENTRY: 'import("./assets_vjs/other-1.js");import("./assets_vjs/%s")' % CHUNK,
                      retool_refresh.EDGE + "other-1.js": "no registry here",
                      retool_refresh.EDGE + CHUNK: chunk_text(document)}
        self.read = []

    def __call__(self, url, limit):
        self.read.append(url)
        return self.pages[url]


def refresher(tmp_path, frontend, answers, **cfg):
    settings = dict({"_retool_catalog_file": str(tmp_path / retool_catalog.AUTO_NAME)}, **cfg)
    asked = []

    def prober(backend, entry, effort=None):
        if effort is not None:  # a level of a model the relay routes (CatalogRefresher._relevel)
            asked.append((entry["id"], effort))
            return dict({"ok": True, "accepted": True, "http": 200, "error": None, "credits": 0.004, "thinking": 1,
                         "seconds": 0.5}, **answers.get((entry["id"], effort), {}))
        asked.append(entry["id"])
        return dict({"http": 200, "error": None, "credits": 0.0125, "thinking": 0, "seconds": 1.0},
                    **answers.get(entry["id"], {"ok": True}))
    value = retool_refresh.CatalogRefresher(settings, backends=[Account()], fetcher=frontend, prober=prober)
    return value, asked


def test_a_new_model_that_answers_is_published_and_routed_without_a_restart(tmp_path):
    frontend = Frontend(registry())
    value, asked = refresher(tmp_path, frontend, {"gpt-7-mini": {"ok": False, "http": 404, "error": "model not found"}})
    before = journal.JOURNAL.last_seq
    summary = value.run_once()
    assert asked == ["claude-fable-5-2", "gpt-7-mini"] and summary["added"] == ["claude-fable-5-2"]
    assert summary["failed"] == [["gpt-7-mini", "model not found"]]
    assert summary["skipped"] == {"gemini-9-pro": "no account has a google resource"}
    entry = models._BY_ID["claude-fable-5-2"]
    assert entry in models.CATALOG and entry["discovered"] and entry["route_status"] == "verified"
    assert (entry["context"], entry["max_output"], entry["reasoning_effort_levels"]) == (1000000, 128000,
                                                                                          ["low", "medium", "high", "max"])
    assert models.resolve("claude-fable-5.2")[0]["id"] == "claude-fable-5-2"  # the dotted name too
    assert models._BY_ID["claude-opus-5-5"]["context"] == 1000000  # an audited entry is never replaced
    assert value.backends[0].credits.turns == [0.0125, 0.0125]  # the probes' cost is on the account
    kinds = [event["kind"] for event in journal.JOURNAL.query(since=before, limit=100)]
    assert "catalog.retool_model_added" in kinds and "catalog.retool_model_failed" in kinds
    assert "catalog.retool_refresh" in kinds
    state = retool_catalog.read(tmp_path / retool_catalog.AUTO_NAME)
    assert state["entry_script"] == ENTRY and state["registry_chunk"] == CHUNK
    assert state["measurements"]["gpt-7-mini"]["failures"] == 1 and state["measurements"]["gpt-7-mini"]["retry_after"]
    # The next look: the same entry script is not read again, the failed model waits for its day.
    frontend.read.clear()
    asked.clear()
    again = value.run_once()
    assert frontend.read == ["https://tenant.retool.com/"] and asked == []
    assert again["skipped"]["gpt-7-mini"].startswith("asked again after")


def test_the_overlay_is_put_into_the_catalog_at_load(tmp_path):
    value, _ = refresher(tmp_path, Frontend(registry()), {"gpt-7-mini": {"ok": False, "http": 404}})
    value.run_once()
    models.CATALOG[:] = [entry for entry in models.CATALOG if not entry.get("discovered")]
    models._BY_ID.pop("claude-fable-5-2", None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base": "https://tenant.retool.com", "page_uuid": "p", "cookie": "accessToken=x",
                                "xsrf": "x", "ua": "fixture-agent", "relay_api_key": "k" * 40,
                                "workspace_root": str(tmp_path)}),
                    encoding="utf-8")
    cfg = config.load(str(path))
    assert cfg["_retool_catalog_file"].endswith(retool_catalog.AUTO_NAME)
    assert models._BY_ID["claude-fable-5-2"]["discovered"] is True


def test_a_page_without_an_entry_script_is_a_warning_not_a_crash(tmp_path):
    frontend = Frontend(registry())
    frontend.pages["https://tenant.retool.com/"] = "<html>maintenance</html>"
    value, asked = refresher(tmp_path, frontend, {})
    before = journal.JOURNAL.last_seq
    summary = value.run_once()
    assert summary["error"] == "no entry script on the login page" and asked == []
    said = [event for event in journal.JOURNAL.query(since=before, limit=50) if event["kind"] == "catalog.retool_refresh_failed"]
    assert len(said) == 1 and said[0]["level"] == "warn"


def test_probes_per_look_are_bounded(tmp_path):
    many = {"gpt-7-%d" % n: {"id": "gpt-7-%d" % n, "capabilities": {"textGeneration": True}} for n in range(8)}
    value, asked = refresher(tmp_path, Frontend(registry(openAI={"models": many})), {}, retool_catalog_probes=3)
    summary = value.run_once()
    assert len(asked) == 3 and sum(reason.startswith("waits for the next look") for reason in summary["skipped"].values()) == 6


def with_levels():
    """The registry as Retool might change it: Opus 5.5 gains xhigh, GPT-6 Sol lists the xhigh its route refuses, GLM
    lists levels its route takes and ignores."""
    document = registry()
    document["anthropic"]["models"]["claude-opus-5-5"]["capabilities"]["reasoningEffortLevels"] = [
        "low", "medium", "high", "xhigh", "max"]
    document["openAI"]["models"]["gpt-6-sol"] = {"id": "gpt-6-sol", "capabilities": {
        "textGeneration": True, "reasoningEffortLevels": ["none", "low", "medium", "high", "xhigh", "max"]}}
    document["retoolFast"] = {"models": {"zai-org/GLM-5.3": {"id": "zai-org/GLM-5.3", "capabilities": {
        "textGeneration": True, "reasoningEffortLevels": ["none", "high", "max"]}}}}
    return document


def test_a_level_retool_adds_is_asked_once_and_offered_without_a_restart(tmp_path):
    gpt_levels = list(models._BY_ID["gpt-6-sol"]["reasoning_effort_levels"])
    assert models._BY_ID["zai-org/GLM-5.3"]["reasoning_effort_control"] == "accepted-ignored"
    value, asked = refresher(tmp_path, Frontend(with_levels()), {"gpt-7-mini": {"ok": False, "http": 404}})
    before = journal.JOURNAL.last_seq
    summary = value.run_once()
    # Only the level the relay lacks and has no reason to doubt is asked: GPT's xhigh is refused (422), GLM ignores effort.
    assert [item for item in asked if isinstance(item, tuple)] == [("claude-opus-5-5", "xhigh")]
    assert summary["leveled"] == [["claude-opus-5-5", "xhigh"]] and summary["refused_levels"] == []
    opus = models._BY_ID["claude-opus-5-5"]
    assert opus["reasoning_effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert not models.replaces_xhigh(opus)  # xhigh is now sent as itself, not as the claude_xhigh_effort setting
    assert models._BY_ID["gpt-6-sol"]["reasoning_effort_levels"] == gpt_levels
    assert models._BY_ID["zai-org/GLM-5.3"]["reasoning_effort_levels"] == []
    added = [event for event in journal.JOURNAL.query(since=before, limit=100) if event["kind"] == "catalog.retool_level_added"]
    assert len(added) == 1 and added[0]["data"]["effort"] == "xhigh"
    path = tmp_path / retool_catalog.AUTO_NAME
    assert retool_catalog.read(path)["levels"]["claude-opus-5-5"]["verified"] == ["xhigh"]
    # The next look asks nothing: the level is offered.
    asked.clear()
    value.run_once()
    assert [item for item in asked if isinstance(item, tuple)] == []
    # A start gives it back from the overlay.
    opus["reasoning_effort_levels"] = ["low", "medium", "high", "max"]
    assert "claude-opus-5-5" in retool_catalog.apply_overlay(path)
    assert models._BY_ID["claude-opus-5-5"]["reasoning_effort_levels"] == ["low", "medium", "high", "xhigh", "max"]


def test_a_level_the_route_refuses_is_asked_again_after_its_day(tmp_path):
    value, asked = refresher(tmp_path, Frontend(with_levels()), {
        ("claude-opus-5-5", "xhigh"): {"ok": False, "accepted": False, "http": 422, "error": "invalid reasoning effort"}})
    summary = value.run_once()
    assert summary["leveled"] == [] and summary["refused_levels"] == [["claude-opus-5-5", "xhigh", "invalid reasoning effort"]]
    assert "xhigh" not in models._BY_ID["claude-opus-5-5"]["reasoning_effort_levels"]
    assert models.replaces_xhigh(models._BY_ID["claude-opus-5-5"])
    mark = retool_catalog.read(tmp_path / retool_catalog.AUTO_NAME)["levels"]["claude-opus-5-5"]["refused"]["xhigh"]
    assert (mark["failures"], mark["http"]) == (1, 422) and mark["retry_after"] > summary["at"]
    asked.clear()
    again = value.run_once()
    assert asked == [] and again["skipped"]["claude-opus-5-5 at xhigh"].startswith("asked again after")


class Stream:
    def __init__(self, lines, status=200):
        self.lines, self.status = [json.dumps(line).encode() + b"\n" for line in lines], status

    def __iter__(self):
        return iter(self.lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_probe_reads_the_relays_own_route(monkeypatch):
    seen = {}

    def answer(request, timeout):
        body = json.loads(request.data)
        seen.update(url=request.full_url, model=body)
        text = json.dumps(body)  # the word the head asks for is READY- and eight characters
        marker = text[text.index("READY-"):text.index("READY-") + 14]
        return Stream([{"event": "content_block_delta", "type": "thinking", "partialThinking": "hm"},
                       {"event": "content_block_delta", "type": "text", "partialText": marker},
                       {"event": "metadata", "usage": {"creditsUsed": 0.02}},
                       {"event": "end_of_stream"}])
    monkeypatch.setattr(retool_refresh.urllib.request, "urlopen", answer)
    monkeypatch.setattr(retool_refresh.auth, "upstream_headers", lambda cfg: {"cookie": "c"})
    account = Account()
    entry = {"id": "claude-fable-5-2", "provider": "anthropic", "reasoning_effort_levels": ["low", "high"]}
    result = retool_refresh.probe(account, entry)
    assert result["ok"] is True and result["credits"] == 0.02 and result["thinking"] == 1
    assert seen["url"] == "https://tenant.retool.com/api/gpt/copilotStreamGPT?origin=frontend"
    assert "claude-fable-5-2" in json.dumps(seen["model"])

    def refused(request, timeout):
        raise retool_refresh.urllib.error.HTTPError(request.full_url, 404, "nf", {}, io.BytesIO(b'{"message":"no"}'))
    monkeypatch.setattr(retool_refresh.urllib.request, "urlopen", refused)
    result = retool_refresh.probe(account, entry)
    assert result["ok"] is False and result["http"] == 404 and "no" in result["error"]


def test_a_level_probe_asks_at_that_level_and_stops_at_the_first_event(monkeypatch):
    seen = {"read": 0}

    class Counting(Stream):
        def __iter__(self):
            for line in self.lines:
                seen["read"] += 1
                yield line

    def answer(request, timeout):
        seen["body"] = json.loads(request.data)
        return Counting([{"event": "content_block_delta", "type": "thinking", "partialThinking": "hm"},
                         {"event": "content_block_delta", "type": "text", "partialText": "never read"},
                         {"event": "end_of_stream"}])
    monkeypatch.setattr(retool_refresh.urllib.request, "urlopen", answer)
    monkeypatch.setattr(retool_refresh.auth, "upstream_headers", lambda cfg: {"cookie": "c"})
    account = Account()
    entry = {"id": "claude-opus-5-5", "provider": "anthropic", "reasoning_effort_levels": ["low", "high"]}
    result = retool_refresh.probe(account, entry, effort="xhigh")
    assert seen["body"]["reasoningEffort"] == "xhigh" and seen["read"] == 1
    assert (result["ok"], result["accepted"], result["error"], result["thinking"]) == (True, True, None, 1)

    def refused(request, timeout):
        raise retool_refresh.urllib.error.HTTPError(request.full_url, 422, "u", {}, io.BytesIO(b'{"message":"effort"}'))
    monkeypatch.setattr(retool_refresh.urllib.request, "urlopen", refused)
    result = retool_refresh.probe(account, entry, effort="xhigh")
    assert (result["ok"], result["accepted"], result["http"]) == (False, False, 422) and "effort" in result["error"]


def test_a_published_model_reaches_the_client_lists_sync(tmp_path):
    # scripts/sync_opencode_catalog.py read config.json alone: a model the relay had already published by itself never
    # reached OpenCode's list. It reads the overlays beside the relay's config.json, as config.load does.
    from scripts.probe_client_settings import routable_entries
    value, _ = refresher(tmp_path, Frontend(registry()), {"gpt-7-mini": {"ok": False, "http": 404}})
    value.run_once()
    models.CATALOG[:] = [entry for entry in models.CATALOG if not entry.get("discovered")]
    models._BY_ID.pop("claude-fable-5-2", None)
    relay = tmp_path / "config.json"
    relay.write_text(json.dumps({"base": "https://tenant.retool.com", "page_uuid": "p", "cookie": "accessToken=x",
                                 "xsrf": "x", "ua": "fixture-agent", "provider_resource_name": "__managed__",
                                 "openai_provider_resource_name": "openai-res"}), encoding="utf-8")
    assert "claude-fable-5-2" in [entry["id"] for entry in routable_entries(str(relay))]
