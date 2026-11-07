"""On GLM the history has one form from the first request, so the first tool result no longer re-renders it.

GLM's reasoning arrives without a signature: a reply that reasons is never kept as a branch, and every request is rebuilt
from the client's transcript. The transcript was taken only once it held a tool result; before that the relay flattened the
history (`User: ...`, `Assistant: ...` as user text). The request with the first tool result therefore changed every earlier
message, and the provider read none of them again: 11,094 tokens re-read uncached in a Harness conversation on GLM-5.3
(72b51698, 1.1 credits; reproduced as 9a425e983f84), three such flips in two hours (journal watch of 2026-11-07)."""
import json

from prompt_support import account, after_tool_call, client_request, prompt_relay, strip_cache  # noqa: F401 - prompt_relay is a fixture
from test_review_fixes_pr19 import END, THINKING, text

GLM = "zai-org/GLM-5.3"
HEADERS = {"x-session-affinity": "glm-one-form"}
REASONING = [*THINKING, {"event": "content_block_delta", "type": "thinking", "partialThinking": "Let me think."},
             {"event": "content_block_end", "type": "thinking"}]  # no signature: GLM's reasoning
CALL = [{"event": "content_block_start", "type": "tool_call", "toolCallId": "c1", "toolName": "read_file"},
        {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps({"path": "a"})},
        {"event": "content_block_end", "type": "tool_call", "toolCallId": "c1"}]


def ask(relay, messages, script, model=GLM):
    relay.scripts.append(script)
    status, payload, _ = relay.post("chat", client_request("chat", messages, model=model), HEADERS)
    assert status == 200, payload
    return payload, relay.posts[-1]


def begins_with(later, earlier):
    shared = strip_cache(later["messages"][:len(earlier["messages"])])
    return json.dumps(shared, sort_keys=True) == json.dumps(strip_cache(earlier["messages"]), sort_keys=True)


def test_every_glm_request_extends_the_body_before_it_also_at_the_first_tool_result(prompt_relay):
    relay = prompt_relay(accounts=[{**account(), "retool_fast_provider_resource_name": "__managed__"}])
    messages = [{"role": "user", "content": "Read the notes first: " + "background " * 200 + ". Then read the file a."}]
    _, first = ask(relay, messages, [*REASONING, text("I will read it."), *END])
    messages += [{"role": "assistant", "content": "I will read it."}, {"role": "user", "content": "Go on."}]
    payload, second = ask(relay, messages, [*REASONING, *CALL, *END])
    messages = after_tool_call("chat", messages, payload)
    _, third = ask(relay, messages, [*REASONING, text("Done."), *END])
    assert begins_with(second, first) and begins_with(third, second)
    # the history is in its own roles from the start, not flattened into user text
    assert [m["role"] for m in third["messages"]][1:] == ["user", "assistant", "user", "assistant", "tool"]


def test_a_route_with_signed_thinking_keeps_its_first_request_as_it_was(prompt_relay):
    """Claude's and GPT's first requests are untouched: their replies are kept as branches, which continue the first body."""
    relay = prompt_relay()
    messages = [{"role": "user", "content": "Read the file a."}]
    _, first = ask(relay, messages, [text("ok"), *END], model="claude-sonnet-5-5")
    flattened = json.dumps(first["messages"][1:])
    assert "User: Read the file a." in flattened
