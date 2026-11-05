"""Synthetic request bodies shaped like real client captures (plan I1, prompt control).

Only the structure follows the captures: the system blocks, the billing header, the <system-reminder> blocks,
the roles and the number and form of the tools. Every text is made up here, so nothing private is stored.
The builders are deterministic: the golden test pins the bytes the relay derives from them.
"""
import copy
import json

OPENCODE_HEADERS = {"user-agent": "opencode/2.2.5", "x-opencode-session-id": "ses_golden0001",
                    "x-session-affinity": "ses_golden0001", "x-opencode-client": "cli",
                    "x-proxi-chat-title": "Synthetic%20golden%20chat"}
CLAUDE_SDK_HEADERS = {"user-agent": "claude-cli/2.1.309 (external, sdk-cli)",
                      "x-claude-code-session-id": "00000000-0000-4000-8000-000000000309"}
CODEX_HEADERS = {"user-agent": "codex_cli_rs/0.164.0 (Synthetic 1.0; x86_64) synthetic", "originator": "codex_cli_rs",
                 "x-session-id": "synthetic-codex-session"}


def filler(topic, sentences):
    """Deterministic prose: no dates, no counters that depend on the machine."""
    return " ".join("Synthetic %s rule %d keeps this fixture stable and unremarkable." % (topic, number)
                    for number in range(1, sentences + 1))


def opencode_system():
    skills = "\n".join("  <skill>\n    <id>synthetic-skill-%d</id>\n    <name>synthetic-skill-%d</name>\n"
                       "    <description>%s</description>\n  </skill>" % (n, n, filler("skill %d" % n, 3))
                       for n in range(1, 10))
    return "\n\n".join([
        "You are an AI agent running in the SyntheticCode harness. Help the user accomplish their goals with the "
        "tools you have available. " + filler("identity", 4),
        "# Harness\n" + filler("harness", 30),
        "# Communication\n" + filler("communication", 22),
        "# Working in codebases\n" + filler("codebase", 40),
        "# Code comments\n" + filler("comment", 10),
        "# Your Model\n" + filler("model", 6),
        "<available_skills>\n" + skills + "\n</available_skills>",
        "<env>\n  Working directory: /synthetic/project\n  Platform: synthetic\n"
        "  Today's date: Thu Jan 01 2026\n</env>"])


def _schema(*names):
    return {"type": "object", "properties": {name: {"type": "string", "description": "synthetic " + name}
                                              for name in names}, "required": [names[0]],
            "additionalProperties": False}


OPENCODE_TOOLS = (("edit", 950), ("experimental_image_generate", 628), ("glob", 77), ("grep", 255),
                  ("question", 596), ("read", 489), ("shell", 541), ("skill", 220), ("subagent", 1275),
                  ("webfetch", 287), ("websearch", 216), ("write", 232), ("execute", 1007))


def opencode_tools():
    tools = []
    for name, size in OPENCODE_TOOLS:
        text = ("Synthetic %s tool. " % name + filler(name, 40))[:size]
        tools.append({"type": "function", "function": {"name": name, "description": text,
                                                      "parameters": _schema("path", "content"), "strict": False}})
    return tools


def _call(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def opencode_messages(rounds=4):
    """system, user, then `rounds` tool rounds with parallel calls, reasoning_content and mid-dialog reminders."""
    messages = [{"role": "system", "content": opencode_system()},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "Synthetic greeting.", "reasoning_content": "synthetic thought"},
                {"role": "user", "content": "<system-update>\nSynthetic update the client adds before a task."
                                            "\n</system-update>"},
                {"role": "user", "content": "inspect the synthetic project and report"}]
    for number in range(rounds):
        calls = [_call("call_%d_a" % number, "shell", command="synthetic command %d" % number),
                 _call("call_%d_b" % number, "read", path="/synthetic/file-%d.txt" % number)]
        messages.append({"role": "assistant", "content": None, "reasoning_content": "synthetic thought",
                         "tool_calls": calls})
        messages.append({"role": "tool", "tool_call_id": calls[0]["id"],
                         "content": "synthetic shell output %d" % number})
        messages.append({"role": "tool", "tool_call_id": calls[1]["id"],
                         "content": "synthetic file text %d\n<system-reminder>\nSynthetic read reminder."
                                    "\n</system-reminder>" % number})
    messages.append({"role": "user", "content": "A goal is now active for this synthetic conversation."})
    return messages


def opencode_body(rounds=4, model="claude-opus-5-5", effort="max"):
    return {"model": model, "messages": opencode_messages(rounds), "tools": opencode_tools(), "stream": True,
            "stream_options": {"include_usage": True}, "max_completion_tokens": 128000,
            "reasoning_effort": effort, "name": "Max", "settings": {"reasoningEffort": effort}}


SDK_BILLING = "x-anthropic-billing-header: cc_version=2.1.309.synthetic; cc_entrypoint=sdk-cli; cch=00000;"
SDK_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
SDK_TOOLS = (("Agent", 1500), ("Bash", 1100), ("Edit", 360), ("Glob", 140), ("Grep", 477), ("ListAgents", 777),
             ("Read", 790), ("ReportFindings", 574), ("ScheduleWakeup", 1400), ("Skill", 900), ("ToolSearch", 953),
             ("Workflow", 1500), ("DeferredToolPlaceholder", 83), ("Write", 240))


