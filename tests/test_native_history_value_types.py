"""The client's history matches the kept reply only when its values are the same values: true is not 1, false is not 0.

History.resume and the echo check compared the client's messages with ==, and Python says True == 1 and False == 0. A
history that held 1 where the model wrote true (or 0 for false) was taken for the very conversation the signed reply
belongs to, and the branch went on with a prefix the client does not hold (bug summary of 2026-11-06, section 3, item 14).
The values that are the same stay the same: 1 and 1.0 are one number to a JSON client."""
import pytest

import native_history

USER = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
RESULT = {"role": "tool", "content": {"toolCallId": "c1", "toolResult": "ok"}}


def call(arguments, call_id="c1"):
    return {"type": "tool_call", "toolCallId": call_id, "toolName": "t", "toolInput": arguments}


def assistant(arguments, call_id="c1"):
    return {"role": "assistant", "content": [call(arguments, call_id)]}


def kept(arguments, before=()):
    history = native_history.History()
    history.record("S", [USER, *before], [USER], [call(arguments)], assistant(arguments))
    return history


def resumes(history, arguments, before=()):
    return history.resume("S", [USER, *before, assistant(arguments), RESULT])[0] is not None


@pytest.mark.parametrize("stored,echoed,same", [
    ({"all": True}, {"all": True}, True), ({"all": False}, {"all": False}, True),
    ({"all": True}, {"all": 1}, False), ({"all": 1}, {"all": True}, False),
    ({"all": False}, {"all": 0}, False), ({"all": 0}, {"all": False}, False),
    ({"flags": [True, False]}, {"flags": [True, False]}, True), ({"flags": [True, False]}, {"flags": [1, 0]}, False),
    ({"deep": {"on": [{"x": True}]}}, {"deep": {"on": [{"x": 1}]}}, False),
    ({"n": 1}, {"n": 1.0}, True), ({"n": 0}, {"n": 0.0}, True), ({"n": 2}, {"n": 3}, False)])
def test_the_echo_of_the_kept_reply_has_to_hold_the_same_values(stored, echoed, same):
    history = kept(stored)
    assert resumes(history, echoed) is same
    assert (history.mismatch("S", [USER, assistant(echoed), RESULT]) == "tool_results") is same


def test_the_label_of_a_reset_names_the_arguments_when_a_bool_stands_for_a_number():
    history = kept({"all": True})
    assert history.mismatch("S", [USER, assistant({"all": 1}), RESULT]) == "echo:arguments"


@pytest.mark.parametrize("before_then,before_now,same", [
    ([assistant({"a": True}, "c0"), {"role": "tool", "content": {"toolCallId": "c0", "toolResult": "r"}}],
     [assistant({"a": True}, "c0"), {"role": "tool", "content": {"toolCallId": "c0", "toolResult": "r"}}], True),
    ([assistant({"a": True}, "c0"), {"role": "tool", "content": {"toolCallId": "c0", "toolResult": "r"}}],
     [assistant({"a": 1}, "c0"), {"role": "tool", "content": {"toolCallId": "c0", "toolResult": "r"}}], False)])
def test_the_older_messages_of_the_prefix_have_to_hold_the_same_values_too(before_then, before_now, same):
    history = kept({"b": 1}, before_then)
    assert resumes(history, {"b": 1}, before_now) is same


def test_an_argument_is_at_its_default_only_when_it_is_that_value():
    defaults = {"t": {"replace_all": False}}

    def echo(arguments):
        return assistant(arguments)

    assert native_history.same_echo(echo({"p": "a", "replace_all": False}), echo({"p": "a"}), defaults)
    assert not native_history.same_echo(echo({"p": "a", "replace_all": 0}), echo({"p": "a"}), defaults)
    assert not native_history.same_echo(echo({"p": "a"}), echo({"p": "a", "replace_all": 0}), defaults)
    assert native_history.same_echo(echo({"p": "a", "limit": 0}), echo({"p": "a"}), {"t": {"limit": 0}})
    assert not native_history.same_echo(echo({"p": "a", "limit": False}), echo({"p": "a"}), {"t": {"limit": 0}})
