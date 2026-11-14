"""Conversation state that survives a relay restart: signed history, cache marks and the account binding.

A conversation's next request is only byte-identical to the previous prefix, and so only hits Retool's prompt
cache, while the signed assistant/thinking blocks of earlier turns go back upstream unchanged, on the account
whose cache holds them. All of that lives in the memory of a session, so a restart silently turned the next
turn of every long conversation into a full prefix write. This module keeps it in a private directory:

* one file per conversation key: a text header (format version, key, account, size and counts) and a zlib
  stream of the state itself (the `native_history.History` branches, `_cached_turn_ends`, the last cache key,
  the prefix size and the time of the last cache touch);
* written after the turn, off the request path, by one background thread, as a temporary file and an atomic
  `os.replace`, so a kill at any moment leaves the previous complete file;
* bounded by age, number of conversations, size of a file and total size;
* read back lazily, when the pool creates a session for the key, and only onto the account it belongs to.

What is stored is only what the relay itself received from upstream (assistant blocks with their signatures)
and the canonical echo of the client's messages used to match them. A signature a client sends back is still
never trusted. The files hold the conversation, so they live in the state directory and never reach the
journal: events carry tags, counts and reasons only.
"""
import asyncio
import collections
import hashlib
import json
import math
import os
import threading
import time
import uuid
import zlib

import console_log
import journal
import native_history
from accounts import account_tag, cfg_identity

FORMAT = 1
MAGIC = b"PROXI-CONVERSATION-STATE\n"
SUFFIX = ".state"
TEMP_PREFIX = ".write-"
TEMP_TTL = 600.0  # an interrupted write older than this is garbage
HEADER_LIMIT = 64 * 1024
MAX_KEY_CHARS = 2048  # a longer conversation key is not stored (it is a client's header value)
MAX_BRANCHES = 8  # what native_history.History keeps
BATCH_BYTES = 256 * 1024  # JSON text handed to zlib at once
RAW_FACTOR = 4  # a state may expand to this many times the file limit
REPLACE_ATTEMPTS = 4
RESTORE_TIMEOUT = 15.0  # seconds a request waits for a stored state to be read
TRANSIENT = frozenset({"io_error", "internal"})  # save failures that may pass: retried without waiting for a change

DEFAULT_TTL = 24 * 3600
DEFAULT_CONVERSATIONS = 200
DEFAULT_FILE_BYTES = 32 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 512 * 1024 * 1024

_REASONS = {
    "corrupt": "файл повреждён",
    "format": "другая версия формата",
    "key_mismatch": "файл принадлежит другому диалогу",
    "expired": "срок хранения истёк",
    "limit_conversations": "превышен предел числа диалогов",
    "limit_bytes": "превышен предел общего объёма",
    "too_large": "состояние больше предела размера файла",
    "emptied": "в диалоге не осталось состояния",
}
# A file somebody else may have written, or a damaged one, is a surprise; an old one is routine housekeeping.
_SURPRISES = frozenset({"corrupt", "format", "key_mismatch"})

Meta = collections.namedtuple("Meta", "key_id tag account saved_at size branches signatures messages")
State = collections.namedtuple("State", "branches turn_ends cache_key prefix_tokens warm_at")
Loaded = collections.namedtuple("Loaded", "header state size")


