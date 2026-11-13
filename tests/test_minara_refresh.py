"""The relay keeps its Minara catalog current by itself (minara_refresh): a model Minara adds is measured and published
without a restart, a new price is taken at once, a model gone from two complete lists is unpublished, and the overlay
file it writes is put over the shipped catalog only while it is the newer of the two."""
import json
from types import SimpleNamespace

import pytest

import config
import journal
import minara
import minara_catalog
import minara_probe
import minara_refresh
import models

NEW = "anthropic/claude-test-new"
PRICE = {"quota_type": 0, "prompt": 3, "completion": 15, "cache_read": 0.3, "cache_write": 3.75, "request": None}


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    measured = dict(minara_catalog.MEASURED)
    prior = dict(models._MINARA_MODELS)
    switched = {key: set(value) for key, value in minara.SWITCHED.items()}
    listeners = list(minara._switch_listeners)
    for key in ('RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_PAGE_UUID', 'RELAY_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    yield
    minara_catalog.MEASURED.clear()
    minara_catalog.MEASURED.update(measured)
    models._MINARA_MODELS.clear()
    models._MINARA_MODELS.update(prior)
    minara.SWITCHED.clear()
    minara.SWITCHED.update(switched)
    minara._switch_listeners[:] = listeners


def relay_cfg(tmp_path, **extra):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(dict({
        "accounts": [{"account_id": "refresh-1", "label": "Minara 1", "upstream": "minara",
                      "bearer_token": "synthetic-bearer-1"}],
        "model": "minara/claude-opus-5-5", "workspace_root": str(tmp_path), "project_context": False, "exec_policy": "off",
        "conversation_state": False, "journal_path": str(tmp_path / "journal.jsonl"),
        "prompts_file": str(tmp_path / "absent.json")}, **extra)), encoding="utf-8")
    return config.load(str(path))


class Listing:
    def __init__(self, items, status=200):
        self.status_code, self._items = status, items

    def json(self):
        return {"data": self._items}


class FakeClient:
    def __init__(self, items, credits=500.0):
        self.items, self.balance, self.base = items, credits, None

    def get(self, path):
        assert path == "/v1/models"
        return Listing(self.items)

    def credits(self):
        return self.balance


def listing_of(extra=()):
    """Minara's list: every model the shipped catalog knows, as it lists them, and `extra`."""
    items = [{"id": upstream, "pricing": entry.get("pricing"), "supported_endpoint_types": ["openai"]}
             for upstream, entry in minara_catalog.SHIPPED.items()]
    return items + list(extra)


def refresher(cfg, client, **kwargs):
    backend = SimpleNamespace(cfg=dict(cfg["accounts"][0], enabled=True, base="https://api.minara.ai/v1"),
                              unavailable=None,
                              credits=SimpleNamespace(balance_snapshot=lambda: {"available": client.balance},
                                                      get_balance=lambda force_refresh=False: {}))
    return minara_refresh.CatalogRefresher(cfg, backends=[backend], client_factory=lambda token: client, published={},
                                           **kwargs)


def full_study():
    rows = {"_question": "hard"}
    for level, tokens in zip(("default",) + minara_probe.EFFORTS, (300, 0, 280, 290, 300, 310, 600, 900)):
        rows[level] = [{"status": 200, "reasoning_tokens": tokens}, {"status": 200, "reasoning_tokens": tokens + 5}]
    return rows


def measured_ok(client, item, checks, samples=2, stop=None, study_budget=None):
    client.balance -= 12.5  # what the measurement cost
    results = {"tools": {"status": "ok", "tools": True, "id": "gen-1", "served": item["id"]},
               "vision": {"status": "ok", "vision": True},
               "thinking": {"status": "ok", "thinking": "end", "reasoning_tokens": 40},
               "cache": {"status": "ok", "caches": True, "written": 5800, "read": 5800}, "checked_at": "2026-11-10"}
    # The real study stops after its first request when the whole would cost more than its budget (minara_probe).
    affordable = study_budget is None or study_budget >= 100
    return results, (full_study() if "study" in checks and affordable else None)


