"""C10: the deltas of a Responses custom tool call add up to the input of its .done event, and show no wrapper."""
import json

import pytest

import adapters
from test_adapter_protocols import Writer, parsed

PATCH = "*** Begin Patch\n*** Add File: x.py\n+x = \"1\"\n*** End Patch"


def stream(fragments, *, done=True, tool_type="custom"):
    """Feed one custom call whose arguments grow fragment by fragment; returns (deltas, the .done input, the item)."""
    writer = Writer()
    adapter = adapters.ResponsesAdapter(parsed(), writer)
    adapter.begin()
    text = ""
    for index, piece in enumerate(fragments):
        text += piece
        last = index == len(fragments) - 1
        adapter.feed({"type": "tool_call", "id": "patch-1", "name": "apply_patch", "tool_type": tool_type,
                      "arguments": text, **({"done": True} if last and done else {})})
    final = adapter.end("tool_calls")
    deltas = [p["delta"] for kind, p in writer.events if kind == "response.custom_tool_call_input.delta"]
    finished = next(p["input"] for kind, p in writer.events if kind == "response.custom_tool_call_input.done")
    item = next(p["item"] for kind, p in writer.events if kind == "response.output_item.done")
    assert item == final["output"][0]
    return deltas, finished, item


def pieces(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


@pytest.mark.parametrize("size", [1, 2, 3, 7, 11])
def test_a_wrapped_patch_streams_without_a_piece_of_the_wrapper(size):
    deltas, finished, item = stream(pieces(json.dumps({"input": PATCH}), size))
    assert finished == item["input"] == PATCH
    assert "".join(deltas) == PATCH
    assert not any(delta.lstrip().startswith("{") or '"input"' in delta for delta in deltas)


def test_the_first_fragments_of_the_wrapper_are_not_sent_as_input():
    deltas, finished, _ = stream(["{", '"inp', 'ut": "', "*** Begin Patch\\n", "*** End Patch", '"}'])
    assert finished == "*** Begin Patch\n*** End Patch"
    assert "".join(deltas) == finished
    assert all("{" not in delta and "inp" not in delta for delta in deltas)


def test_a_wrapper_that_names_another_key_first_still_adds_up():
    deltas, finished, _ = stream(['{"note": "x", ', '"input": "', PATCH.replace("\n", "\\n").replace('"', '\\"'), '"}'])
    assert finished == PATCH
    assert "".join(deltas) == PATCH


def test_a_raw_grammar_input_that_opens_a_brace_adds_up():
    deltas, finished, _ = stream(["{ edit", " arbitrary", " grammar }"])
    assert finished == "{ edit arbitrary grammar }"
    assert "".join(deltas) == finished


def test_raw_text_streams_as_it_arrives():
    deltas, finished, _ = stream(["*** Begin", " Patch\n", "*** End Patch"])
    assert deltas == ["*** Begin", " Patch\n", "*** End Patch"] and finished == "*** Begin Patch\n*** End Patch"


def test_a_call_that_never_says_it_is_done_still_adds_up_at_the_end():
    deltas, finished, _ = stream(['{"input": "ab', 'c"}'], done=False)
    assert "".join(deltas) == finished == "abc"
