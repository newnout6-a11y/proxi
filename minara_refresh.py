"""The relay keeps its Minara catalog current by itself: a model Minara adds, a new price, a model gone.

minara_models.json is measured by hand (scripts/probe_minara.py) and ships with the code; Minara adds models between
two such runs (Claude Fable 5.2 on 2026-11-10 was listed and never published). Every `minara_catalog_refresh_hours`
(6 by default, 0 switches it off), first two minutes after the start, the relay:

- reads Minara's /v1/models (free) with the Minara account that has the most credits;
- takes the new prices of the models it knows at once (Minara debits its own list: a stale price misprices every turn);
- measures each new chat model with exactly the checks of the manual run (minara_probe: tools, image input, how the
  reasoning arrives, the channels on chat and on /v1/messages or a cache check for Claude, the effort study on the hard
  question) and publishes it when they pass: at most `minara_catalog_budget` credits a look (300 by default; a model
  whose study would not fit is measured without it and takes its family's levels);
- unpublishes a model missing from two complete lists in a row (a short answer, which Minara gives at times, counts not).

What it learns goes to minara_models.auto.json beside config.json (minara_catalog.apply_overlay puts it over the shipped
file, and a newer shipped file wins again) and takes effect without a restart: /v1/models, /codex/models and the routes
see the model at once. The same file keeps the Claude levels the relay moved to /v1/messages (minara.switch_to_messages).
Nothing here is ever sent to Retool, and the account's balance is read again after a measurement.
"""
import copy
import datetime
import json
import os
import pathlib
import tempfile
import threading
import time

import console_log
import journal
import minara
import minara_catalog
import minara_probe

FIRST_DELAY = 120.0
GONE_AFTER = 2  # complete lists in a row without a model before it is unpublished
COMPLETE_SHARE = 0.9  # a list this much shorter than the last one is one of Minara's short answers
MAX_ATTEMPTS = 4  # a model whose checks keep failing is tried again at most this often a day
STUDY_SAMPLES = 2
FINAL = frozenset({"ok", "broken", "substituted", "uncached", "excluded"})
VERSION = 1


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _pricing_key(pricing):
    return json.dumps(pricing, sort_keys=True, default=str)


def chat_model(item):
    """True for a model of the list the relay can publish: a chat or responses model, not one the owner excluded."""
    upstream = item.get("id")
    return (isinstance(upstream, str) and "/" in upstream
            and (minara_probe.supports_chat(item) or minara_probe.supports_responses(item))
            and upstream not in minara_probe.NOT_CHAT and upstream not in minara_probe.INHERIT
            and not minara_probe.excluded(upstream))


def inherit_effort(entry, measured):
    """A measured entry without its own effort study takes the levels of a published model of its family (else the
    vendor's flagship): the levels a client is offered and the map, never the channels or the cache."""
    if not entry.get("reasoning") or entry.get("effort_map"):
        return entry
    upstream = entry["upstream_model"]
    vendor, family = upstream.split("/")[0], minara_probe._family(upstream)
    # A model with its own study first, the newest of them (Fable 5.1 before Fable 5, whose levels are the Messages API's).
    donors = sorted((other for name, other in measured.items()
                     if name != upstream and other.get("status") == "ok" and other.get("effort_map")
                     and name.split("/")[0] == vendor and minara_probe._family(name) == family),
                    key=lambda other: (other.get("effort_source") == "study", other["upstream_model"]), reverse=True)
    flagship = measured.get(minara_probe.STUDY_FLAGSHIP.get(vendor) or "")
    donor = donors[0] if donors else flagship if flagship and flagship.get("effort_map") else None
    if donor is None:
        return entry
    result = dict(entry)
    for key in ("reasoning_effort_levels", "effort_map", "effort_control", "effort_study", "natural_effort"):
        result[key] = copy.deepcopy(donor.get(key))
    result["effort_source"], result["effort_effect"] = "family:" + donor["upstream_model"], "inherited"
    return result


