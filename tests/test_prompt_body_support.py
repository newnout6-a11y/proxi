"""Shared fixtures for the prompt_body tests: a fake Snapshot, profiles and one conversation in the three dialects.

Conversations are described once, dialect-free, as turns, and `neutral` reads any dialect back into
the same tuples, so a test states what a rewrite must do once and runs it against chat, responses and
anthropic bodies.
"""
import copy
import json
import re

import native_history
import validate

DIALECTS = ["chat", "responses", "anthropic"]
MODEL = "claude-opus-5-5"
CFG = {"strict_models": True, "model": MODEL}
REMINDER = "<system-reminder>\nPlan mode is active. Do not edit files.\n</system-reminder>"
TOOL_REMINDER = "<system-reminder>\nInstructions from: AGENTS.md\nUse tabs.\n</system-reminder>"
IMAGE = "data:image/png;base64,AAAA"
_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


class FakeSnapshot:
    """The part of prompt_policy.Snapshot that prompt_body reads (plan 4.3): regex() and relay_text()."""

    def __init__(self, reminder=None):
        self.reminder = reminder
        self.regex_calls = []

    def regex(self, pattern, flags=""):
        self.regex_calls.append((pattern, flags))
        return re.compile(pattern, sum(_FLAGS[flag] for flag in flags))

    def relay_text(self, name):
        return self.reminder if name == "reminder" else None


def profile(mid_system="keep", user_blocks=(), tools=()):
    """A normalized profile as Snapshot.profile() returns it, with only the sections prompt_body reads edited."""
    return {"description": "", "head": "{{client_system}}", "client_system": {"edits": []},
            "history": {"mid_system": mid_system, "user_blocks": copy.deepcopy(list(user_blocks))},
            "tools": {"edits": copy.deepcopy(list(tools))}, "generation": {}, "publish_as_model": False}


def drop_tag(tag="system-reminder", **extra):
    return {"op": "drop_tag", "tag": tag, **extra}


def history_key(dialect):
    return "input" if dialect == "responses" else "messages"


def history_of(data, dialect):
    return data[history_key(dialect)]


def _user_parts(dialect, texts):
    kind = "input_text" if dialect == "responses" else "text"
    return [part if isinstance(part, dict) else {"type": kind, "text": part} for part in texts]


def _message(dialect, turn):
    """One dialect message (or two for Messages tool results with text) for a neutral turn."""
    kind = turn[0]
    if kind == "user":
        value = turn[1]
        if dialect == "chat":
            return [{"role": "user", "content": value if isinstance(value, str) else _user_parts(dialect, value)}]
        if dialect == "anthropic":
            return [{"role": "user", "content": value if isinstance(value, str) else _user_parts(dialect, value)}]
        content = value if isinstance(value, str) else _user_parts(dialect, value)
        return [{"type": "message", "role": "user", "content": content}]
    if kind == "assistant":
        if dialect == "chat":
            return [{"role": "assistant", "content": turn[1]}]
        if dialect == "anthropic":
            return [{"role": "assistant", "content": [{"type": "text", "text": turn[1]}]}]
        return [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": turn[1]}]}]
    if kind == "call":
        _, call_id, name = turn
        arguments = {"path": "a"}
        if dialect == "chat":
            return [{"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}]
        if dialect == "anthropic":
            return [{"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": name,
                                                       "input": arguments}]}]
        return [{"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}]
    if kind == "result":
        _, call_id, value = turn
        if dialect == "chat":
            return [{"role": "tool", "tool_call_id": call_id,
                     "content": value if isinstance(value, str) else _user_parts(dialect, value)}]
        if dialect == "anthropic":
            return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                  "content": value if isinstance(value, str)
                                                  else _user_parts(dialect, value)}]}]
        return [{"type": "function_call_output", "call_id": call_id,
                 "output": value if isinstance(value, str) else _user_parts(dialect, value)}]
    if kind in ("system", "lead"):
        extra = dict(turn[2]) if len(turn) > 2 else {}
        value = turn[1]
        if dialect == "responses":
            content = value if isinstance(value, str) else _user_parts(dialect, value)
            return [{"type": "message", "role": "developer", "content": content, **extra}]
        return [{"role": "system", "content": value if isinstance(value, str) else _user_parts(dialect, value),
                 **extra}]
    raise AssertionError(kind)


