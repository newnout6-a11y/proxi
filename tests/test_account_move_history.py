"""A conversation that has to change account can take its signed history along (account_move_keeps_history, off by default).

Live measurement, 2026-11-07, an ordinary client of the relay: the provider's cache is shared by the accounts. Twelve pairs of
conversations sent the same 7,947-token prefix, the second on another account than the first (journal accounts of both
requests): all twelve read the whole prefix (cache_write 7947 on the first, cache_read 7947 on the second), two of two on
GPT-6.1 Sol too; 35 of 35 conversations that an eviction had put on another account between two turns read their cache. What a
forced move cost in the journal, 203,002 and 581,249 tokens written (20.6 and 30.0 credits), came from the signed thinking
blocks the relay left behind: a body without them is not the cached one. The first move of that journal, whose conversation had
no signed history, read 194,866 tokens on the new account and wrote 2,540."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import admin
import config
import conversation_state as cs
import journal
from test_conversation_state import (BIASED, DONE, KEY, TWO, converse, processes, refused,  # noqa: F401 - processes is a fixture
                                     signature, signatures_in, state_file, stored, tool_scripts)
from test_native_history import parsed
from test_proxy_limits import load_config

ON = {"account_move_keeps_history": True}
SIGNATURES = [signature(n) for n in range(3)]


def conversation_that_moves(process, *, rounds=3):
    """`rounds` tool round trips on acc-two (the preferred account), then acc-two is switched off and the client asks again."""
    async def story():
        messages = await converse(process, "chat", rounds)
        branches = len(process.pool._conversations[KEY]._native_history.branches)
        process.backends[1].cfg["enabled"] = False
        events = await process.turn(parsed(messages))
        return messages, branches, events

    return asyncio.run(story())


def test_by_default_a_conversation_that_changes_account_leaves_its_signed_history_behind(tmp_path, processes):
    process = processes(tmp_path / "state", accounts=TWO, priority=BIASED, scripts=[*tool_scripts(3), DONE])
    seq = journal.JOURNAL.last_seq
    conversation_that_moves(process)
    assert [sent.account for sent in process.sent] == ["acc-two"] * 3 + ["acc-one"]
    assert signatures_in(process.sent[-1].body) == []
    (moved,) = stored(seq, "account.migrate")
    assert "carried_branches" not in moved["data"]


def test_with_the_setting_the_signed_history_goes_along_and_the_request_is_the_one_that_never_moved(tmp_path, processes):
    control = processes(tmp_path / "control", accounts=TWO, priority=BIASED, scripts=[*tool_scripts(3), DONE])

    async def stays():
        await control.turn(parsed(await converse(control, "chat", 3)))

    asyncio.run(stays())
    process = processes(tmp_path / "state", accounts=TWO, priority=BIASED, scripts=[*tool_scripts(3), DONE],
                        backend_options=ON)
    seq = journal.JOURNAL.last_seq
    _, branches, events = conversation_that_moves(process)
    moved, unmoved = process.sent[-1], control.sent[-1]
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert (unmoved.account, moved.account) == ("acc-two", "acc-one")
    assert signatures_in(moved.body) == SIGNATURES == signatures_in(unmoved.body)
    # What the provider's shared cache needs: the very same prefix as the conversation that stayed.
    assert moved.body["messages"] == unmoved.body["messages"]
    assert moved.body["rit"] == unmoved.body["rit"] and moved.body["openAICacheKey"] == unmoved.body["openAICacheKey"]
    (event,) = stored(seq, "account.migrate")
    assert event["data"]["carried_branches"] == branches >= 1
    assert not stored(seq, "native_history.cold", "native_history.reset")


def test_the_stored_copy_of_a_conversation_that_moved_names_its_new_account(tmp_path, processes):
    state = tmp_path / "state"
    process = processes(state, accounts=TWO, priority=BIASED, scripts=[*tool_scripts(3), DONE], backend_options=ON)
    conversation_that_moves(process)
    header, _ = cs.decode(state_file(state).read_bytes(), 10 ** 9)
    assert header["account"] == "acc-one" and header["branches"] >= 1
    assert header["signatures"] == len(SIGNATURES)  # the signed blocks are in the copy that a restart would read


def test_an_account_that_refuses_the_signatures_loses_them_and_the_retry_goes_the_unsigned_way(tmp_path, processes):
    process = processes(tmp_path / "state", accounts=TWO, priority=BIASED, scripts=[*tool_scripts(3), refused(), DONE],
                        backend_options=ON)
    seq = journal.JOURNAL.last_seq

    async def story():
        messages = await converse(process, "chat", 3)
        process.backends[1].cfg["enabled"] = False
        first = await process.turn(parsed(messages))
        assert first[-1]["type"] == "error"
        assert process.pool._conversations[KEY]._native_history.branches == []  # forgotten, not replayed again
        retry = await process.turn(parsed(messages))
        assert retry[-1] == {"type": "end", "reason": "stop"}

    asyncio.run(story())
    assert signatures_in(process.sent[3].body) == SIGNATURES  # the refused attempt did carry them
    assert "SIG-" not in json.dumps(process.sent[4].body)  # the retry did not
    assert [event["data"]["reason"] for event in stored(seq, "conversation_state.dropped")] == ["rejected"]


def test_a_branch_without_thinking_goes_along_too_and_the_conversation_goes_on(tmp_path, processes):
    process = processes(tmp_path / "state", accounts=TWO, priority=BIASED, scripts=[DONE, DONE], backend_options=ON)
    seq = journal.JOURNAL.last_seq

    async def story():
        messages = [{"role": "user", "content": "hello"}]
        await process.turn(parsed(messages))
        process.backends[1].cfg["enabled"] = False
        return await process.turn(parsed(messages))

    events = asyncio.run(story())
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert process.sent[-1].account == "acc-one" and signatures_in(process.sent[-1].body) == []
    (moved,) = stored(seq, "account.migrate")
    assert moved["data"]["carried_branches"] == 1


def test_a_history_that_cannot_be_carried_never_fails_the_move(tmp_path, processes, monkeypatch):
    process = processes(tmp_path / "state", accounts=TWO, priority=BIASED, scripts=[*tool_scripts(3), DONE],
                        backend_options=ON)
    seq = journal.JOURNAL.last_seq

    async def story():
        messages = await converse(process, "chat", 3)
        process.backends[1].cfg["enabled"] = False

        def broken(*_args):
            raise RuntimeError("a bug in the capture")

        monkeypatch.setattr(cs, "capture", broken)
        return await process.turn(parsed(messages))

    events = asyncio.run(story())
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert process.sent[-1].account == "acc-one" and signatures_in(process.sent[-1].body) == []
    assert [event["data"]["error"] for event in stored(seq, "account.history_kept_back")] == ["RuntimeError"]


def test_the_setting_is_a_boolean_that_the_admin_applies_without_a_restart(tmp_path, monkeypatch):
    assert config.DEFAULTS["account_move_keeps_history"] is False
    assert load_config(tmp_path, monkeypatch, account_move_keeps_history=True)["account_move_keeps_history"] is True
    for bad in ("true", 1, None):
        with pytest.raises(Exception):
            load_config(tmp_path, monkeypatch, account_move_keeps_history=bad)
    field = admin._schema()["account_move_keeps_history"]
    assert field["type"] == "boolean" and field["restart_required"] is False
    assert SimpleNamespace(**field).group == "routing"
