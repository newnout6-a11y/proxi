"""Past its count limit the state store drops the states too small to matter before the big ones (item 2b, a4 F3).

a4's run (2026-11-06): a flow of one-shot jobs and tests kept the store at the 200 files it is limited to through all of its
2,316 saves, and the oldest state, always one of 343 bytes, was dropped 266 times (`conversation_state.dropped
reason=limit_conversations`). Replaying those saves, a state of a real session (133 of the 280 conversations had more than 4 KiB)
is dropped by the same rule once it is the oldest, after 84-95 minutes (median 87), and the first request after the next
restart then writes that session's whole prefix again. Now the state that holds nothing goes first, and none of the large ones does.
"""
import conversation_state as cs
import journal
from test_conversation_state import Clock, capture_of, files, stored

LARGE, TINY = 30 * 1024, 343


def store_with(tmp_path, sizes, **limits):
    """A store whose index lists the states `sizes` gives as {number: (saved_at, bytes)}: no file is written."""
    store = cs.ConversationStore(tmp_path / "state", clock=lambda: 1000.0, **limits)
    for number, (saved_at, size) in sizes.items():
        store._index["k%d" % number] = cs.Meta("k%d" % number, "t%d" % number, "acc", float(saved_at), size, 1, 0, 2)
    return store


def kept(store):
    return sorted(int(key[1:]) for key in store._index)


def test_the_tiny_states_go_before_the_large_ones_whatever_their_age(tmp_path):
    # three large states, the oldest of all, and six newer tiny ones, five files allowed
    sizes = {**{number: (number, LARGE) for number in range(3)}, **{number: (number, TINY) for number in range(3, 9)}}
    store = store_with(tmp_path, sizes, max_conversations=5)
    seq = journal.JOURNAL.last_seq
    store._enforce()
    assert kept(store) == [0, 1, 2, 7, 8]  # the large ones stay, and of the tiny ones the newest two
    dropped = stored(seq, "conversation_state.dropped")
    assert [event["data"]["reason"] for event in dropped] == ["limit_conversations"] * 4
    assert [event["conversation"] for event in dropped] == ["t3", "t4", "t5", "t6"]  # oldest first within the tiny ones
    assert [event["data"]["bytes"] for event in dropped] == [TINY] * 4
    store.close()


def test_among_states_of_one_kind_the_oldest_goes(tmp_path):
    store = store_with(tmp_path, {number: (number, LARGE) for number in range(8)}, max_conversations=5)
    store._enforce()
    assert kept(store) == [3, 4, 5, 6, 7]
    store.close()
    store = store_with(tmp_path, {number: (number, TINY) for number in range(8)}, max_conversations=5)
    store._enforce()
    assert kept(store) == [3, 4, 5, 6, 7]  # all tiny: the old rule, which the existing tests of the store pin as well
    store.close()


def test_when_there_are_not_enough_tiny_states_the_oldest_large_ones_go_after_them(tmp_path):
    sizes = {0: (0, LARGE), 1: (1, LARGE), 2: (2, TINY), 3: (3, TINY), 4: (4, LARGE)}
    store = store_with(tmp_path, sizes, max_conversations=3)
    store._enforce()
    assert kept(store) == [0, 1, 4]
    store.close()
    store = store_with(tmp_path, sizes, max_conversations=2)
    store._enforce()
    assert kept(store) == [1, 4]  # both tiny ones, then the older of the large ones
    store.close()


def test_the_state_just_written_is_never_dropped_even_when_it_is_tiny_and_the_oldest(tmp_path):
    sizes = {0: (0, TINY), 1: (1, LARGE), 2: (2, LARGE)}
    store = store_with(tmp_path, sizes, max_conversations=2)
    store._enforce(keep="k0")
    assert kept(store) == [0, 2]  # the oldest large one went in its place
    store.close()


