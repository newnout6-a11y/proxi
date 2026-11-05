"""The history edits stay within the plan's time budget on a request of about 800K tokens (plan I11)."""
import copy
import json
import time

import pytest

import prompt_body
from test_prompt_body_support import FakeSnapshot, profile

BUDGET_MS = 50
RUNS = 9


def agent_body(messages, characters):
    """An OpenCode-shaped chat request: a 17 KB head, a user task, then tool calls and long tool results,
    some of them with a system-reminder block and a few mid-conversation system messages."""
    per = 2 * characters // messages
    body = [{"role": "system", "content": "H" * 17000}, {"role": "user", "content": "Inspect the project. " * 90}]
    for i in range(messages // 2):
        output = ("line %d of the tool output, long enough to look like a file listing\n" % i) * max(1, per // 70)
        if i % 3 == 0:
            output += "\n<system-reminder>\nPlan mode is active. Do not edit files.\n</system-reminder>\n"
        elif i % 7 == 0:
            output += "\n<system-reminder>\nInstructions from: AGENTS.md\nUse tabs.\n</system-reminder>\n"
        body.append({"role": "assistant", "content": None, "reasoning_content": "thinking " * 12,
                     "tool_calls": [{"id": "call_%d" % i, "type": "function",
                                     "function": {"name": "read", "arguments": json.dumps({"path": "src/mod%d.py" % i})}}]})
        body.append({"role": "tool", "tool_call_id": "call_%d" % i, "content": output})
        if i % 150 == 149:
            body.append({"role": "system", "content": "Mid-conversation rule %d." % i})
        if i % 400 == 399:
            body.append({"role": "user", "content": "A follow-up question number %d?\n\n<system-reminder>\nPlan mode "
                                                    "is active.\n</system-reminder>" % i})
    return {"model": "claude-opus-5-5", "messages": body, "stream": True,
            "tools": [{"type": "function", "function": {"name": "t%d" % k, "description": "d" * 600, "parameters": {
                "type": "object", "properties": {"path": {"type": "string"}}}}} for k in range(14)]}


def best_ms(call, runs=RUNS):
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        call()
        best = min(best, (time.perf_counter() - start) * 1000)
    return best


@pytest.mark.parametrize("messages", [1500, 4000])
def test_history_edits_on_an_800k_token_body_stay_within_the_budget(messages):
    body = agent_body(messages, 3_200_000)
    size = len(json.dumps(body))
    assert 3_000_000 < size < 4_500_000          # about 800K tokens of text
    rules = profile(
        mid_system="wrap",
        user_blocks=[{"op": "drop_tag", "tag": "system-reminder", "contains": "Plan mode"},
                     {"op": "drop_tag", "tag": "system-reminder", "contains": "Instructions from"},
                     {"op": "remove_regex", "pattern": r"Mid-conversation rule \d+\."},
                     {"op": "replace_text", "find": "line 77 of", "with": "LINE 77 of"}],
        tools=[{"tool": "t1*", "op": "append", "text": "Ask before running anything destructive."},
               {"tool": "t2", "hide": True}])
    snapshot = FakeSnapshot()
    before = copy.deepcopy(body)
    result, changes = prompt_body.rewrite(body, "chat", rules, snapshot)
    assert body == before
    assert len(changes) > messages // 6 and any(c["op"] == "mid_system" for c in changes)
    assert "Plan mode" not in json.dumps(result["messages"])
    timing = best_ms(lambda: prompt_body.rewrite(body, "chat", rules, snapshot))
    history_only = profile(mid_system=rules["history"]["mid_system"], user_blocks=rules["history"]["user_blocks"])
    history = best_ms(lambda: prompt_body.rewrite(body, "chat", history_only, snapshot))
    assert history <= BUDGET_MS, "history edits took %.1f ms of %d ms (whole rewrite %.1f ms)" % (
        history, BUDGET_MS, timing)
    assert timing <= BUDGET_MS * 1.5, "the whole rewrite took %.1f ms" % timing


@pytest.mark.parametrize("shape", ["one-line", "many-lines"])
def test_a_huge_text_with_tens_of_thousands_of_tags_is_cut_in_linear_time(shape):
    # Minified HTML in a tool result: one line, 40K small blocks. Looking back for the line start of every
    # tag, or gluing the pieces together one by one, would make this take minutes.
    separator = "" if shape == "one-line" else "\n"
    block = "<note>x</note>" + separator
    text = ("<p>keep</p>" + separator + block) * 40_000
    data = {"model": "claude-opus-5-5", "messages": [
        {"role": "system", "content": "HEAD"}, {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                                "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": text}, {"role": "user", "content": "more"}]}
    start = time.perf_counter()
    result, changes = prompt_body.rewrite(data, "chat", profile(user_blocks=[
        {"op": "drop_tag", "tag": "note"}, {"op": "drop_tag", "tag": "p", "contains": "keep"}]), FakeSnapshot())
    elapsed = time.perf_counter() - start
    assert elapsed < 3, "%d tags took %.1f s" % (80_000, elapsed)
    after = result["messages"][3]["content"]
    if shape == "one-line":
        # Only the first tag starts the line; every other one is a mention in the middle of it.
        assert after == text[len("<p>keep</p>"):]
        assert [(c["edit"], c["removed_chars"]) for c in changes] == [(1, len("<p>keep</p>"))]
    else:
        assert "<note>" not in after and after.count("<p>keep</p>") == 0
        assert len(changes) == 2 and changes[0]["removed_chars"] > 0


@pytest.mark.parametrize("dialect", ["responses", "anthropic"])
def test_the_other_dialects_are_as_fast(dialect):
    chat = agent_body(2000, 3_200_000)
    messages = []
    for message in chat["messages"][1:]:
        if dialect == "anthropic":
            if message["role"] == "assistant":
                call = message["tool_calls"][0]
                messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": call["id"], "name": "read",
                                                                   "input": {"path": "x"}}]})
            elif message["role"] == "tool":
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": message["tool_call_id"],
                                                              "content": message["content"]}]})
            else:
                messages.append(message)
        else:
            if message["role"] == "assistant":
                call = message["tool_calls"][0]
                messages.append({"type": "function_call", "call_id": call["id"], "name": "read",
                                 "arguments": call["function"]["arguments"]})
            elif message["role"] == "tool":
                messages.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                                 "output": message["content"]})
            else:
                messages.append({"type": "message", "role": "developer" if message["role"] == "system" else "user",
                                 "content": message["content"]})
    body = {"model": "claude-opus-5-5", "tools": []}
    body["input" if dialect == "responses" else "messages"] = messages
    rules = profile(mid_system="wrap", user_blocks=[
        {"op": "drop_tag", "tag": "system-reminder", "contains": "Plan mode"},
        {"op": "drop_tag", "tag": "system-reminder", "contains": "Instructions from"}])
    result, changes = prompt_body.rewrite(body, dialect, rules, FakeSnapshot())
    assert len(changes) > 300
    assert best_ms(lambda: prompt_body.rewrite(body, dialect, rules, FakeSnapshot())) <= BUDGET_MS