def test_a_new_model_is_measured_published_and_routed_without_a_restart(tmp_path, monkeypatch):
    cfg = relay_cfg(tmp_path)
    routes = cfg["_minara_models"]
    assert "minara/claude-test-new" not in routes
    seen = []
    monkeypatch.setattr(minara_probe, "measure_model", lambda *a, **k: seen.append(a[2]) or measured_ok(*a, **k))
    client = FakeClient(listing_of([{"id": NEW, "pricing": PRICE, "supported_endpoint_types": ["openai"]}]))
    before = journal.JOURNAL.last_seq
    summary = refresher(cfg, client).run_once()
    assert summary["added"] == [NEW] and summary["spent_credits"] == 12.5
    assert seen == [["tools", "vision", "thinking", "cache", "study"]]
    # The same dict every account's cfg holds now routes the model, with its own study's levels and the listed price.
    entry = routes["minara/claude-test-new"]
    assert routes is cfg["_minara_models"] and entry["upstream_model"] == NEW and entry["pricing"] == PRICE
    assert entry["reasoning_effort_levels"] == ["xhigh", "max"] and entry["metadata_status"] == "measured 2026-11-10"
    saved = json.loads((tmp_path / minara_catalog.AUTO_NAME).read_text(encoding="utf-8"))
    assert saved["models"][NEW]["status"] == "ok" and saved["models"][NEW]["measured_by"] == "relay"
    kinds = [event["kind"] for event in journal.JOURNAL.query(since=before)]
    assert "catalog.model_added" in kinds and "catalog.refresh" in kinds
    # A relay started later reads it from the overlay: no measurement again.
    minara_catalog.MEASURED.pop(NEW, None)
    again = relay_cfg(tmp_path)
    assert "minara/claude-test-new" in again["_minara_models"]
    seen.clear()
    assert refresher(again, client).run_once()["added"] == [] and seen == []


def test_a_study_over_the_budget_is_left_out_and_the_family_levels_are_taken(tmp_path, monkeypatch):
    cfg = relay_cfg(tmp_path, minara_catalog_budget=200)
    seen = []
    budgets = []

    def measure(*args, **kwargs):
        seen.append(args[2])
        budgets.append(kwargs.get("study_budget"))
        return measured_ok(*args, **kwargs)
    monkeypatch.setattr(minara_probe, "measure_model", measure)
    # Ten times Sonnet's price: about 150 credits for the checks, so the study keeps about 50 and is left out.
    expensive = dict(PRICE, prompt=30, completion=150, cache_read=3, cache_write=37.5)
    item = {"id": "anthropic/claude-opus-5.6", "pricing": expensive, "supported_endpoint_types": ["openai"]}
    summary = refresher(cfg, FakeClient(listing_of([item]))).run_once()
    assert seen == [["tools", "vision", "thinking", "cache", "study"]] and summary["added"] == ["anthropic/claude-opus-5.6"]
    checks_cost = minara_probe.measure_estimate(expensive, ["tools", "vision", "thinking", "cache"])
    assert budgets == [pytest.approx(200 - checks_cost)]
    entry = minara_catalog.MEASURED["anthropic/claude-opus-5.6"]
    # The family's levels, from a model with its own study.
    assert entry["effort_source"] == "family:anthropic/claude-opus-5" and entry["effort_effect"] == "inherited"
    assert entry["effort_map"] == minara_catalog.MEASURED["anthropic/claude-opus-5"]["effort_map"]
    # Where even the checks would not fit, the model waits for the next look.
    cfg["minara_catalog_budget"] = 20
    other = dict(item, id="anthropic/claude-opus-5.7")
    assert refresher(cfg, FakeClient(listing_of([other]))).run_once()["skipped"] == ["anthropic/claude-opus-5.7"]


def test_the_newest_studied_model_of_the_family_gives_its_levels():
    measured = {name: dict(entry) for name, entry in minara_catalog.MEASURED.items()}
    fresh = {"upstream_model": "anthropic/claude-fable-5.3", "reasoning": True, "effort_map": None}
    entry = minara_refresh.inherit_effort(fresh, measured)
    assert entry["effort_source"] == "family:anthropic/claude-fable-5.2"  # studied, newer than 5.1; Fable 5 has none


