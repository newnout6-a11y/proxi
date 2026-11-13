"""The relay keeps its Retool catalog current by itself: a model Retool adds is found, asked one question and published.

models.CATALOG is audited by hand, and Retool adds models between two audits (Claude Fable 5.1 and 5.2 were in Retool's
own registry and not in the relay's on 2026-11-13). Every `retool_catalog_refresh_hours` (6 by default, 0 switches it
off), first three minutes after the start, the relay:

- reads the login page of one enabled Retool account (public, no cookie is sent) for the frontend's entry script, and
  that script for the chunk that holds Retool's model registry (retool_catalog.parse_registry; both are public static
  files on retool-edge.com). An entry script it has already read is not read again: the registry it held is kept;
- takes the registry's chat models the relay does not know yet (Anthropic, OpenAI and Retool-fast; Google needs a BYOK
  resource no account has) and asks each one a short question through an account with the provider's resource, at
  most `retool_catalog_probes` a look (10 by default; one question costs a few hundredths of a credit). A model that
  answers is published with the registry's limits and levels, at once and without a restart (/v1/models, /codex/models
  and the routes see it); one that does not is asked again on a later look, at most MAX_ATTEMPTS times a day;
- says what it found: catalog.retool_model_added, catalog.retool_model_failed and catalog.retool_refresh in the journal,
  a [catalog] line on the console.

What it learns goes to retool_models.auto.json beside config.json (retool_catalog.apply_overlay puts it into the catalog
at every start). The client files (OpenCode, Claude Desktop) list a new model after their sync script, as for Minara.
"""
import datetime
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

import auth
import console_log
import direct
import journal
import models
import retool_catalog
from accounts import account_tag, enabled

FIRST_DELAY = 180.0
# A model that did not answer is asked again after a day, then three, then a week: a route Retool refuses (gpt-5.3-codex
# needs Responses) would otherwise cost a question and a warning on every look.
RETRY_DAYS = (1, 3, 7)
PAGE_BYTES = 2 * 1024 * 1024
SCRIPT_BYTES = 16 * 1024 * 1024
EDGE = "https://retool-edge.com/assets_vjs/"
_ENTRY = re.compile(r"assets_vjs/(main-[A-Za-z0-9_-]+\.js)")
_CHUNK = re.compile(r"assets_vjs/([A-Za-z0-9_.-]+\.js)")
_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) retool-relay-catalog"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _origin(base):
    match = re.match(r"(https?://[^/]+)", str(base or ""))
    return match[1] if match else None


