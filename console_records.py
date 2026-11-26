"""JSONL framing shared by console output and the journal's failure path."""
import datetime
import json
import os
import sys
import threading


IDENTITY = ("request_id", "turn_id", "conversation", "client", "chat_title", "agent", "client_task",
            "request_purpose", "route", "upstream", "account", "model", "upstream_model", "effective_effort")
_LOCK = threading.Lock()


def enabled():
    return os.environ.get("RELAY_LOG_FORMAT", "").lower() == "jsonl"


def write(kind, level, fields, *, when=None, file=None):
    """Write one already redacted event. Controls and newlines stay escaped inside the record."""
    try:
        now = (datetime.datetime.now().astimezone() if when is None
               else datetime.datetime.fromtimestamp(when, datetime.timezone.utc).astimezone())
        record = {"ts": now.isoformat(timespec="milliseconds"), "kind": kind, "level": level}
        record.update((key, value) for key, value in fields.items() if value is not None and key not in record)
        line = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        stream = sys.stdout if file is None else file
        with _LOCK:
            stream.write(line + "\n")
            stream.flush()
    except (OSError, TypeError, ValueError, OverflowError):
        pass  # a disconnected supervisor or unusable diagnostic must not stop requests