class Pilot:
    """A client whose every study request costs `cost` credits."""

    def __init__(self, cost):
        self.cost, self.balance, self.asked = cost, 1000.0, 0

    def credits(self):
        return self.balance

    def wait(self, seconds):
        return False


def test_the_study_stops_after_its_first_request_when_the_whole_would_not_fit(monkeypatch):
    def row(client, model, level):
        client.asked += 1
        client.balance -= client.cost
        return {"status": 200, "reasoning_tokens": 100}
    monkeypatch.setattr(minara_probe, "study_row", row)
    cheap, dear = Pilot(1.0), Pilot(10.0)
    assert minara_probe.study_effort(cheap, "m", samples=2, budget=50) is not None and cheap.asked == 16
    assert minara_probe.study_effort(dear, "m", samples=2, budget=50) is None and dear.asked == 1


def test_a_new_price_is_taken_at_once(tmp_path):
    cfg = relay_cfg(tmp_path)
    items = listing_of()
    target = next(item for item in items if item["id"] == "anthropic/claude-opus-5.5")
    target["pricing"] = dict(target["pricing"], prompt=3.5)
    summary = refresher(cfg, FakeClient(items)).run_once()
    assert summary["repriced"] == ["anthropic/claude-opus-5.5"]
    assert cfg["_minara_models"]["minara/claude-opus-5-5"]["pricing"]["prompt"] == 3.5


def test_a_model_gone_from_two_complete_lists_is_unpublished_and_a_short_list_counts_not(tmp_path):
    cfg = relay_cfg(tmp_path)
    items = [item for item in listing_of() if item["id"] != "x-ai/grok-4.7"]
    short = items[:30]  # one of Minara's short answers
    fixed = refresher(cfg, FakeClient(short))
    assert fixed.run_once()["gone"] == []
    assert "minara/grok-4.7" in cfg["_minara_models"]
    assert fixed.run_once()["gone"] == []  # still short: nothing counted
    complete = refresher(cfg, FakeClient(items))
    assert complete.run_once()["gone"] == []  # first complete list without it
    assert complete.run_once()["gone"] == ["x-ai/grok-4.7"]  # second one
    assert "minara/grok-4.7" not in cfg["_minara_models"]


def test_an_overlay_entry_wins_only_while_it_is_newer_than_the_shipped_file(tmp_path):
    path = tmp_path / minara_catalog.AUTO_NAME
    shipped = minara_catalog.SHIPPED["anthropic/claude-opus-5.5"]
    newer = dict(shipped, label="NEWER", refreshed_at="2999-01-01T00:00:00+00:00")
    path.write_text(json.dumps({"version": 1, "models": {"anthropic/claude-opus-5.5": newer}}), encoding="utf-8")
    assert minara_catalog.apply_overlay(path) == ["anthropic/claude-opus-5.5"]
    assert minara_catalog.MEASURED["anthropic/claude-opus-5.5"]["label"] == "NEWER"
    older = dict(shipped, label="OLDER", refreshed_at="2000-01-01T00:00:00+00:00")
    path.write_text(json.dumps({"version": 1, "models": {"anthropic/claude-opus-5.5": older}}), encoding="utf-8")
    minara_catalog.apply_overlay(path)
    assert minara_catalog.MEASURED["anthropic/claude-opus-5.5"] is shipped  # the shipped file is newer: it is back
    assert minara_catalog.apply_overlay(tmp_path / "absent.json") == []


def test_a_level_moved_to_messages_is_kept_in_the_overlay_and_restored(tmp_path):
    cfg = relay_cfg(tmp_path)
    refresher(cfg, FakeClient(listing_of()))
    assert minara.switch_to_messages("anthropic/claude-opus-5.5", "high", reason="test")
    saved = json.loads((tmp_path / minara_catalog.AUTO_NAME).read_text(encoding="utf-8"))
    assert saved["switched"] == {"anthropic/claude-opus-5.5": ["high"]}
    minara.SWITCHED.clear()
    refresher(cfg, FakeClient(listing_of()))  # a relay started again
    assert minara.SWITCHED == {"anthropic/claude-opus-5.5": {"high"}}