def test_a_state_of_exactly_the_tiny_size_counts_as_tiny_and_one_byte_more_does_not(tmp_path):
    sizes = {0: (0, cs.TINY_STATE_BYTES + 1), 1: (1, cs.TINY_STATE_BYTES), 2: (2, cs.TINY_STATE_BYTES + 1)}
    store = store_with(tmp_path, sizes, max_conversations=2)
    store._enforce()
    assert kept(store) == [0, 2]
    store.close()
    assert 300 <= cs.TINY_STATE_BYTES <= 8192  # a one-request state is a few hundred bytes, a real session's tens of KiB


def test_past_the_byte_limit_alone_the_oldest_goes_and_a_tiny_state_is_not_dropped_for_nothing(tmp_path):
    sizes = {**{number: (number, LARGE) for number in range(3)}, 3: (3, TINY)}
    store = store_with(tmp_path, sizes, max_conversations=50, max_total_bytes=70 * 1024)
    seq = journal.JOURNAL.last_seq
    store._enforce()
    assert kept(store) == [1, 2, 3]  # dropping the tiny one would free 343 bytes of the 20 KiB that are over
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.dropped")] == ["limit_bytes"]
    store.close()


def test_past_both_limits_the_tiny_states_go_for_the_count_and_the_oldest_for_the_bytes(tmp_path):
    sizes = {0: (0, LARGE), 1: (1, LARGE), 2: (2, LARGE), 3: (3, TINY), 4: (4, TINY), 5: (5, LARGE)}
    store = store_with(tmp_path, sizes, max_conversations=4, max_total_bytes=100 * 1024)
    seq = journal.JOURNAL.last_seq
    store._enforce()
    assert kept(store) == [1, 2, 5]  # over by two files and 70 KiB: the two tiny ones, then the oldest large one
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.dropped")] == [
        "limit_conversations", "limit_conversations", "limit_bytes"]
    store.close()


def test_an_expired_state_still_goes_first_and_does_not_count_towards_the_limit(tmp_path):
    sizes = {0: (-100000, LARGE), 1: (1, LARGE), 2: (2, TINY), 3: (3, LARGE)}
    store = store_with(tmp_path, sizes, max_conversations=3)
    seq = journal.JOURNAL.last_seq
    store._enforce()
    assert kept(store) == [1, 2, 3]
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.dropped")] == ["expired"]
    store.close()


# -- with real files ---------------------------------------------------------------------------------------------------

def test_written_states_follow_the_same_rule_and_the_files_are_removed(tmp_path):
    clock, state = Clock(), tmp_path / "state"
    store = cs.ConversationStore(state, max_conversations=5, clock=clock)
    names = {}
    for label, pad in [("real-%d" % number, 6000) for number in range(3)] + [("job-%d" % number, 0) for number in range(6)]:
        clock.now += 10
        assert store.submit(capture_of(label, pad=pad)) and store.flush(5)
        names[label] = cs.key_digest(label) + cs.SUFFIX
    assert store.peek("real-0").size > cs.TINY_STATE_BYTES >= store.peek("job-5").size  # the premise of the test
    survivors = [label for label in names if store.peek(label) is not None]
    assert survivors == ["real-0", "real-1", "real-2", "job-4", "job-5"]
    assert files(state) == sorted(names[label] for label in survivors)
    store.close()


def test_a_restart_with_more_files_than_the_limit_applies_the_rule_too(tmp_path):
    clock, state = Clock(), tmp_path / "state"
    first = cs.ConversationStore(state, clock=clock)
    for label, pad in [("real-%d" % number, 6000) for number in range(2)] + [("job-%d" % number, 0) for number in range(5)]:
        clock.now += 10
        assert first.submit(capture_of(label, pad=pad)) and first.flush(5)
    first.close()
    assert len(files(state)) == 7
    second = cs.ConversationStore(state, max_conversations=4, clock=clock)  # the limit was lowered, or the files piled up
    assert [label for label in ("real-0", "real-1", "job-0", "job-1", "job-2", "job-3", "job-4")
            if second.peek(label) is not None] == ["real-0", "real-1", "job-3", "job-4"]
    assert len(files(state)) == 4
    second.close()
