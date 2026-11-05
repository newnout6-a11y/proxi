"""Conversation pins (plan I3, 4.3 item 10): an active conversation keeps the revision it met."""
import pytest

import apierr
import journal
import prompt_policy as pp
from test_prompt_policy_common import Clock, document, inp, make_store


def doc(head="V1", rules=None, **extra):
    return document(profiles={"a": {"head": head}, "b": {"head": "B"}},
                    rules=rules if rules is not None else [{"name": "all", "use": "a"}], **extra)


def revise(store, **fields):
    """Save a new revision of doc() (with fields overriding) on top of the current one."""
    return store.save(doc(**fields), store.snapshot().rev)


def key(name="conv-1", **fields):
    return inp(conversation_key=name, **fields)


def test_a_request_without_a_conversation_key_is_never_pinned(tmp_path):
    store = make_store(tmp_path, doc())
    assert store.plan(inp()).rev == 1
    assert store.pins() == []
    revise(store, head="V2")
    assert store.plan(inp()).rev == 2


def test_the_first_request_of_a_conversation_pins_the_current_revision(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    first = store.plan(key())
    assert (first.rev, first.pinned) == (1, False)
    clock.advance(30)
    again = store.plan(key())
    assert (again.rev, again.pinned) == (1, True)
    assert store.pins() == [{"conversation": journal.tag("conv-1"), "purpose": "client-tools", "rev": 1,
                             "profile": "a", "age_s": 30.0}]


def test_a_new_revision_changes_new_conversations_only(tmp_path):
    store = make_store(tmp_path, doc())
    store.plan(key("old"))
    revise(store, head="V2")
    old, new = store.plan(key("old")), store.plan(key("new"))
    assert (old.rev, old.pinned) == (1, True)
    assert (new.rev, new.pinned) == (2, False)
    assert old.snapshot.rev == 1 and new.snapshot.rev == 2
    assert {item["conversation"]: item["rev"] for item in store.pins()} == {
        journal.tag("old"): 1, journal.tag("new"): 2}


def test_a_pinned_conversation_follows_the_rules_of_its_own_revision(tmp_path):
    store = make_store(tmp_path, doc(rules=[{"name": "first", "use": "a"}]))
    store.plan(key("old"))
    revise(store, rules=[{"name": "second", "use": "b"}])
    assert store.plan(key("old")).profile == "a" and store.plan(key("old")).rule == "first"
    assert store.plan(key("new")).profile == "b"


def test_every_purpose_of_a_conversation_is_pinned_on_its_own(tmp_path):
    store = make_store(tmp_path, doc())
    store.plan(key(purpose="client-tools"))
    revise(store, head="V2")
    assert store.plan(key(purpose="client-tools")).rev == 1
    assert store.plan(key(purpose="client-text")).rev == 2
    assert sorted((item["purpose"], item["rev"]) for item in store.pins()) == [("client-text", 2), ("client-tools", 1)]


def test_the_pinned_profile_shown_is_the_one_the_last_request_got(tmp_path):
    store = make_store(tmp_path, doc(rules=[{"name": "titles", "use": "b", "if": {"model": "title-*"}},
                                            {"name": "rest", "use": "a"}]))
    store.plan(key(model="claude-opus-5-5"))
    assert store.pins()[0]["profile"] == "a"
    store.plan(key(model="title-model"))
    assert store.pins()[0]["profile"] == "b"


# -- the legacy pin (no file yet) --------------------------------------------------------------------------------------


def test_without_a_file_a_conversation_is_pinned_to_the_legacy_revision(tmp_path):
    store = make_store(tmp_path)
    plan = store.plan(key("early"))
    assert plan.legacy and not plan.pinned
    assert store.pins() == [{"conversation": journal.tag("early"), "purpose": "client-tools", "rev": pp.LEGACY_REV,
                             "profile": None, "age_s": 0.0}]
    assert pp.LEGACY_REV == 0
    assert store.plan(key("early")).pinned is True                  # the second request meets its own pin


def test_a_file_that_appears_serves_new_conversations_and_not_the_early_ones(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock=clock)
    store.plan(key("early"))
    store.save(doc(), None)
    early, late = store.plan(key("early")), store.plan(key("late"))
    assert early.legacy and early.pinned and early.snapshot is None
    assert late.profile == "a" and late.rev == 1 and not late.pinned
    pins = {item["conversation"]: item for item in store.pins()}
    assert pins[journal.tag("early")]["rev"] == pp.LEGACY_REV and pins[journal.tag("early")]["profile"] is None
    assert pins[journal.tag("late")]["rev"] == 1


def test_a_file_that_disappears_returns_new_conversations_to_legacy_and_keeps_the_pinned_ones(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    store.plan(key("kept"))
    (tmp_path / "prompts.json").unlink()
    clock.advance(pp.RELOAD_INTERVAL + 0.1)
    store.maybe_reload()
    assert store.snapshot() is None
    assert store.plan(key("fresh")).legacy
    kept = store.plan(key("kept"))
    assert kept.profile == "a" and kept.rev == 1 and kept.pinned


def test_unpinning_a_legacy_conversation_gives_it_the_current_revision(tmp_path):
    store = make_store(tmp_path)
    store.plan(key("early"))
    store.save(doc(), None)
    assert store.plan(key("early")).legacy
    assert store.unpin("early") == 1
    assert store.plan(key("early")).profile == "a"


# -- unpin and apply to all ---------------------------------------------------------------------------------------------


def test_unpin_by_raw_key_by_tag_or_all(tmp_path):
    store = make_store(tmp_path, doc())
    for name in ("c1", "c2", "c3"):
        store.plan(key(name))
    store.plan(key("c1", purpose="client-text"))
    revise(store, head="V2")
    assert store.unpin("c1") == 2                                       # both purposes of the conversation
    assert store.plan(key("c1")).rev == 2 and store.plan(key("c2")).rev == 1
    assert store.unpin(journal.tag("c2")) == 1
    assert store.plan(key("c2")).rev == 2 and store.plan(key("c3")).rev == 1
    assert store.unpin("unknown") == 0 and store.unpin("") == 0
    assert store.unpin() >= 3
    assert store.pins() == []
    assert store.plan(key("c3")).rev == 2


def test_applying_to_all_conversations_moves_each_exactly_once(tmp_path):
    store = make_store(tmp_path, doc())
    store.plan(key("a"))
    store.plan(key("b"))
    revise(store, head="V2")
    assert [store.plan(key(name)).rev for name in ("a", "b")] == [1, 1]
    assert store.unpin(None) == 2
    first = [store.plan(key(name)) for name in ("a", "b")]
    second = [store.plan(key(name)) for name in ("a", "b")]
    assert [p.rev for p in first] == [2, 2] and [p.pinned for p in first] == [False, False]
    assert [p.rev for p in second] == [2, 2] and [p.pinned for p in second] == [True, True]


# -- limits ------------------------------------------------------------------------------------------------------------


def test_a_revision_pushed_out_of_the_history_gives_the_conversation_the_current_one(tmp_path):
    store = make_store(tmp_path, doc())
    store.plan(key("old"))
    for index in range(pp.SNAPSHOT_HISTORY):
        revise(store, head="V%d" % (index + 2))
    current = store.snapshot().rev
    assert current == pp.SNAPSHOT_HISTORY + 1
    plan = store.plan(key("old"))
    assert plan.rev == current and not plan.pinned
    assert store.pins()[0]["rev"] == current
    assert store.plan(key("old")).pinned


def test_a_revision_still_in_the_history_stays_pinned(tmp_path):
    store = make_store(tmp_path, doc())
    store.plan(key("old"))
    for index in range(pp.SNAPSHOT_HISTORY - 1):
        revise(store, head="V%d" % (index + 2))
    assert store.plan(key("old")).rev == 1


def test_pins_that_have_not_been_used_for_hours_are_forgotten(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    store.plan(key("idle"))
    store.plan(key("busy"))
    clock.advance(pp.PIN_IDLE_SECONDS - 60)
    store.plan(key("busy"))
    revise(store, head="V2")
    clock.advance(120)
    assert [item["conversation"] for item in store.pins()] == [journal.tag("busy")]
    assert store.plan(key("busy")).rev == 1
    idle = store.plan(key("idle"))
    assert idle.rev == 2 and not idle.pinned


def test_the_number_of_pins_is_bounded_and_the_least_recently_used_goes_first(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_PINS", 3)
    store = make_store(tmp_path, doc())
    for name in ("c1", "c2", "c3"):
        store.plan(key(name))
    store.plan(key("c1"))
    store.plan(key("c4"))
    assert sorted(item["conversation"] for item in store.pins()) == sorted(journal.tag(n) for n in ("c1", "c3", "c4"))
    revise(store, head="V2")
    assert store.plan(key("c1")).rev == 1 and store.plan(key("c2")).rev == 2


def test_pins_are_listed_most_recently_used_first_with_their_age(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    store.plan(key("first"))
    clock.advance(10)
    store.plan(key("second"))
    clock.advance(5)
    store.plan(key("first"))
    pins = store.pins()
    assert [item["conversation"] for item in pins] == [journal.tag("first"), journal.tag("second")]
    assert [item["age_s"] for item in pins] == [15.0, 5.0]
    assert set(pins[0]) == {"conversation", "purpose", "rev", "profile", "age_s"}


def test_pins_hold_a_tag_and_never_the_raw_key(tmp_path):
    store = make_store(tmp_path, doc())
    store.plan(key("claude-metadata:secret-session-id"))
    text = repr(store.pins())
    assert "secret-session-id" not in text and len(store.pins()[0]["conversation"]) == 8


# -- a profile the request names itself ----------------------------------------------------------------------------------


def test_a_virtual_profile_only_the_current_revision_has_moves_the_pin(tmp_path):
    store = make_store(tmp_path, document(profiles={"a": {"head": "A"}}, rules=[{"name": "r", "use": "a"}]))
    store.plan(key("chat"))
    store.save(document(profiles={"a": {"head": "A"}, "extra": {"head": "X"}}, rules=[{"name": "r", "use": "a"}]), 1)
    plan = store.plan(key("chat", virtual_profile="extra", requested_model="m"))
    assert plan.profile == "extra" and plan.rev == 2 and not plan.pinned
    assert store.pins()[0]["rev"] == 2
    assert store.plan(key("chat")).rev == 2


def test_a_virtual_profile_of_the_pinned_revision_keeps_the_pin(tmp_path):
    store = make_store(tmp_path, document(profiles={"a": {"head": "A"}, "extra": {"head": "X"}}))
    store.plan(key("chat"))
    store.save(document(profiles={"a": {"head": "A2"}}), 1)
    plan = store.plan(key("chat", virtual_profile="extra"))
    assert plan.profile == "extra" and plan.rev == 1 and plan.pinned


def test_a_legacy_pinned_conversation_that_picks_a_virtual_model_gets_the_current_revision(tmp_path):
    store = make_store(tmp_path)
    store.plan(key("early"))
    store.save(doc(), None)
    assert store.plan(key("early")).legacy
    plan = store.plan(key("early", virtual_profile="b", requested_model="m"))
    assert plan.profile == "b" and plan.rev == 1
    assert store.plan(key("early")).profile == "a"


def test_a_virtual_profile_no_revision_has_is_refused_and_leaves_the_pin_alone(tmp_path):
    store = make_store(tmp_path, doc())
    store.plan(key("chat"))
    revise(store, head="V2")
    with pytest.raises(apierr.ApiError) as raised:
        store.plan(key("chat", virtual_profile="ghost", requested_model="m"))
    assert raised.value.status == 404
    assert store.pins()[0]["rev"] == 1


def test_a_header_profile_only_the_current_revision_has_moves_the_pin(tmp_path):
    rules = [{"name": "h", "use": "$header", "if": {"header_profile": "*"}}, {"name": "r", "use": "a"}]
    store = make_store(tmp_path, document(profiles={"a": {"head": "A"}}, rules=rules))
    store.plan(key("chat"))
    store.save(document(profiles={"a": {"head": "A"}, "extra": {"head": "X"}}, rules=rules), 1)
    plan = store.plan(key("chat", header_profile="extra"))
    assert plan.profile == "extra" and plan.rev == 2
    with pytest.raises(apierr.ApiError) as raised:
        store.plan(key("chat", header_profile="ghost"))
    assert raised.value.status == 400


def test_a_refusal_without_a_pin_is_still_a_refusal(tmp_path):
    store = make_store(tmp_path, doc(rules=[{"name": "h", "use": "$header"}]))
    with pytest.raises(apierr.ApiError) as raised:
        store.plan(inp(header_profile="ghost"))
    assert raised.value.status == 400 and raised.value.param == "x-proxi-profile"


def test_a_plan_never_touches_the_journal(tmp_path):
    before = journal.JOURNAL.last_seq
    store = make_store(tmp_path, doc())
    after_load = journal.JOURNAL.last_seq
    for index in range(20):
        store.plan(key("c%d" % (index % 3)))
    assert journal.JOURNAL.last_seq == after_load >= before


def test_unpin_takes_the_tag_that_pins_shows(tmp_path):
    store = make_store(tmp_path, doc())
    store.plan(key("claude-metadata:session-1"))
    store.plan(key("claude-metadata:session-1", purpose="client-text"))
    store.plan(key("header:other"))
    revise(store, head="V2")
    shown = store.pins()
    assert len(shown) == 3 and all(len(item["conversation"]) == 8 for item in shown)
    tag = next(item["conversation"] for item in shown if item["purpose"] == "client-text")
    assert store.unpin(tag) == 2                                   # both purposes of that conversation
    remaining = store.pins()
    assert [item["conversation"] for item in remaining] == [journal.tag("header:other")]
    assert store.plan(key("claude-metadata:session-1")).rev == 2
    assert store.plan(key("header:other")).rev == 1 and store.plan(key("header:other")).pinned
    assert store.unpin(remaining[0]["conversation"]) == 1
    assert store.plan(key("header:other")).rev == 2
    assert store.unpin(tag) == 1                                    # the new pin of the first conversation, by its tag