def test_the_refresh_needs_minara_accounts_and_can_be_switched_off(tmp_path):
    cfg = relay_cfg(tmp_path, minara_catalog_refresh_hours=0)
    assert not refresher(cfg, FakeClient([])).start()
    assert not minara_refresh.CatalogRefresher(relay_cfg(tmp_path), backends=[]).start()


@pytest.mark.parametrize("key,value", [("minara_catalog_refresh_hours", -1), ("minara_catalog_refresh_hours", "6"),
                                       ("minara_catalog_budget", 1e9)])
def test_bad_refresh_settings_stop_the_start(tmp_path, key, value):
    with pytest.raises(Exception) as error:
        relay_cfg(tmp_path, **{key: value})
    assert key in str(error.value)


def test_messages_levels_come_from_the_channels_on_chat_and_on_messages():
    chat = {level: ("openrouter" if level in ("low", "medium", "high") else "bedrock")
            for level in minara_probe.CHANNEL_LEVELS}
    cached = {level: {"status": 200, "channel": "bedrock", "caches": True} for level in minara_probe.CHANNEL_LEVELS}
    results = {"anthropic/claude-opus-4.7": {
        "tools": {"status": "ok", "tools": True, "id": "gen-1"}, "thinking": {"status": "ok", "thinking": "end"},
        "channels": {"status": "ok", "channels": chat}, "messages": {"status": "ok", "levels": cached}},
        "anthropic/claude-sonnet-4.6": {
        "tools": {"status": "ok", "tools": True, "id": "gen-1"}, "thinking": {"status": "ok", "thinking": "end"},
        "channels": {"status": "ok", "channels": {level: "bedrock" for level in minara_probe.CHANNEL_LEVELS}},
        "messages": {"status": "ok", "levels": cached}}}
    listing = [{"id": name, "supported_endpoint_types": ["openai", "anthropic"]} for name in results]
    entries = {entry["upstream_model"]: entry for entry in minara_probe.generate(results, listing, {})["models"]}
    opus, sonnet = entries["anthropic/claude-opus-4.7"], entries["anthropic/claude-sonnet-4.6"]
    assert opus["status"] == "ok" and opus["messages_levels"] == ["default", "none", "minimal", "xhigh", "max"]
    assert opus["default_effort"] == "high"  # Default stays the level that caches on chat
    assert opus["effort_map"]["max"] == "max" and "max" in opus["reasoning_effort_levels"]
    assert sonnet["status"] == "ok" and sonnet["messages_levels"] == list(minara_probe.CHANNEL_LEVELS)
    assert "xhigh" not in sonnet["reasoning_effort_levels"]  # Claude 4.6 knows no xhigh
    without = dict(results["anthropic/claude-sonnet-4.6"], messages={"status": "failed", "levels": {}})
    alone = minara_probe.generate({"anthropic/claude-sonnet-4.6": without}, listing[1:], {})["models"][0]
    assert alone["status"] == "uncached" and alone["messages_levels"] is None


# Gemini 4 Carbon's price as Minara lists it (2026-11-13): an expression whose `ai` is audio input (new-api's billing
# expression). The relay's reader did not know `ai`, so Carbon and Hydrogen waited unmeasured, named nowhere but in a
# count of "отложено", and their turns would have had no price.
CARBON = {"quota_type": 0, "billing_mode": "tiered_expr", "group_ratio": 1, "prompt": None, "completion": None,
          "cache_read": None, "cache_write": None, "request": None,
          "billing_expr": "p * 2 + c * 10 + cr * 0.1 + cc * 0.1875 + ai * 2"}


def test_a_new_model_priced_by_an_expression_with_audio_input_is_measured(tmp_path, monkeypatch):
    cfg = relay_cfg(tmp_path)
    monkeypatch.setattr(minara_probe, "measure_model", measured_ok)
    item = {"id": "google/gemini-test-carbon", "pricing": CARBON, "supported_endpoint_types": ["openai"]}
    summary = refresher(cfg, FakeClient(listing_of([item]))).run_once()
    assert summary["added"] == ["google/gemini-test-carbon"] and summary["skipped"] == []
    assert summary["unpriced"] == {}  # every published model of the shipped catalog has a price the relay reads


