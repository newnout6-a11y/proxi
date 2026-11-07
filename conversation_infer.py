"""The conversation of a request that carries no identity: the one whose request its history extends.

DeepSeek Harness (and any client like it) sends no session header, so every request used to be a conversation of its own:
a throwaway session, `native_history.cold` on 488 of 490 requests (Harness test run of 2026-11-06), the signed thinking
of earlier steps dropped each time, no keepwarm. Such a client does re-send its whole history on every request, and
the history of a request is the history of the previous one plus what came after. So a request whose messages begin with
all the messages of a request the relay has seen (same model, same system prompt) belongs to that request's
conversation, and takes its key.

The check is exact, never a guess: a hash over the model, the system text and the digest of every message
(validate._message_fingerprints, which every parse computes already). Two requests share a key only when one of them
carries the whole of the other's history, so the only conversations that can meet in one key are one conversation and its
forks (the same prefix, then two continuations), and native_history keeps each continuation on a branch of its own and
resumes one only when the client's messages are exactly the branch's. A conversation whose earlier messages the client
edits (a pruned tool result, a regenerated answer) simply starts a key of its own, which is what every request did before.
"""
import collections
import hashlib
import threading
import time

# How long and how many histories are remembered: an entry is a length, a digest, a key and a time.
TTL = 24 * 3600
MAX_ENTRIES = 20000


class ConversationIndex:
    """(model, system, message digests) -> the key of the conversation the request continues, or a new one."""

    def __init__(self, ttl=TTL, max_entries=MAX_ENTRIES, clock=time.time):
        self._ttl, self._max, self._clock = ttl, max_entries, clock
        self._lock = threading.Lock()
        # (message count, digest of model + system + that many messages) -> [key, last seen], oldest use first
        self._chains = collections.OrderedDict()
        self._lengths = collections.Counter()

    @staticmethod
    def _hasher(model, system):
        hasher = hashlib.sha256()
        hasher.update(str(model or "").encode("utf-8", "surrogatepass"))
        hasher.update(b"\x00")
        hasher.update(hashlib.sha256(str(system or "").encode("utf-8", "surrogatepass")).digest())
        return hasher

    def identify(self, model, system, digests, stored=None):
        """(key, how) for a request, and remember its history for the next one.

        `digests` is the tuple of per-message digests of the request (bytes). how is "extended" when the request carries
        the whole history of a request seen before (the key is that request's), "identical" when it is that history
        exactly (a retry), "stored" when no request seen in this process is a prefix of it but a conversation kept on disk
        is (`stored(key)` says whether a key has a stored state: the first request after a restart), "new" otherwise (a key
        of its own, made from this history).

        The index lives in memory, so after a restart the first request of a conversation used to get a new key from its
        whole history, and the signed history stored under the old key was never read again: the request went cold, its
        prefix was written to the cache again, and the old file was orphaned."""
        now = self._clock()
        hasher = self._hasher(model, system)
        total = len(digests)
        with self._lock:
            self._expire(now)
            seen, how, taken = None, "new", 0
            for length in sorted(n for n in self._lengths if n <= total):
                while taken < length:
                    hasher.update(digests[taken])
                    taken += 1
                chain = (length, hasher.copy().digest())
                if chain in self._chains:
                    seen, how = chain, "identical" if length == total else "extended"
            while taken < total:
                hasher.update(digests[taken])
                taken += 1
            full = (total, hasher.digest())
            if seen is not None:
                key = self._touch(seen, now)
            else:
                key = "inferred:" + full[1].hex()[:20]
                found = self._stored_prefix(model, system, digests, stored) if stored is not None else None
                if found is not None:
                    key, how = found, "stored"
            if full != seen:
                self._add(full, key, now)
        return key, how

    def _stored_prefix(self, model, system, digests, stored):
        """The key of the longest history prefix (shorter than the request) that a stored conversation was started with."""
        hasher, keys = self._hasher(model, system), []
        for digest in digests[:-1]:
            hasher.update(digest)
            keys.append("inferred:" + hasher.copy().digest().hex()[:20])
        for key in reversed(keys):
            try:
                if stored(key):
                    return key
            except Exception:  # noqa: BLE001 - a store that cannot answer leaves the request a new conversation
                return None
        return None

    def _touch(self, chain, now):
        entry = self._chains[chain]
        entry[1] = now
        self._chains.move_to_end(chain)
        return entry[0]

    def _add(self, chain, key, now):
        self._chains[chain] = [key, now]
        self._lengths[chain[0]] += 1
        while len(self._chains) > self._max:
            self._drop(next(iter(self._chains)))

    def _drop(self, chain):
        self._chains.pop(chain, None)
        self._lengths[chain[0]] -= 1
        if self._lengths[chain[0]] <= 0:
            del self._lengths[chain[0]]

    def _expire(self, now):
        while self._chains:
            chain = next(iter(self._chains))
            if now - self._chains[chain][1] < self._ttl:
                break
            self._drop(chain)

    def __len__(self):
        with self._lock:
            return len(self._chains)
