"""Which models the Retool plans of the relay's accounts include, as the relay found out: retool_plans.auto.json beside
config.json.

An account whose plan lacks a model answers Retool's HTTP 402 "This model is not available for your plan." (free, 0.4-1.7
s). AccountDispatcher holds the pair (account, model) back in memory at once; a refusal that repeats is a fact of the plan,
written here, so that a restart does not learn every refusing pair again from clients' requests (74 refusals after each of
two restarts on 2026-11-18), and the pair is asked again by a background probe after 12-24 h, never by a client's request.
A turn that a pair served is written too (at most once an hour per pair): the pool asks accounts that served a model first.

The file holds no secret: an account is named by accounts.cfg_identity (its account_id or page UUID, what the stored
conversations name it by) and a tag for the reader. Writes are atomic (a temporary file in the same folder, then
os.replace) and merge with what is on disk under the file's lock: two relays on one config.json keep each other's facts,
and per pair the newer record wins. A damaged file is set aside as .bad and the relay starts as if it had none.
"""
import datetime
import json
import os
import pathlib
import tempfile
import time

import journal

VERSION = 1
AUTO_NAME = "retool_plans.auto.json"
FORGET_ACCOUNT = 7 * 86400.0  # what is known of an account that left config.json is kept a week, then dropped


def overlay_path(config_path):
    """Where the plans file of the relay whose config.json is `config_path` lives (None without a config file)."""
    return pathlib.Path(config_path).resolve().with_name(AUTO_NAME) if config_path else None


def stamp(epoch):
    """A moment as the file keeps it: local wall time with its offset, to the second (UTC where the platform has no local
    time for it)."""
    moment = datetime.datetime.fromtimestamp(float(epoch), tz=datetime.timezone.utc)
    try:
        moment = moment.astimezone()
    except (OSError, OverflowError, ValueError):
        pass
    return moment.isoformat(timespec="seconds")


def epoch(text):
    """The epoch seconds of a moment the file keeps, None for anything else."""
    try:
        return datetime.datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _newest(record):
    return epoch((record or {}).get("at")) or 0.0


class PlanStore:
    """The plans file of one relay; every method may be called from any thread."""

    def __init__(self, path):
        self.path = os.fspath(path)

    def read(self):
        """{identity: {"tag", "limit", "models": {model: record}}}; {} when there is no file. A file that cannot be read as
        one is renamed to .bad (journal warn) and the relay goes on without it."""
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            accounts = data.get("accounts") if isinstance(data, dict) else None
            if not isinstance(accounts, dict):
                raise ValueError("no accounts")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            self._set_aside(error)
            return {}
        return {identity: row for identity, row in accounts.items()
                if isinstance(identity, str) and isinstance(row, dict) and isinstance(row.get("models"), dict)}

    def _set_aside(self, error):
        try:
            os.replace(self.path, self.path + ".bad")
        except OSError:
            pass
        journal.emit("account.plans_unreadable", "Файл тарифов Retool не читается (%s) — отложен как .bad, отказы "
                     "тарифа relay узнает заново" % type(error).__name__, "warn", path=os.path.basename(self.path))

    def merge(self, updates, known=None, now=None):
        """Write `updates` ({identity: {"tag", "limit", "models": {model: record}}}) into the file: under its lock, over
        what is on disk, the newer record of each pair winning (record["at"]). The records of an account that is not in
        `known` (the identities of config.json) and has not changed for FORGET_ACCOUNT are dropped. Returns the merged
        accounts."""
        import config  # local: the account module that imports this one is imported by config's own imports

        now = time.time() if now is None else now
        with config.storage_lock(self.path, timeout=10, best_effort=True):
            accounts = self.read()
            for identity, row in updates.items():
                kept = accounts.setdefault(identity, {"models": {}})
                for key in ("tag", "limit"):
                    if row.get(key) is not None:
                        kept[key] = row[key]
                for model, record in (row.get("models") or {}).items():
                    if _newest(record) >= _newest(kept["models"].get(model)):
                        kept["models"][model] = record
            if known is not None:
                for identity in [identity for identity, row in accounts.items() if identity not in known
                                 and now - max(map(_newest, row["models"].values()), default=0.0) > FORGET_ACCOUNT]:
                    del accounts[identity]
            self._write({"version": VERSION, "updated_at": stamp(now), "accounts": accounts})
        return accounts

    def _write(self, state):
        folder = os.path.dirname(os.path.abspath(self.path))
        handle, temporary = tempfile.mkstemp(prefix=".retool-plans-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