def claude_sdk_system():
    body = "\n\n".join(["# Harness\n" + filler("sdk harness", 30), "# Session-specific guidance\n" + filler("guidance", 10),
                        "# Memory\n" + filler("memory", 9), "# Environment\n" + filler("environment", 12),
                        "# Context management\n" + filler("context", 8)])
    return [{"type": "text", "text": SDK_BILLING},
            {"type": "text", "text": SDK_IDENTITY, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": body, "cache_control": {"type": "ephemeral"}}]


def claude_sdk_tools():
    tools = []
    for name, size in SDK_TOOLS:
        tool = {"name": name, "description": ("Synthetic %s tool. " % name + filler(name, 40))[:size],
                "input_schema": _schema("path", "content")}
        if name == "DeferredToolPlaceholder":
            tool["defer_loading"] = True
        tools.append(tool)
    return tools


def claude_sdk_body(turns=5, model="claude-opus-5-5"):
    """The Agent SDK runner: three system blocks, a reminder-laden first user message, a role:system entry per turn."""
    first = {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>\nSynthetic attribution reminder. " + filler("attribution", 6)
                                 + "\n</system-reminder>"},
        {"type": "text", "text": "synthetic prefix"},
        {"type": "text", "text": "<system-reminder>\nSynthetic skills reminder.\n</system-reminder>"},
        {"type": "text", "text": "<system-reminder>\n" + filler("claude md", 220) + "\n</system-reminder>"}]}
    messages = [first,
                {"role": "system", "output_config": {"effort": "xhigh"},
                 "content": [{"type": "text", "text": "# Environment\n" + filler("inline environment", 165),
                              "cache_control": {"type": "ephemeral"}}]}]
    for number in range(turns):
        messages += [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                     {"role": "user", "content": [{"type": "text", "text": "synthetic turn %02d" % number}]},
                     {"role": "system", "content": "Synthetic per-turn reminder, number %02d, stable." % number}]
    user_id = json.dumps({"device_id": "synthetic-device", "account_uuid": "",
                          "session_id": "00000000-0000-4000-8000-000000000309"})
    return {"model": model, "max_tokens": 128000, "stream": True, "system": claude_sdk_system(),
            "messages": messages, "tools": claude_sdk_tools(), "metadata": {"user_id": user_id},
            "thinking": {"type": "adaptive", "display": "updates"}, "output_config": {"effort": "xhigh"},
            "context_management": {"edits": [{"type": "clear_thinking_20251015"}]}}


def codex_body(model="gpt-6-sol"):
    """A Responses request in the form Codex sends: instructions, a developer block, AGENTS.md and environment as user items."""
    text = lambda role, value: {"type": "message", "role": role, "content": [{"type": "input_text", "text": value}]}
    return {"model": model, "stream": True, "store": False, "parallel_tool_calls": True, "tool_choice": "auto",
            "instructions": "Synthetic base instructions of the client. " + filler("base", 6),
            "reasoning": {"effort": "high"}, "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": "synthetic-codex-session", "client_metadata": {"synthetic": "1"},
            "tools": [{"type": "function", "name": "shell_command", "description": "Synthetic shell tool.",
                       "parameters": _schema("command")},
                      {"type": "custom", "name": "apply_patch", "description": "Synthetic patch tool.",
                       "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"}}],
            "input": [text("developer", "<permissions instructions>\nSynthetic sandbox rules.\n</permissions instructions>"),
                      text("user", "# AGENTS.md instructions for /synthetic\n\n<INSTRUCTIONS>\n" + filler("agents", 8)
                                   + "\n</INSTRUCTIONS>"),
                      text("user", "<environment_context>\n  <cwd>/synthetic</cwd>\n</environment_context>"),
                      text("user", "synthetic task")]}


# What OpenCode's hidden agents send: tool-less, the agent's own prompt as the only system, one user message.
OPENCODE_TITLE_V1 = ("You are a title generator. You output ONLY a thread title. Nothing else.\n\n<task>\nGenerate a "
                     "brief title that would help the user find this conversation later.\n</task>\n\n<rules>\n"
                     "- synthetic rule\n</rules>")
OPENCODE_TITLE_V2 = ("You name conversations so the user can find them later in a list of past sessions.\n\n"
                     "Reply with a title and nothing else. " + filler("title", 4))
OPENCODE_SUMMARY = ("Summarize what was done in this conversation. Write like a pull request description.\n\n"
                    "Rules:\n- 2-3 sentences max\n- synthetic rule")
OPENCODE_COMPACTION_V1 = ("You are an anchored context summarization assistant for coding sessions.\n\n"
                          "Summarize only the conversation history you are given. " + filler("compaction", 3))


def service_body(system, user="Here is the first message of the conversation:\n\n<message>\nhello\n</message>"):
    return {"model": "claude-opus-5-5", "messages": [{"role": "system", "content": system},
                                                      {"role": "user", "content": user}],
            "stream": True, "stream_options": {"include_usage": True}, "max_completion_tokens": 32000}


def clone(value):
    return copy.deepcopy(value)