def test_a_model_whose_price_cannot_be_read_is_not_measured_and_says_why(tmp_path, monkeypatch, capsys):
    cfg = relay_cfg(tmp_path)
    monkeypatch.setattr(minara_probe, "measure_model", lambda *a, **k: pytest.fail("an unpriced model was measured"))
    unreadable = dict(CARBON, billing_expr="p * 2 + c * 10 + vid * 3")
    item = {"id": "google/gemini-test-video", "pricing": unreadable, "supported_endpoint_types": ["openai"]}
    before = journal.JOURNAL.last_seq
    summary = refresher(cfg, FakeClient(listing_of([item]))).run_once()
    assert summary["skipped"] == ["google/gemini-test-video"] and summary["price_unreadable"] == summary["skipped"]
    assert summary["skipped_reasons"]["google/gemini-test-video"].endswith("неизвестная переменная vid")
    record = json.loads((tmp_path / minara_catalog.AUTO_NAME).read_text(encoding="utf-8"))["measurements"][
        "google/gemini-test-video"]
    assert record["attempts"] == 0 and record["skipped"]["price_unreadable"] is True and "vid" in record["skipped"]["reason"]
    events = journal.JOURNAL.query(since=before, limit=500)
    said = [event for event in events if event["kind"] == "catalog.model_skipped"]
    assert len(said) == 1 and said[0]["level"] == "warn" and "google/gemini-test-video" in said[0]["message"]
    assert "google/gemini-test-video" in [event for event in events if event["kind"] == "catalog.refresh"][-1]["message"]
    assert "[catalog] Minara: новая модель google/gemini-test-video не измерена" in capsys.readouterr().out


def test_a_model_over_the_budget_waits_with_its_reason_as_information(tmp_path, monkeypatch):
    cfg = relay_cfg(tmp_path, minara_catalog_budget=1)
    monkeypatch.setattr(minara_probe, "measure_model", lambda *a, **k: pytest.fail("measured over the budget"))
    item = {"id": "google/gemini-test-carbon", "pricing": CARBON, "supported_endpoint_types": ["openai"]}
    before = journal.JOURNAL.last_seq
    summary = refresher(cfg, FakeClient(listing_of([item]))).run_once()
    assert summary["skipped"] == ["google/gemini-test-carbon"] and summary["price_unreadable"] == []
    assert "бюджете" in summary["skipped_reasons"]["google/gemini-test-carbon"]
    said = [event for event in journal.JOURNAL.query(since=before, limit=500) if event["kind"] == "catalog.model_skipped"]
    assert len(said) == 1 and said[0]["level"] == "info"


def test_a_published_model_repriced_into_a_price_the_relay_cannot_read_is_a_warning(tmp_path):
    cfg = relay_cfg(tmp_path)
    upstream = next(name for name, entry in minara_catalog.MEASURED.items() if entry.get("status") == "ok")
    items = [dict(item, pricing=dict(CARBON, billing_expr="p * 2 + c * 10 + zz * 1")) if item["id"] == upstream else item
             for item in listing_of()]
    before = journal.JOURNAL.last_seq
    summary = refresher(cfg, FakeClient(items)).run_once()
    assert upstream in summary["repriced"] and summary["unpriced"][upstream].endswith("неизвестная переменная zz")
    warned = [event for event in journal.JOURNAL.query(since=before, limit=500)
              if event["kind"] == "catalog.price_unreadable"]
    assert len(warned) == 1 and warned[0]["level"] == "warn" and warned[0]["data"]["upstream_model"] == upstream


# The provider's own list of a model's effort levels (OpenRouter's public /api/v1/models, its `reasoning` field). The effort
# study's two samples a level offered Gemini 4 Hydrogen and GPT-6.1 Sol max alone (2026-11-13), though Hydrogen reasoned
# ~1,100 tokens at low, ~1,450 at medium and high and ~9,000 at xhigh: the provider's list says what the model takes.
HYDROGEN_INFO = {"reasoning": {"mandatory": True, "supported_efforts": ["xhigh", "high", "medium", "low"],
                               "default_effort": "medium"}}