class CatalogRefresher:
    def __init__(self, cfg, dispatcher=None, backends=None, client_factory=None, published=None):
        self.cfg = cfg
        self.dispatcher = dispatcher
        pool = backends if backends is not None else list(getattr(dispatcher, "backends", None) or [])
        self.backends = [b for b in pool if (getattr(b, "cfg", None) or {}).get("upstream") == "minara"]
        self.path = cfg.get("_minara_catalog_file")
        self.hours = float(cfg.get("minara_catalog_refresh_hours", 6) or 0)
        self.budget = float(cfg.get("minara_catalog_budget", 300) or 0)
        self._stop = threading.Event()
        self._thread = None
        self._file_lock = threading.Lock()
        self._factory = client_factory
        self._published = published  # OpenRouter's public list (windows, output ceilings, levels); fetched when None
        self._published_given = published  # a list given (tests) is the one every look reads
        self.last = None
        self.next_at = None
        self.after_look = None  # called with "minara" after every look (client_sync.OpenCodeSync: the client lists)
        for upstream, levels in (self._read().get("switched") or {}).items():
            for level in levels if isinstance(levels, list) else []:
                if minara.messages_supported(upstream):
                    with minara._SWITCH_LOCK:
                        minara.SWITCHED.setdefault(upstream, set()).add(level)
        minara.on_switch(self._switched)

    # -- lifecycle ---------------------------------------------------------------------------------------------------
    def start(self):
        if not self.backends or self.hours <= 0 or not self.path or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._loop, name="minara-catalog", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)

    def _loop(self):
        delay = FIRST_DELAY
        while True:
            self.next_at = time.time() + delay
            if self._stop.wait(delay):
                return
            self.look()
            delay = self.hours * 3600.0

    def look(self):
        """One look (run_once), then `after_look`, even after a failed look: the client lists follow what is published."""
        try:
            self.run_once()
        except Exception as exc:  # noqa: BLE001 - a failed look waits for the next one; the relay goes on
            journal.emit("catalog.refresh_failed", "Проверка каталога Minara не удалась: %s" % type(exc).__name__,
                         "warn", upstream="minara", error=type(exc).__name__)
        if self.after_look is not None and not self._stop.is_set():
            try:
                self.after_look("minara")
            except Exception as exc:  # noqa: BLE001 - the client list waits for the next look
                journal.emit("client.opencode_sync_failed", "Список моделей OpenCode не обновлён: %s" % type(exc).__name__,
                             "warn", client="opencode", reason="minara", error=type(exc).__name__)

    # -- the overlay file --------------------------------------------------------------------------------------------
    def _read(self):
        if not self.path:
            return {}
        try:
            data = json.loads(pathlib.Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) and data.get("version") == VERSION else {}

    def _write(self, state):
        state["version"] = VERSION
        folder = os.path.dirname(os.path.abspath(self.path))
        handle, temporary = tempfile.mkstemp(prefix=".minara-catalog-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=1)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _switched(self, upstream, level, switched):
        if not self.path:
            return
        with self._file_lock:
            state = self._read()
            levels = set((state.setdefault("switched", {})).get(upstream) or [])
            (levels.add if switched else levels.discard)(level)
            if levels:
                state["switched"][upstream] = sorted(levels)
            else:
                state["switched"].pop(upstream, None)
            self._write(state)

    # -- one look ----------------------------------------------------------------------------------------------------
    def _account(self):
        """(backend, client) of the enabled Minara account in the pool with the most credits, or (None, None)."""
        best, credits = None, None
        for backend in self.backends:
            cfg = backend.cfg
            if not cfg.get("enabled", True) or getattr(backend, "unavailable", None) or not minara.token_of(cfg):
                continue
            snapshot = getattr(getattr(backend, "credits", None), "balance_snapshot", lambda: {})() or {}
            available = snapshot.get("available")
            value = available if isinstance(available, (int, float)) else -1.0
            if best is None or value > credits:
                best, credits = backend, value
        if best is None:
            return None, None
        token = minara.token_of(best.cfg)
        client = (self._factory(token) if self._factory is not None
                  else minara_probe.Client(token, wait=self._stop.wait, cfg=best.cfg))
        base = str(best.cfg.get("base") or "").rstrip("/")
        if base.endswith("/v1"):
            client.base = base[:-3]
        return best, client

    def run_once(self):
        """One look at Minara's list; a summary of what changed (also kept as self.last)."""
        backend, client = self._account()
        try:
            return self._run_once_with_client(backend, client)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _run_once_with_client(self, backend, client):
        summary = {"at": _now(), "listed": 0, "added": [], "repriced": [], "releveled": [], "gone": [], "failed": [],
                   "skipped": [],
                   "skipped_reasons": {}, "price_unreadable": [], "unpriced": {}, "spent_credits": 0.0}
        if client is None:
            summary["error"] = "no_account"
            self.last = summary
            return summary
        response = client.get("/v1/models")
        data = response.json() if response.status_code == 200 else None
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            summary["error"] = "HTTP %s" % response.status_code
            journal.emit("catalog.refresh_failed", "Minara не отдала список моделей (HTTP %s)" % response.status_code,
                         "warn", upstream="minara", status=response.status_code)
            self.last = summary
            return summary
        listed = {item["id"]: item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)}
        summary["listed"] = len(listed)
        with self._file_lock:
            state = self._read()
        previous = state.get("listing") or {}
        reference = len(previous) or len(minara_catalog.SHIPPED)
        complete = len(listed) >= COMPLETE_SHARE * reference
        now = _now()
        measured = minara_catalog.snapshot()
        overlay = state.setdefault("models", {})
        # Prices and endpoint capabilities: Minara debits its own list, and may enable /v1/responses dynamically.
        for upstream, item in listed.items():
            entry = measured.get(upstream)
            if entry is None:
                continue
            current = overlay.get(upstream) or entry
            if isinstance(item.get("pricing"), dict):
                if _pricing_key(current.get("pricing")) != _pricing_key(item["pricing"]):
                    current = overlay[upstream] = dict(current, pricing=copy.deepcopy(item["pricing"]), refreshed_at=now)
                    summary["repriced"].append(upstream)
                # A published model whose price the relay cannot read has every turn unpriced: said on each look, never quietly.
                problem = minara.price_problem(item["pricing"]) if entry.get("status") == "ok" else None
                if problem:
                    summary["unpriced"][upstream] = problem
            ep_types = minara_probe.endpoint_types(item)
            if ep_types:
                has_resp = minara_probe.supports_responses(ep_types)
                if (has_resp != bool(current.get("responses"))
                        or ("supported_endpoint_types" in current
                            and current.get("supported_endpoint_types") != ep_types)):
                    overlay[upstream] = dict(current, responses=has_resp, supported_endpoint_types=ep_types,
                                             refreshed_at=now)
        missing = state.setdefault("missing", {})
        self._relevel(listed, measured, overlay, summary, now)
        if complete:
            for upstream, entry in list(measured.items()):
                if upstream in listed:
                    missing.pop(upstream, None)
                    continue
                if entry.get("status") != "ok":
                    continue
                record = missing.setdefault(upstream, {"count": 0, "since": now})
                record["count"] += 1
                if record["count"] >= GONE_AFTER:
                    overlay[upstream] = dict(entry, status="gone", gone_since=record["since"], refreshed_at=now)
                    summary["gone"].append(upstream)
        self._measure_new(client, listed, measured, overlay, state, summary, now)
        if complete:
            state["listing"] = {upstream: {k: item.get(k) for k in ("id", "pricing", "supported_endpoint_types")}
                                for upstream, item in listed.items()}
        state["refreshed_at"] = now
        state["spent_credits"] = round(float(state.get("spent_credits") or 0) + summary["spent_credits"], 4)
        with self._file_lock:
            fresh = self._read()
            if "switched" in fresh and isinstance(fresh["switched"], dict):
                state["switched"] = fresh["switched"]
            elif not isinstance(state.get("switched"), dict):
                state["switched"] = {}
            self._write(state)
        changed = minara_catalog.apply_overlay(self.path)
        if changed:
            self.install()
        if summary["spent_credits"] and getattr(backend, "credits", None) is not None:
            try:
                backend.credits.get_balance(force_refresh=True)  # the pool sees what the measurement spent
            except Exception:  # noqa: BLE001
                pass
        self._report(summary)
        self.last = summary
        return summary

    def _measure_new(self, client, listed, measured, overlay, state, summary, now):
        budget = self.budget
        records = state.setdefault("measurements", {})
        today = now[:10]
        for upstream, item in sorted(listed.items()):
            if self._stop.is_set():
                return
            entry = measured.get(upstream)
            if not chat_model(item) or (entry is not None and entry.get("status") in FINAL):
                continue
            record = records.setdefault(upstream, {"attempts": 0, "day": today})
            if record.get("day") != today:
                record.update(attempts=0, day=today)
            if record["attempts"] >= MAX_ATTEMPTS:
                continue
            checks = minara_probe.checks_for(item)
            # The checks' cost is known from the price list; the study's is not (300 to 27,000 tokens a request on its
            # question): it gets what is left and stops after its first request when it would not fit. A price the relay
            # cannot read leaves the model unmeasured (its turns would have no price), and says why: Gemini 4 Hydrogen
            # and Carbon waited unseen on 2026-11-13 for the `ai` (audio input) of their expressions.
            problem = minara.price_problem(item.get("pricing"))
            estimate = None if problem else minara_probe.measure_estimate(
                item.get("pricing"), [c for c in checks if c != "study"])
            if estimate is None or estimate > budget:
                reason = problem or ("замер стоит около %.0f кредитов, а в бюджете этой проверки осталось %.0f" % (
                    estimate, budget) if estimate is not None else "стоимость замера не посчитать")
                unreadable = bool(problem or estimate is None)
                record["skipped"] = {"at": now, "reason": reason, "price_unreadable": unreadable}
                summary["skipped"].append(upstream)
                summary["skipped_reasons"][upstream] = reason
                if unreadable:
                    summary["price_unreadable"].append(upstream)
                continue
            record.pop("skipped", None)
            try:
                before = client.credits()
            except Exception:  # noqa: BLE001
                before = None
            results, study = minara_probe.measure_model(client, item, checks, samples=STUDY_SAMPLES,
                                                        stop=self._stop.is_set, study_budget=budget - estimate)
            try:
                spent = max(0.0, before - client.credits()) if before is not None else estimate
            except Exception:  # noqa: BLE001
                spent = estimate
            budget -= spent
            summary["spent_credits"] = round(summary["spent_credits"] + spent, 4)
            record.update(attempts=record["attempts"] + 1, last=now, spent=round(spent, 4), results=results,
                          study=study)
            fresh = self.entry(upstream, item, results, study)
            overlay[upstream] = dict(fresh, refreshed_at=now, measured_by="relay")
            if fresh["status"] == "ok":
                summary["added"].append(upstream)
            else:
                summary["failed"].append([upstream, fresh["status"]])

    def _published_now(self):
        """OpenRouter's public list, read again on each look (a model's levels can change); the last one read, or the one
        given to the constructor (tests), when it cannot be read."""
        if self._published_given is not None:
            return self._published_given
        try:
            self._published = minara_probe.fetch_published()
        except Exception:  # noqa: BLE001 - the last list read stands; the look goes on
            pass
        return self._published or {}

    def _relevel(self, listed, measured, overlay, summary, now):
        """A published model takes the effort levels its provider documents (minara_probe.provider_levels), at once and
        with no measurement: the effort study's two samples a level offered Gemini 4 Hydrogen and GPT-6.1 Sol max alone,
        though their low, medium and high differ (2026-11-13). Claude keeps its own levels; a level the study saw broken
        or refused is never offered."""
        published = None
        for upstream in sorted(listed):
            entry = overlay.get(upstream) or measured.get(upstream)
            if (not entry or entry.get("status") != "ok" or not entry.get("reasoning")
                    or not minara_probe.provider_levels_apply(upstream, entry.get("backend"))):
                continue
            if published is None:
                published = self._published_now()
            study = entry.get("effort_study") or {}
            documented = minara_probe.provider_levels(published.get(upstream),
                                                      set(study.get("broken") or ()) | set(study.get("refused") or ()))
            if documented is None:
                continue
            levels, effort_map, default = documented
            natural = default or entry.get("natural_effort")
            if (entry.get("reasoning_effort_levels"), entry.get("effort_map"), entry.get("natural_effort"),
                    entry.get("effort_control")) == (levels, effort_map, natural, "mapped"):
                continue
            overlay[upstream] = dict(entry, reasoning_effort_levels=levels, effort_map=effort_map, effort_control="mapped",
                                     natural_effort=natural, effort_source="openrouter", refreshed_at=now)
            summary["releveled"].append(upstream)

    def entry(self, upstream, item, results, study):
        """The catalog entry the measurements make (minara_probe.generate), with its family's levels when unstudied."""
        published = self._published
        if published is None:
            try:
                published = self._published = minara_probe.fetch_published()
            except Exception:  # noqa: BLE001 - no window known now (asked again next time): the relay's catalog may have it
                published = {}
        studies = {upstream: dict(study, _question="hard")} if study else {}
        generated = minara_probe.generate({upstream: results}, [item], published, studies)
        return inherit_effort(generated["models"][0], minara_catalog.snapshot())

    def install(self):
        """Rebuild the routes from the measured catalog, in place: every account's cfg holds the same dict."""
        import models
        routes = models.build_minara_catalog(self.cfg.get("accounts") or [self.cfg])
        with models._CATALOG_LOCK:
            target = self.cfg.get("_minara_models")
            if not isinstance(target, dict):
                self.cfg["_minara_models"] = routes
                return
            for key, value in routes.items():
                target[key] = value
            for key in [key for key in target if key not in routes]:
                target.pop(key, None)

    def _report(self, summary):
        for upstream in summary["added"]:
            name = minara_catalog.public_id(upstream)
            journal.emit("catalog.model_added", "Minara: новая модель %s измерена и опубликована" % name, "success",
                         upstream="minara", model=name, upstream_model=upstream)
            console_log.write("[catalog] Minara: new model %s measured and published" % name)
        for upstream, status in summary["failed"]:
            journal.emit("catalog.model_unpublished", "Minara: модель %s не опубликована (%s)" % (upstream, status),
                         "warn", upstream="minara", upstream_model=upstream, status=status)
        for upstream in summary["gone"]:
            name = minara_catalog.public_id(upstream)
            journal.emit("catalog.model_gone", "Minara больше не перечисляет %s: модель снята с публикации" % name, "warn",
                         upstream="minara", model=name, upstream_model=upstream)
            console_log.write("[catalog] Minara: %s is gone from Minara's list, unpublished" % name)
        for upstream in summary["releveled"]:
            name = minara_catalog.public_id(upstream)
            entry = minara_catalog.snapshot().get(upstream) or {}
            journal.emit("catalog.model_releveled", "Minara: уровни effort %s по данным провайдера: %s" % (
                name, ", ".join(entry.get("reasoning_effort_levels") or ()) or "нет"), "info", upstream="minara",
                model=name, upstream_model=upstream, levels=entry.get("reasoning_effort_levels"))
        for upstream in summary["repriced"]:
            journal.emit("catalog.model_repriced", "Minara: новая цена %s" % upstream, "info", upstream="minara",
                         upstream_model=upstream)
        for upstream, reason in summary.get("unpriced", {}).items():
            name = minara_catalog.public_id(upstream)
            journal.emit("catalog.price_unreadable", "Minara: цену %s релей не читает (%s): ходы этой модели идут без "
                         "цены" % (name, reason), "warn", upstream="minara", model=name, upstream_model=upstream,
                         reason=reason)
            console_log.write("[catalog] Minara: цену %s релей не читает (%s): ходы этой модели идут без цены" % (
                name, reason))
        for upstream, reason in summary.get("skipped_reasons", {}).items():
            # A price the relay cannot read waits for a fix of the relay; a measurement over the budget, for the next look.
            journal.emit("catalog.model_skipped", "Minara: новая модель %s не измерена: %s" % (upstream, reason),
                         "warn" if upstream in summary.get("price_unreadable", ()) else "info", upstream="minara",
                         upstream_model=upstream, reason=reason)
            console_log.write("[catalog] Minara: новая модель %s не измерена: %s" % (upstream, reason))
        skipped = "; ".join("%s: %s" % item for item in summary.get("skipped_reasons", {}).items())
        journal.emit("catalog.refresh", "Каталог Minara проверен: %d моделей; новых %d, новых цен %d, новых уровней %d, "
                     "снято %d, не прошли %d, отложено %d%s; замеры стоили %.2f кредита" % (
                         summary["listed"], len(summary["added"]), len(summary["repriced"]),
                         len(summary.get("releveled", ())), len(summary["gone"]),
                         len(summary["failed"]), len(summary["skipped"]), " (%s)" % skipped if skipped else "",
                         summary["spent_credits"]),
                     "info", upstream="minara", **{key: summary.get(key) for key in (
                         "listed", "added", "repriced", "releveled", "gone", "failed", "skipped", "skipped_reasons",
                         "unpriced", "spent_credits")})