def fetch(url, limit, timeout=30.0):
    """The text at `url` (a public page or script, no cookie), at most `limit` bytes; OSError when it cannot be read."""
    request = urllib.request.Request(url, headers={"User-Agent": _AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError("HTTP %s" % response.status)
        data = response.read(limit + 1)
    if len(data) > limit:
        raise OSError("larger than %d bytes" % limit)
    return data.decode("utf-8", "replace")


def probe(backend, entry, timeout=90.0):
    """Ask `entry`'s model one short question through `backend` (a Retool account) on the relay's own route: a dict with
    `ok` (it answered with the expected word), `http`, `error`, `credits` (what Retool reported), `thinking` (thinking
    events seen) and `seconds`. A model that only thinks within the output limit counts as not answering."""
    cfg = backend.cfg
    nonce = uuid.uuid4().hex[:8].upper()
    word = "READY-" + nonce
    head = "You are answering a one-line availability check. Reply with exactly the word %s and nothing else." % word
    provider = {"model": entry["id"], "providerResourceName": models.provider_resource(cfg, entry)}
    if "low" in (entry.get("reasoning_effort_levels") or ()):
        provider["reasoningEffort"] = "low"  # a small answer: thinking at length would eat the output limit
    body = direct.build_body(cfg, provider, head, "Reply now with the word.", [], None, 2048)
    headers = auth.upstream_headers(cfg)
    headers["content-type"] = "application/json"
    request = urllib.request.Request(str(cfg["base"]).rstrip("/") + direct.ROUTE,
                                     data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    row = {"ok": False, "http": None, "error": None, "credits": None, "thinking": 0, "seconds": None}
    text, ended, started = [], False, time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            row["http"] = response.status
            for index, raw in enumerate(response):
                if index >= 20000 or time.monotonic() - started > timeout:
                    row["error"] = "the answer took too long"
                    break
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("event") or "")
                block = str(event.get("type") or "")
                if kind == "content_block_delta" and block == "text":
                    text.append(str(event.get("partialText") or ""))
                elif kind == "content_block_delta" and block == "thinking":
                    row["thinking"] += 1
                elif kind == "metadata" and isinstance(event.get("usage"), dict):
                    used = console_log.number(event["usage"].get("creditsUsed"))
                    row["credits"] = used if used is not None else row["credits"]
                elif kind == "error":
                    row["error"] = str(event.get("message") or event.get("error") or "upstream error")[:300]
                elif kind == "end_of_stream" or block == "end_of_stream":
                    ended = True
                    break
    except urllib.error.HTTPError as error:
        row["http"] = error.code
        row["error"] = error.read(2000).decode("utf-8", "replace")[:300]
        error.close()
    except (OSError, ValueError) as error:
        row["error"] = type(error).__name__
    row["seconds"] = round(time.monotonic() - started, 2)
    answer = "".join(text).strip()
    row["ok"] = row["http"] == 200 and ended and not row["error"] and word in answer
    if not row["ok"] and not row["error"]:
        row["error"] = "no answer" if not answer else "unexpected answer: %s" % answer[:80]
    # Never a credential in what is kept or said: the answer of an error page may quote the request.
    row["error"] = journal.scrub(row["error"]) if row["error"] else None
    return row


class CatalogRefresher:
    def __init__(self, cfg, dispatcher=None, backends=None, fetcher=None, prober=None):
        self.cfg = cfg
        pool = backends if backends is not None else list(getattr(dispatcher, "backends", None) or [])
        self.backends = [b for b in pool if (getattr(b, "cfg", None) or {}).get("upstream", "retool") == "retool"]
        self.path = cfg.get("_retool_catalog_file")
        self.hours = float(cfg.get("retool_catalog_refresh_hours", 6) or 0)
        self.probes = int(cfg.get("retool_catalog_probes", 10) or 0)
        self._fetch = fetcher or fetch
        self._probe = prober or probe
        self._stop = threading.Event()
        self._thread = None
        self.last = None
        self.next_at = None

    # -- lifecycle ---------------------------------------------------------------------------------------------------
    def start(self):
        if not self.backends or self.hours <= 0 or not self.path or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._loop, name="retool-catalog", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _loop(self):
        delay = FIRST_DELAY
        while True:
            self.next_at = time.time() + delay
            if self._stop.wait(delay):
                return
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - a failed look waits for the next one; the relay goes on
                journal.emit("catalog.retool_refresh_failed", "Проверка каталога Retool не удалась: %s" % type(exc).__name__,
                             "warn", upstream="retool", error=type(exc).__name__)
            delay = self.hours * 3600.0

    # -- one look ----------------------------------------------------------------------------------------------------
    def _live(self):
        return [b for b in self.backends if enabled(b) and not getattr(b, "unavailable", None)]

    def _registry(self, state, summary):
        """The registry's chat models: read again only when the frontend's entry script changed. Updates `state`."""
        for backend in self._live():
            origin = _origin(backend.cfg.get("base"))
            if not origin:
                continue
            try:
                page = self._fetch(origin + "/", PAGE_BYTES)
            except OSError as error:
                summary["error"] = "login page: %s" % error
                continue
            found = _ENTRY.search(page)
            if not found:
                summary["error"] = "no entry script on the login page"
                continue
            entry = found[1]
            summary["entry_script"] = entry
            if entry == state.get("entry_script") and isinstance(state.get("registry"), dict):
                summary.pop("error", None)
                return state["registry"]
            script = self._fetch(EDGE + entry, SCRIPT_BYTES)
            chunks = list(dict.fromkeys(_CHUNK.findall(script)))
            if state.get("registry_chunk") in chunks:
                chunks.remove(state["registry_chunk"])
                chunks.insert(0, state["registry_chunk"])
            for chunk in [entry] + [name for name in chunks if name != entry]:
                text = script if chunk == entry else self._fetch(EDGE + chunk, SCRIPT_BYTES)
                registry = retool_catalog.parse_registry(text)
                if registry is not None:
                    state.update(entry_script=entry, registry_chunk=chunk, registry=retool_catalog.chat_models(registry),
                                 registry_read_at=summary["at"])
                    summary.pop("error", None)
                    return state["registry"]
            summary["error"] = "no model registry in the frontend's %d scripts" % (len(chunks) + 1)
            return None
        summary.setdefault("error", "no enabled Retool account")
        return None

    def run_once(self):
        """One look at Retool's registry; a summary of what changed (also kept as self.last)."""
        summary = {"at": _now(), "entry_script": None, "registry": 0, "new": [], "added": [], "failed": [],
                   "skipped": {}, "spent_credits": 0.0}
        state = retool_catalog.read(self.path)
        found = self._registry(state, summary)
        if found is None:
            self._report(summary)
            self.last = summary
            return summary
        summary["registry"] = len(found)
        published = state.setdefault("models", {})
        records = state.setdefault("measurements", {})
        today = summary["at"][:10]
        budget = self.probes
        for ident, model in sorted(retool_catalog.new_models(found, published).items()):
            summary["new"].append(ident)
            key = models.PROVIDER_RESOURCE_KEY.get(model["provider"])
            accounts = [b for b in self._live() if key and b.cfg.get(key)]
            if model["provider"] == "google" and accounts and all(
                    b.cfg.get(key) == "__managed__" for b in accounts):
                accounts = []  # managed Google is no route (models.route_block_reason)
            if not accounts:
                summary["skipped"][ident] = "no account has a %s resource" % model["provider"]
                continue
            record = records.setdefault(ident, {"failures": 0})
            if record.get("retry_after") and record["retry_after"] > summary["at"]:
                summary["skipped"][ident] = "asked again after %s" % record["retry_after"]
                continue
            if budget <= 0:
                summary["skipped"][ident] = "waits for the next look (retool_catalog_probes)"
                continue
            if self._stop.is_set():
                break
            budget -= 1
            entry = dict(model, source=EDGE + str(state.get("registry_chunk") or ""), checked_at=today)
            backend = accounts[0]
            result = self._probe(backend, entry)
            record.update(last=summary["at"], account=account_tag(backend),
                          result={key: result.get(key) for key in ("ok", "http", "error", "credits", "thinking",
                                                                   "seconds")})
            credits = result.get("credits")
            if credits:
                summary["spent_credits"] = round(summary["spent_credits"] + credits, 5)
                manager = getattr(backend, "credits", None)
                if manager is not None and hasattr(manager, "record_turn"):
                    manager.record_turn("retool-catalog-%s" % uuid.uuid4().hex, credits)
            if result.get("ok"):
                published[ident] = dict(entry, published_at=summary["at"], thinking=bool(result.get("thinking")))
                records.pop(ident, None)
                summary["added"].append(ident)
            else:
                record["failures"] = int(record.get("failures") or 0) + 1
                wait = RETRY_DAYS[min(record["failures"], len(RETRY_DAYS)) - 1]
                record["retry_after"] = (datetime.datetime.now(datetime.timezone.utc)
                                         + datetime.timedelta(days=wait)).isoformat(timespec="seconds")
                summary["failed"].append([ident, result.get("error") or "HTTP %s" % result.get("http")])
        state["refreshed_at"] = summary["at"]
        if self.path:
            retool_catalog.write(self.path, state)
            retool_catalog.apply_overlay(self.path)
        self._report(summary)
        self.last = summary
        return summary

    def _report(self, summary):
        for ident in summary["added"]:
            journal.emit("catalog.retool_model_added", "Retool: новая модель %s проверена и опубликована" % ident,
                         "success", upstream="retool", model=ident)
            console_log.write("[catalog] Retool: новая модель %s проверена и опубликована" % ident)
        for ident, reason in summary["failed"]:
            journal.emit("catalog.retool_model_failed", "Retool: модель %s не ответила на проверку (%s)" % (ident, reason),
                         "warn", upstream="retool", model=ident, reason=reason)
            console_log.write("[catalog] Retool: модель %s не ответила на проверку (%s)" % (ident, reason))
        if summary.get("error"):
            journal.emit("catalog.retool_refresh_failed", "Каталог Retool не прочитан: %s" % summary["error"], "warn",
                         upstream="retool", error=summary["error"])
            return
        skipped = "; ".join("%s: %s" % item for item in summary["skipped"].items())
        journal.emit("catalog.retool_refresh", "Каталог Retool проверен: %d моделей в реестре, новых %d, опубликовано %d, "
                     "не ответили %d, отложено %d%s; проверки стоили %.4f кредита" % (
                         summary["registry"], len(summary["new"]), len(summary["added"]), len(summary["failed"]),
                         len(summary["skipped"]), " (%s)" % skipped if skipped else "", summary["spent_credits"]),
                     "info", upstream="retool", **{key: summary[key] for key in (
                         "entry_script", "registry", "new", "added", "failed", "skipped", "spent_credits")})
