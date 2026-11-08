"""A stale read may invalidate its own state, never the newer state of a writer."""
import os

import pytest

import conversation_state as cs
from test_conversation_state import capture_of


@pytest.mark.parametrize("change", ["replace", "rewrite", "remove", "unchanged"])
def test_failed_old_decode_invalidates_only_the_file_it_read(tmp_path, monkeypatch, change):
    store = cs.ConversationStore(tmp_path, clock=lambda: 100)
    key = "invalidation-race"
    captured = capture_of(key)
    store._save(captured)
    path = tmp_path / (captured.key_id + cs.SUFFIX)
    good = path.read_bytes()
    # A valid header gets indexed; the payload is corrupt when the request later tries to restore it.
    broken = good[:good.index(b"\n") + 1] + b"bad compressed payload"
    path.write_bytes(broken)
    original = cs.decode

    def interleaved(blob, limit):
        assert blob == broken
        if change == "replace":
            # Exercise the real writer, including updating the index, not just a filesystem replacement.
            store._save(captured)
            assert original(path.read_bytes(), limit)
        elif change == "rewrite":
            # Also detect a same-inode rewrite (e.g. another process repairing the state).
            with store._io:
                path.write_bytes(good)
        elif change == "remove":
            with store._io:
                os.unlink(path)
        return original(blob, limit)

    monkeypatch.setattr(cs, "decode", interleaved)
    try:
        assert store.load(key) is None
        if change in ("replace", "rewrite"):
            assert path.read_bytes() == good
            assert store.peek(key) is not None
            monkeypatch.setattr(cs, "decode", original)
            assert store.load(key) is not None
        else:
            assert not path.exists()
            assert store.peek(key) is None
    finally:
        store.close()