class StateError(Exception):
    """A stored file that cannot be used; `reason` is one of _REASONS."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class TooLarge(Exception):
    """The state does not fit the configured limits."""


class Capture:
    """Everything a stored copy needs, taken from a live session in one step.

    The branches are immutable once recorded, so a background thread may serialize them while the session
    goes on; `mark` says whether the session has moved since the last copy."""
    __slots__ = ("key", "key_id", "account", "branches", "turn_ends", "cache_key", "prefix_tokens",
                 "warm_at", "mark")

    def __init__(self, key, key_id, account, branches, turn_ends, cache_key, prefix_tokens, warm_at, mark):
        self.key, self.key_id, self.account = key, key_id, account
        self.branches, self.turn_ends, self.cache_key = branches, turn_ends, cache_key
        self.prefix_tokens, self.warm_at, self.mark = prefix_tokens, warm_at, mark

    @property
    def empty(self):
        """Nothing left worth keeping: the stored copy, if there is one, is to be removed."""
        return not self.branches and not self.cache_key


# -- keys ----------------------------------------------------------------------------------------------------
def key_json(key):
    """A pool key (a string, or a tuple of them for a scoped conversation) as JSON."""
    if key is None or isinstance(key, str):
        return key
    if isinstance(key, (tuple, list)):
        return [key_json(item) for item in key]
    raise TypeError("a conversation key of type %s cannot be stored" % type(key).__name__)


def key_value(value):
    """The pool key a stored key_json stands for."""
    return tuple(key_value(item) for item in value) if isinstance(value, list) else value


def key_digest(key):
    """Stable file name of a key: it never contains the key itself."""
    text = json.dumps(key_json(key), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:32]


# -- capture and apply -----------------------------------------------------------------------------------------
def capture(key, session):
    """The state of a pool session, or None when the session has no account identity to store it under.

    An empty capture (no branch, no cache key) is how a session says that what was stored for it is gone."""
    identity = cfg_identity(getattr(getattr(session, "backend", None), "cfg", None))
    history = getattr(session, "_native_history", None)
    if not identity or history is None:
        return None
    try:
        key_id = key_digest(key)
    except TypeError:
        return None
    if len(json.dumps(key_json(key), ensure_ascii=True)) > MAX_KEY_CHARS:
        return None  # the key travels in the file's header line; a client-chosen giant one is simply not stored
    branches = tuple(history.branches)
    cache_key = getattr(session, "_last_cache_key", None)
    turn_ends = tuple(getattr(session, "_cached_turn_ends", None) or ())
    mark = (id(history), getattr(history, "revision", 0), cache_key, turn_ends, identity)
    return Capture(key, key_id, identity, branches, turn_ends, cache_key,
                   int(getattr(session, "_last_prefix_tokens", 0) or 0),
                   float(getattr(session, "_warm_at", 0) or 0), mark)


def apply(session, state):
    """Put a stored state into a fresh session; the History object stays, so branches sharing it see it too."""
    session._native_history.branches = list(state.branches)[-MAX_BRANCHES:]
    session._cached_turn_ends = list(state.turn_ends)
    session._last_cache_key = state.cache_key
    session._last_prefix_tokens = state.prefix_tokens
    session._warm_at = state.warm_at


def refuse(session, snapshot, status):
    """Upstream refused a body built from a branch that was read back from disk: forget that branch.

    A branch recorded live is left alone (the live session has always kept it). One that came off the disk is
    not replayed again, nor stored again, so a refusal does not repeat after every restart until the file
    expires: the next try takes the unsigned path."""
    if not getattr(snapshot, "restored", False):
        return
    try:
        history = session._native_history
        history.branches = [branch for branch in history.branches if branch is not snapshot]
        history.revision += 1
        journal.emit("conversation_state.dropped", "Восстановленная ветка отклонена upstream (HTTP %s) и забыта" % status,
                     "warn", conversation=journal.tag(getattr(session, "_native_conversation_key", None)),
                     account=account_tag(getattr(session, "backend", None)), reason="rejected", upstream_status=status)
    except Exception:  # noqa: BLE001 - it runs while the refusal is being reported and must never replace it
        pass


async def restore(store, key, session):
    """Load the stored state of `key` into the session just created for it; True when it was applied.

    Never moves a signature to another account: the state goes only onto a session on the account it was
    recorded for. A damaged file is dropped by the store and the conversation starts the way it always did."""
    meta = store.peek(key)
    if meta is None:
        return False
    if meta.account != cfg_identity(session.backend.cfg):
        store.skip(meta, "account_changed")
        return False
    loop = asyncio.get_running_loop()
    # The default executor, not the relay's IO pool: that one holds workers blocked on upstream reads by design.
    # A disk that stalls costs the conversation its stored state, never the request its answer.
    loaded = await asyncio.wait_for(loop.run_in_executor(None, store.load, key), RESTORE_TIMEOUT)
    if loaded is None:
        return False
    apply(session, loaded.state)
    captured = capture(key, session)
    session._state_mark = captured.mark if captured is not None else None
    store.restored(meta, loaded)
    return True


# -- file format -----------------------------------------------------------------------------------------------
# One encoder for every call: it keeps no state between calls, so the writer thread may share it.
_dump = json.JSONEncoder(ensure_ascii=True, separators=(",", ":")).encode


def _pieces(capture_, branches):
    """The state as JSON text, a message at a time: no single call holds the interpreter for long."""
    yield '{"branches":['
    for index, snapshot in enumerate(branches):
        yield ("," if index else "") + '{"scope":' + _dump(snapshot.scope)
        for name in ("client", "messages"):
            yield ',"%s":[' % name
            for position, message in enumerate(getattr(snapshot, name)):
                yield ("," if position else "") + _dump(message)
            yield "]"
        yield ',"assistant":' + _dump(snapshot.assistant) + ',"echo":' + _dump(snapshot.echo) + "}"
    yield '],"turn_ends":' + _dump(list(capture_.turn_ends))
    yield ',"cache_key":' + _dump(capture_.cache_key)
    yield ',"prefix_tokens":' + _dump(capture_.prefix_tokens)
    yield ',"warm_at":' + _dump(capture_.warm_at) + "}"


def _compress(pieces, max_disk, max_raw):
    """The compressed state and the size of its JSON text; TooLarge past either limit."""
    deflater = zlib.compressobj(3)
    chunks, disk, raw, batch, waiting = [], 0, 0, [], 0
    for piece in pieces:
        data = piece.encode("ascii")
        raw += len(data)
        if raw > max_raw:
            raise TooLarge("raw")
        batch.append(data)
        waiting += len(data)
        if waiting >= BATCH_BYTES:
            chunks.append(deflater.compress(b"".join(batch)))
            batch, waiting = [], 0
            disk += len(chunks[-1])
            if disk > max_disk:
                raise TooLarge("disk")
    chunks.append(deflater.compress(b"".join(batch)) + deflater.flush())
    if disk + len(chunks[-1]) > max_disk:
        raise TooLarge("disk")
    return b"".join(chunks), raw


def _thinking_blocks(snapshot):
    count = 0
    for message in (*snapshot.messages, {"role": "assistant", "content": snapshot.assistant}):
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            count += sum(1 for block in content
                         if isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking"))
    return count


def encode(capture_, saved_at, max_file, max_raw):
    """(file bytes, header, branches kept). The oldest branches go first when the state does not fit."""
    branches = list(capture_.branches)
    while True:
        try:
            payload, raw = _compress(_pieces(capture_, branches), max_file, max_raw)
            header = {"format": FORMAT, "key": key_json(capture_.key), "account": capture_.account,
                      "saved_at": round(saved_at, 3), "raw": raw, "branches": len(branches),
                      "messages": len(branches[-1].messages) if branches else 0,
                      "signatures": sum(_thinking_blocks(item) for item in branches)}
            blob = MAGIC + _dump(header).encode("ascii") + b"\n" + payload
            if len(blob) > max_file:
                raise TooLarge("disk")
            return blob, header, len(branches)
        except TooLarge:
            if len(branches) <= 1:
                raise
            branches = branches[1:]


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _parse_header(line):
    try:
        header = json.loads(line)
    except (ValueError, RecursionError):
        raise StateError("corrupt") from None
    if not isinstance(header, dict):
        raise StateError("corrupt")
    if header.get("format") != FORMAT:
        raise StateError("format")
    account = header.get("account")
    counts = [header.get(name) for name in ("raw", "branches", "messages", "signatures")]
    if (not isinstance(account, str) or not account or len(account) > 256 or not _number(header.get("saved_at"))
            or any(type(item) is not int or item < 0 for item in counts)
            or not (header.get("key") is None or isinstance(header.get("key"), (str, list)))):
        raise StateError("corrupt")
    return header


def _snapshot(item):
    if not isinstance(item, dict):
        raise StateError("corrupt")
    scope, client, messages = item.get("scope"), item.get("client"), item.get("messages")
    assistant, echo = item.get("assistant"), item.get("echo")
    if not (isinstance(scope, str) and isinstance(client, list) and isinstance(messages, list)
            and isinstance(assistant, list) and isinstance(echo, dict)):
        raise StateError("corrupt")
    if not all(isinstance(message, dict) for message in (*client, *messages, *assistant)):
        raise StateError("corrupt")
    return native_history.Snapshot(scope, client, messages, assistant, echo, restored=True)


def _state(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("branches"), list):
        raise StateError("corrupt")
    if len(payload["branches"]) > MAX_BRANCHES:
        raise StateError("corrupt")
    branches = [_snapshot(item) for item in payload["branches"]]
    ends, cache_key = payload.get("turn_ends"), payload.get("cache_key")
    tokens, warm_at = payload.get("prefix_tokens"), payload.get("warm_at")
    if (not isinstance(ends, list) or any(type(item) is not int or item < 0 for item in ends)
            or not (cache_key is None or isinstance(cache_key, str) and len(cache_key) <= 256)
            or type(tokens) is not int or tokens < 0 or not _number(warm_at) or warm_at < 0):
        raise StateError("corrupt")
    return State(branches, ends, cache_key, tokens, float(warm_at))


def decode(blob, max_raw):
    """(header, State) of a stored file; StateError names why it cannot be used."""
    if not blob.startswith(MAGIC):
        raise StateError("corrupt" if MAGIC.startswith(blob) else "format")
    end = blob.find(b"\n", len(MAGIC), len(MAGIC) + HEADER_LIMIT)
    if end < 0:
        raise StateError("corrupt")
    header = _parse_header(blob[len(MAGIC):end])
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(blob[end + 1:], max_raw + 1)
    except zlib.error:
        raise StateError("corrupt") from None
    if len(raw) > max_raw:
        raise StateError("too_large")
    if not inflater.eof or inflater.unused_data:
        raise StateError("corrupt")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise StateError("corrupt") from None
    return header, _state(payload)


def _read_header(handle):
    """The header of an open stored file, without reading the state behind it."""
    first = handle.readline(len(MAGIC) + 1)
    if first != MAGIC:
        raise StateError("corrupt" if MAGIC.startswith(first) else "format")
    line = handle.readline(HEADER_LIMIT)
    if not line.endswith(b"\n"):
        raise StateError("corrupt")
    return _parse_header(line[:-1])


# -- the store -------------------------------------------------------------------------------------------------
class ConversationStore:
    """A private, bounded directory of conversation states, written by one background thread."""

    def __init__(self, directory, *, ttl=DEFAULT_TTL, max_conversations=DEFAULT_CONVERSATIONS,
                 max_file_bytes=DEFAULT_FILE_BYTES, max_total_bytes=DEFAULT_TOTAL_BYTES, clock=time.time):
        self.directory = os.path.abspath(os.fspath(directory))
        self.ttl = ttl
        self.max_conversations = max_conversations
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._clock = clock
        self._index = {}  # key_id -> Meta of the file on disk
        self._lock = threading.Lock()  # the index
        self._io = threading.Lock()  # replacing, reading and removing files
        self._cond = threading.Condition()  # the queue of states to write
        self._pending = collections.OrderedDict()
        self._failed = set()  # conversations whose last write failed for a reason that may pass
        self._reported = set()  # conversations whose permanent failure has been journaled since they last saved
        self._busy = False
        self._closed = False
        self._thread = None
        self.enabled = False
        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
        except OSError as exc:
            journal.emit("conversation_state.disabled",
                         "Состояние диалогов не сохраняется: каталог недоступен (%s)" % type(exc).__name__, "warn")
            console_log.write("[state] conversation state is off: %s is not usable (%s)" % (
                self.directory, type(exc).__name__))
            return
        self.enabled = True
        self.scan()

    # -- housekeeping ------------------------------------------------------------------------------------------
    def _path(self, key_id):
        return os.path.join(self.directory, key_id + SUFFIX)

    def _meta(self, key_id, header, size):
        key = key_value(header.get("key"))
        return Meta(key_id, journal.tag(key), header["account"], float(header["saved_at"]), size,
                    header["branches"], header["signatures"], header["messages"])

    def _announce_drop(self, meta, reason, tag=None):
        journal.emit("conversation_state.dropped", "Сохранённое состояние диалога удалено: %s" % _REASONS[reason],
                     "warn" if reason in _SURPRISES else "info",
                     conversation=tag if meta is None else meta.tag, reason=reason,
                     bytes=None if meta is None else meta.size,
                     account=None if meta is None else journal.tag(meta.account))

    def _unlink(self, path):
        try:
            with self._io:
                os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True

    def scan(self):
        """Index the files of the directory; remove what cannot be used and enforce the limits."""
        now = self._clock()
        try:
            entries = list(os.scandir(self.directory))
        except OSError:
            return
        found = 0
        for entry in entries:
            name = entry.name
            try:
                if name.startswith(TEMP_PREFIX):
                    if now - entry.stat().st_mtime > TEMP_TTL:
                        self._unlink(entry.path)
                    continue
                if not name.endswith(SUFFIX) or not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat().st_size
            except OSError:
                continue
            key_id = name[:-len(SUFFIX)]
            try:
                with open(entry.path, "rb") as handle:
                    header = _read_header(handle)
                if key_digest(key_value(header.get("key"))) != key_id:
                    raise StateError("key_mismatch")
                if size > self.max_file_bytes:
                    raise StateError("too_large")
                if now - header["saved_at"] > self.ttl:
                    raise StateError("expired")
            except StateError as error:
                self._announce_drop(None, error.reason)
                self._unlink(entry.path)
                continue
            except (OSError, TypeError):
                self._announce_drop(None, "corrupt")
                self._unlink(entry.path)
                continue
            with self._lock:
                self._index[key_id] = self._meta(key_id, header, size)
            found += 1
        self._enforce()
        with self._lock:
            files, total = len(self._index), sum(meta.size for meta in self._index.values())
        journal.emit("conversation_state.scan", "Сохранённые диалоги: %d, %d КиБ" % (files, total // 1024),
                     "info" if files else "debug", files=files, bytes=total, found=found)
        if files:
            console_log.write("[state] %d stored conversation(s), %d KiB in %s" % (files, total // 1024, self.directory))

    def _enforce(self, keep=None):
        """Remove expired states, then the oldest ones past the limits; never the one just written."""
        now = self._clock()
        victims = []
        with self._lock:
            ordered = sorted(self._index.values(), key=lambda meta: meta.saved_at)
            for meta in ordered:
                if meta.key_id != keep and now - meta.saved_at > self.ttl:
                    victims.append((meta, "expired"))
            gone = {meta.key_id for meta, _ in victims}
            alive = [meta for meta in ordered if meta.key_id not in gone]
            total = sum(meta.size for meta in alive)
            while alive and (len(alive) > self.max_conversations or total > self.max_total_bytes):
                victim = next((meta for meta in alive if meta.key_id != keep), None)
                if victim is None:
                    break
                reason = "limit_conversations" if len(alive) > self.max_conversations else "limit_bytes"
                alive.remove(victim)
                total -= victim.size
                victims.append((victim, reason))
            for meta, _ in victims:
                self._index.pop(meta.key_id, None)
        for meta, reason in victims:
            self._unlink(self._path(meta.key_id))
            self._announce_drop(meta, reason)

    def _forget(self, key_id, reason, tag=None, announce_missing=True):
        """Remove one file because it cannot be used (or can no longer match anything)."""
        with self._lock:
            meta = self._index.pop(key_id, None)
        removed = self._unlink(self._path(key_id))
        if meta is not None or (announce_missing and removed):
            self._announce_drop(meta, reason, tag)

    # -- reading -----------------------------------------------------------------------------------------------
    def peek(self, key):
        """What is stored for a key (account, age, size) without reading it; None when nothing usable is."""
        try:
            key_id = key_digest(key)
        except TypeError:
            return None
        with self._lock:
            meta = self._index.get(key_id)
        if meta is None or self._clock() - meta.saved_at > self.ttl:
            return None
        return meta

    def load(self, key):
        """Read, check and decode the state of a key (blocking: call it from an executor); None if unusable.

        A file that cannot be used is removed, so the next request does not trip over it again."""
        try:
            key_id = key_digest(key)
        except TypeError:
            return None
        with self._lock:
            meta = self._index.get(key_id)
        if meta is None:
            return None
        try:
            with self._io:
                with open(self._path(key_id), "rb") as handle:
                    blob = handle.read(self.max_file_bytes + 1)
        except FileNotFoundError:
            with self._lock:
                self._index.pop(key_id, None)
            return None
        except OSError as exc:
            journal.emit("conversation_state.load_failed", "Сохранённое состояние диалога не прочитано (%s)"
                         % type(exc).__name__, "warn", conversation=meta.tag, error=type(exc).__name__)
            return None
        try:
            if len(blob) > self.max_file_bytes:
                raise StateError("too_large")
            header, state = decode(blob, self.max_file_bytes * RAW_FACTOR)
            if key_value(header.get("key")) != key:
                raise StateError("key_mismatch")
            if self._clock() - header["saved_at"] > self.ttl:
                raise StateError("expired")
        except StateError as error:
            self._forget(key_id, error.reason, meta.tag)
            return None
        if header["account"] != meta.account:
            # Somebody (another relay on this directory) rewrote the file after it was indexed: the index is
            # stale, the file is fine. Learn it and let the next request judge it.
            with self._lock:
                self._index[key_id] = self._meta(key_id, header, len(blob))
            return None
        return Loaded(header, state, len(blob))

    # -- events ------------------------------------------------------------------------------------------------
    def restored(self, meta, loaded):
        """Report a state that was put into a session."""
        now = self._clock()
        age = round(max(0.0, now - loaded.header["saved_at"]))
        cache_age = round(max(0.0, now - loaded.state.warm_at)) if loaded.state.warm_at else None
        journal.emit("conversation_state.restored",
                     "Состояние диалога восстановлено после перезапуска: ветки %d, подписанных блоков %d"
                     % (loaded.header["branches"], loaded.header["signatures"]), "info",
                     conversation=meta.tag, account=journal.tag(meta.account), branches=loaded.header["branches"],
                     messages=loaded.header["messages"], signatures=loaded.header["signatures"],
                     turn_ends=len(loaded.state.turn_ends), bytes=loaded.size, age_seconds=age,
                     cache_age_seconds=cache_age)
        console_log.write("[state] conversation restored account=%s branches=%d signatures=%d age=%ds" % (
            journal.tag(meta.account), loaded.header["branches"], loaded.header["signatures"], age))

    def skip(self, meta, reason, **fields):
        """Report a stored state that was left unused (and kept)."""
        journal.emit("conversation_state.skipped", "Сохранённое состояние диалога не применено: %s" % reason,
                     "info", conversation=meta.tag, account=journal.tag(meta.account), reason=reason,
                     age_seconds=round(max(0.0, self._clock() - meta.saved_at)), **fields)

    # -- writing -----------------------------------------------------------------------------------------------
    def failed(self, key_id):
        """True when the last copy of a conversation could not be written (it is retried by the next change)."""
        with self._cond:
            return key_id in self._failed

    def submit(self, captured):
        """Queue a state for the writer; a newer state of the same conversation replaces a waiting one."""
        with self._cond:
            if self._closed or not self.enabled:
                return False
            self._pending[captured.key_id] = captured
            self._pending.move_to_end(captured.key_id)
            self._failed.discard(captured.key_id)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="conversation-state", daemon=True)
                self._thread.start()
            self._cond.notify_all()
        return True

    def flush(self, timeout=10.0):
        """Wait until everything submitted has been written; False when the time ran out."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    def close(self, timeout=5.0):
        """Stop accepting states, let the writer finish what is queued and stop it."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _run(self):
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait()
                if not self._pending:
                    return
                _, captured = self._pending.popitem(last=False)
                self._busy = True
            try:
                self._save(captured)
            except Exception as exc:  # noqa: BLE001 - the writer outlives any one conversation
                self._fail(captured, "internal", exc)
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def _fail(self, captured, reason, error=None, **fields):
        """Report a state that was not stored. A transient failure (the disk, a bug) is retried at the next
        opportunity; a permanent one (too big, cannot be serialized) is said once and left to the next change."""
        key_id = captured.key_id
        with self._cond:
            if reason in TRANSIENT:
                self._failed.add(key_id)
            elif key_id in self._reported:
                return
            else:
                self._reported.add(key_id)
        journal.emit("conversation_state.save_failed",
                     "Состояние диалога не сохранено: %s" % (reason if error is None else
                                                              "%s (%s)" % (reason, type(error).__name__)),
                     "warn", conversation=journal.tag(captured.key), account=journal.tag(captured.account),
                     reason=reason, error=None if error is None else type(error).__name__, **fields)

    def _save(self, captured):
        started = time.monotonic()
        if captured.empty:
            # The conversation has nothing left to remember (its only branch was refused upstream).
            self._forget(captured.key_id, "emptied", journal.tag(captured.key), announce_missing=False)
            return
        try:
            blob, header, kept = encode(captured, self._clock(), self.max_file_bytes,
                                        self.max_file_bytes * RAW_FACTOR)
        except TooLarge:
            # What was stored before describes an older turn: it can never match the conversation again.
            self._fail(captured, "too_large", limit=self.max_file_bytes)
            self._forget(captured.key_id, "too_large", journal.tag(captured.key), announce_missing=False)
            return
        except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
            self._fail(captured, "unserializable", exc)
            return
        try:
            self._write(captured.key_id, blob)
        except OSError as exc:
            self._fail(captured, "io_error", exc, errno=getattr(exc, "errno", None))
            return
        meta = self._meta(captured.key_id, header, len(blob))
        with self._cond:
            self._reported.discard(captured.key_id)
        with self._lock:
            self._index[captured.key_id] = meta
            files, total = len(self._index), sum(item.size for item in self._index.values())
        journal.emit("conversation_state.saved", "Состояние диалога сохранено: ветки %d, %d КиБ на диске"
                     % (kept, len(blob) // 1024), "debug", conversation=meta.tag, account=journal.tag(meta.account),
                     branches=kept, trimmed=len(captured.branches) - kept, messages=header["messages"],
                     signatures=header["signatures"], turn_ends=len(captured.turn_ends), bytes=len(blob),
                     raw_bytes=header["raw"], duration_ms=round((time.monotonic() - started) * 1000),
                     files=files, total_bytes=total)
        self._enforce(keep=captured.key_id)

    def _write(self, key_id, blob):
        temporary = os.path.join(self.directory, "%s%s-%s" % (TEMP_PREFIX, key_id, uuid.uuid4().hex[:8]))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            with os.fdopen(os.open(temporary, flags, 0o600), "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    with self._io:
                        os.replace(temporary, self._path(key_id))
                    return
                except PermissionError:
                    # Windows: a scanner or indexer may hold the target for a moment.
                    if attempt + 1 == REPLACE_ATTEMPTS:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def open_store(cfg):
    """The store the configuration asks for, or None: switched off, no directory, or one that cannot be used."""
    if not cfg.get("conversation_state", True):
        return None
    directory = cfg.get("conversation_state_dir")
    if not directory:
        return None

    def number(name, default):
        try:
            return max(1, int(cfg.get(name, default)))
        except (TypeError, ValueError, OverflowError):
            return default

    try:
        store = ConversationStore(
            directory, ttl=number("conversation_state_ttl", DEFAULT_TTL),
            max_conversations=number("conversation_state_max_conversations", DEFAULT_CONVERSATIONS),
            max_file_bytes=number("conversation_state_max_file_bytes", DEFAULT_FILE_BYTES),
            max_total_bytes=number("conversation_state_max_total_bytes", DEFAULT_TOTAL_BYTES))
    except Exception as exc:  # noqa: BLE001 - a store that cannot start must never keep the relay from starting
        journal.emit("conversation_state.disabled", "Состояние диалогов не сохраняется: хранилище не запустилось (%s)"
                     % type(exc).__name__, "warn")
        console_log.write("[state] conversation state is off: the store did not start (%s)" % type(exc).__name__)
        return None
    return store if store.enabled else None