def tool_objects(dialect, specs):
    """specs: [(name, description)] -> tool definitions of the dialect."""
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    if dialect == "chat":
        return [{"type": "function", "function": {"name": n, "description": d, "parameters": copy.deepcopy(schema)}}
                for n, d in specs]
    if dialect == "responses":
        return [{"type": "function", "name": n, "description": d, "parameters": copy.deepcopy(schema)}
                for n, d in specs]
    return [{"name": n, "description": d, "input_schema": copy.deepcopy(schema)} for n, d in specs]


def conversation(dialect, turns, *, system="HEAD", tools=None, **extra):
    """A request body of the dialect for the neutral turns; `lead` turns open the history (the head)."""
    messages = []
    for turn in turns:
        messages.extend(_message(dialect, turn))
    data = {"model": MODEL}
    if dialect == "chat":
        data["messages"] = ([{"role": "system", "content": system}] if system else []) + messages
    elif dialect == "responses":
        data["input"] = messages
        if system:
            data["instructions"] = system
    else:
        data["messages"] = messages
        data["max_tokens"] = 1024
        if system:
            data["system"] = system
    if tools:
        data["tools"] = tool_objects(dialect, tools)
    data.update(extra)
    return data


def _text(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return "\n".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in value
                     if not isinstance(part, dict) or part.get("type") in ("text", "input_text", "output_text"))


def neutral(data, dialect, head=False):
    """The history as dialect-free tuples: ("user", text), ("assistant", text), ("call", id, name),
    ("result", id, text) and ("system", text); a message with several parts joins them with a newline.
    The leading system / developer messages (the head) are left out unless head is True."""
    turns = []
    items = history_of(data, dialect)
    for item in items[0 if head else native_history.leading_instructions(items):]:
        role = item.get("role")
        if dialect == "responses" and role is None:
            if item["type"] == "function_call":
                turns.append(("call", item["call_id"], item["name"]))
            elif item["type"] == "function_call_output":
                turns.append(("result", item["call_id"], _text(item["output"])))
            continue
        if role in ("system", "developer"):
            turns.append(("system", _text(item.get("content"))))
        elif role == "user" and dialect == "anthropic" and isinstance(item["content"], list) \
                and any(b.get("type") == "tool_result" for b in item["content"]):
            for block in item["content"]:
                if block["type"] == "tool_result":
                    turns.append(("result", block["tool_use_id"], _text(block["content"])))
                else:
                    turns.append(("user", _text([block])))
        elif role == "user":
            turns.append(("user", _text(item["content"])))
        elif role == "tool":
            turns.append(("result", item["tool_call_id"], _text(item["content"])))
        elif role == "assistant":
            if dialect == "chat":
                if item.get("content"):
                    turns.append(("assistant", _text(item["content"])))
                for call in item.get("tool_calls") or []:
                    turns.append(("call", call["id"], call["function"]["name"]))
            elif dialect == "anthropic":
                for block in item["content"]:
                    if block["type"] == "text":
                        turns.append(("assistant", block["text"]))
                    elif block["type"] == "tool_use":
                        turns.append(("call", block["id"], block["name"]))
            else:
                turns.append(("assistant", _text(item["content"])))
    return turns


def parse(dialect, data):
    return getattr(validate, "parse_" + dialect)(copy.deepcopy(data), CFG)


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


def leaves(value):
    """Every container (dict / list) inside value, for checks that two bodies share nothing mutable."""
    found = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, (dict, list)):
            found.append(item)
            stack.extend(item.values() if isinstance(item, dict) else item)
    return found


def rich_conversation(dialect):
    """A conversation that touches every rewrite: reminders in user and tool text, a mid system message,
    tool calls, an image, a plain assistant turn and a second tool."""
    turns = [
        ("user", "Fix the build.\n\n" + REMINDER),
        ("call", "c1", "bash"),
        ("result", "c1", "ok\n\n" + TOOL_REMINDER),
        ("system", "Late rule: reply in Russian."),
        ("assistant", "Done with the first step."),
        ("user", ["Next, check the tests.", REMINDER]),
        ("call", "c2", "read"),
        ("result", "c2", ["contents of the file", TOOL_REMINDER]),
        ("assistant", "All good."),
        ("user", "Thanks."),
    ]
    return conversation(dialect, turns, tools=[("bash", "Run a command."), ("read", "Read a file."),
                                              ("webfetch", "Fetch a page.")])