def test_the_provider_list_gives_the_levels_and_the_nearest_for_the_others():
    levels, effort_map, default = minara_probe.provider_levels(HYDROGEN_INFO)
    assert levels == ["low", "medium", "high", "xhigh"] and default == "medium"
    assert effort_map == {"none": "low", "minimal": "low", "low": "low", "medium": "medium", "high": "high",
                          "xhigh": "xhigh", "max": "xhigh"}
    # A level the study saw broken is never offered, and goes as the nearest one that works (the lower of two as near).
    levels, effort_map, _ = minara_probe.provider_levels(HYDROGEN_INFO, broken={"high"})
    assert levels == ["low", "medium", "xhigh"] and effort_map["high"] == "medium"
    assert minara_probe.provider_levels({}) is None and minara_probe.provider_levels({"reasoning": {}}) is None


def test_generated_entries_take_the_provider_levels_except_claude():
    listing = [{"id": "google/gemini-test", "supported_endpoint_types": ["openai"]},
               {"id": "anthropic/claude-fable-5.9", "supported_endpoint_types": ["openai"]}]
    results = {name: {"tools": {"status": "ok", "tools": True, "id": "gen-1"},
                      "thinking": {"status": "ok", "thinking": "hidden", "reasoning_tokens": 80}} for name in
               ("google/gemini-test", "anthropic/claude-fable-5.9")}
    published = {"google/gemini-test": HYDROGEN_INFO,
                 "anthropic/claude-fable-5.9": {"reasoning": {"supported_efforts": ["high"], "default_effort": "high"}}}
    entries = {entry["upstream_model"]: entry for entry in minara_probe.generate(results, listing, published)["models"]}
    gemini, fable = entries["google/gemini-test"], entries["anthropic/claude-fable-5.9"]
    assert gemini["reasoning_effort_levels"] == ["low", "medium", "high", "xhigh"] and gemini["effort_source"] == "openrouter"
    assert gemini["effort_map"]["max"] == "xhigh" and gemini["natural_effort"] == "medium"
    assert fable["reasoning_effort_levels"] == list(minara_probe.native_levels("anthropic/claude-fable-5.9"))


def test_a_published_model_takes_the_provider_levels_on_a_look_without_a_measurement(tmp_path, monkeypatch):
    cfg = relay_cfg(tmp_path)
    monkeypatch.setattr(minara_probe, "measure_model", lambda *a, **k: pytest.fail("no measurement is needed"))
    upstream = "google/gemini-4-argon"
    assert minara_catalog.MEASURED[upstream]["status"] == "ok"
    claude = next(name for name, entry in minara_catalog.MEASURED.items()
                  if name.startswith("anthropic/") and entry.get("status") == "ok" and entry.get("reasoning"))
    claude_levels = list(minara_catalog.MEASURED[claude].get("reasoning_effort_levels") or [])
    published = {upstream: {"reasoning": {"mandatory": True, "supported_efforts": ["high", "medium", "low"],
                                          "default_effort": "medium"}},
                 claude: {"reasoning": {"supported_efforts": ["low"], "default_effort": "low"}}}
    backend = SimpleNamespace(cfg=dict(cfg["accounts"][0], enabled=True, base="https://api.minara.ai/v1"),
                              unavailable=None, credits=SimpleNamespace(balance_snapshot=lambda: {"available": 500.0},
                                                                        get_balance=lambda force_refresh=False: {}))
    client = FakeClient(listing_of())
    value = minara_refresh.CatalogRefresher(cfg, backends=[backend], client_factory=lambda token: client,
                                            published=published)
    before = journal.JOURNAL.last_seq
    summary = value.run_once()
    assert summary["releveled"] == [upstream]
    entry = minara_catalog.MEASURED[upstream]
    assert entry["reasoning_effort_levels"] == ["low", "medium", "high"] and entry["effort_source"] == "openrouter"
    assert minara_catalog.MEASURED[claude].get("reasoning_effort_levels") == claude_levels  # Claude keeps its own
    route, sent = models.resolve("minara/gemini-4-argon", "max", cfg=cfg)
    assert sent == "high" and route["reasoning_effort_levels"] == ["low", "medium", "high"]
    assert any(event["kind"] == "catalog.model_releveled" for event in journal.JOURNAL.query(since=before, limit=200))
    # The next look finds the levels in place: nothing to do.
    assert value.run_once()["releveled"] == []
