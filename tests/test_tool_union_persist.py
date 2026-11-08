"""The union of a client's tools is kept between relay runs, and Claude Code is one of its clients.

After a relay restart the union was empty, and the first request of a Codex process declared fewer tools than the stored
branch of its conversation: tools 91 -> 100, a reset and 32,599 tokens written (the owner's Codex Desktop at 12:18:31 on
2026-11-07, 4e298b6eb041). Claude Code connects an MCP server after its first request in an interactive session (59 -> 90
tools, 107,650 tokens written, 6fe5c86b), so it gets the union too, with its kinds of agent kept apart."""
import json
from types import SimpleNamespace

import tool_union
from tool_union import ToolUnion, client_key, project_of


def tool(name):
    return {"type": "function", "function": {"name": name, "description": "does " + name, "parameters": {"type": "object"}}}


def names(tools):
    return [item["function"]["name"] for item in tools]


KEY = ("codex", "codex_exec", "gpt-6.1-sol", "root", "c:\\work\\proj", "", 0, "")


def test_the_union_is_kept_on_disk_and_read_back_by_the_next_run(tmp_path):
    path = str(tmp_path / tool_union.FILE_NAME)
    first = ToolUnion(path=path)
    first.apply(KEY, [tool("a"), tool("b"), tool("late")])
    second = ToolUnion(path=path)  # the relay started again
    merged, carried = second.apply(KEY, [tool("a"), tool("b")])
    assert names(merged) == ["a", "b", "late"] and carried == ["late"]


def test_a_stored_tool_not_declared_for_the_ttl_is_not_read_back(tmp_path):
    path = str(tmp_path / tool_union.FILE_NAME)
    now = [1000.0]
    ToolUnion(ttl=100, path=path, clock=lambda: now[0]).apply(KEY, [tool("a"), tool("old")])
    now[0] += 50
    ToolUnion(ttl=100, path=path, clock=lambda: now[0]).apply(KEY, [tool("a")])  # "a" declared again, "old" carried
    now[0] += 80  # "old" was last declared 130 s ago
    merged, carried = ToolUnion(ttl=100, path=path, clock=lambda: now[0]).apply(KEY, [tool("a")])
    assert carried == [] and names(merged) == ["a"]


def test_a_file_that_cannot_be_read_starts_an_empty_union(tmp_path):
    path = tmp_path / tool_union.FILE_NAME
    path.write_text("{not json", encoding="utf-8")
    merged, carried = ToolUnion(path=str(path)).apply(KEY, [tool("a")])
    assert carried == [] and names(merged) == ["a"]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1  # and the next save replaces it


def test_without_a_path_nothing_is_written(tmp_path):
    ToolUnion().apply(KEY, [tool("a")])
    assert list(tmp_path.iterdir()) == []


def test_a_table_whose_only_change_is_the_time_is_written_at_most_every_minute(tmp_path, monkeypatch):
    path = tmp_path / tool_union.FILE_NAME
    now = [1000.0]
    union = ToolUnion(path=str(path), clock=lambda: now[0])
    union.apply(KEY, [tool("a")])
    written = path.stat().st_mtime_ns
    path.write_text(json.dumps({"version": 1, "clients": []}), encoding="utf-8")  # a marker: was it written again?
    now[0] += 5
    union.apply(KEY, [tool("a")])
    assert json.loads(path.read_text(encoding="utf-8"))["clients"] == []
    now[0] += tool_union.SAVE_EVERY
    union.apply(KEY, [tool("a")])
    assert json.loads(path.read_text(encoding="utf-8"))["clients"] != [] and written


def test_claude_code_names_its_folder_in_a_bullet_of_its_environment_block():
    environment = ("<env>\nYou have been invoked in the following environment:\n - Primary working directory: C:\\Work\\Proj\n"
                   " - Is a git repository: true\n</env>")
    assert project_of(SimpleNamespace(system="", prompt=environment)) == "c:\\work\\proj"
    assert project_of(SimpleNamespace(system="<env>\n  Working directory: D:\\x\n</env>", prompt="")) == "d:\\x"
    assert project_of(SimpleNamespace(system="", prompt="<cwd>C:/Work/Other/</cwd>")) == "c:\\work\\other"


def test_claude_code_keeps_its_kinds_of_agent_apart_and_codex_keys_do_not_change_with_the_system():
    headers = {"x-claude-code-agent-id": "agent-1"}
    general = SimpleNamespace(tools=[tool("a")], model="m", system="You are an agent for Claude Code. Do the task fully.",
                              prompt="")
    explore = SimpleNamespace(tools=[tool("a")], model="m", system="You are a file search specialist for Claude Code.",
                              prompt="")
    assert client_key("claude-code", headers, general) != client_key("claude-code", headers, explore)
    assert client_key("claude-code", headers, general)[3] == "sub"
    assert client_key("codex", {}, general) == client_key("codex", {}, explore)


def test_the_attribution_line_of_claude_code_does_not_split_its_sessions():
    """Its version ends in three characters drawn from the session's first message: with it in the key no two sessions
    shared a table, and every session pushed another client's table out of the bounded union."""
    def parsed(suffix, rest="You are Claude Code, Anthropic's official CLI for Claude."):
        return SimpleNamespace(tools=[tool("a")], model="m", prompt="", system=(
            "x-anthropic-billing-header: cc_version=2.1.310.%s; cc_entrypoint=cli;\n%s" % (suffix, rest)))
    assert client_key("claude-code", {}, parsed("2e4")) == client_key("claude-code", {}, parsed("6cd"))
    assert client_key("claude-code", {}, parsed("2e4")) != client_key(
        "claude-code", {}, parsed("2e4", "You are a file search specialist for Claude Code."))


def test_claude_code_is_a_client_of_the_union_by_default():
    import config
    assert config.DEFAULTS["tool_union_clients"] == ["codex", "claude-code"]


class _Snapshot:
    """What client_key reads of a prompts snapshot: the normalized profile, its tools section."""

    def __init__(self, profiles):
        self._profiles = profiles

    def profile(self, name):
        return self._profiles[name]


def _key(plan):
    from types import SimpleNamespace

    import tool_union
    parsed = SimpleNamespace(tools=[{"type": "function", "function": {"name": "a"}}], model="gpt-6.1-sol",
                             system="", prompt="", prompt_plan=plan)
    return tool_union.client_key("codex", {"originator": "codex_exec"}, parsed)


def test_the_union_key_survives_a_restart_that_renumbers_the_prompts_revision():
    from types import SimpleNamespace
    profiles = {"p": {"tools": {"edits": [{"hide": "c"}]}}}
    before = SimpleNamespace(profile="p", rev=2, snapshot=_Snapshot(profiles))   # after one save of prompts.json
    after = SimpleNamespace(profile="p", rev=1, snapshot=_Snapshot(profiles))    # the same document after a restart
    assert _key(before) == _key(after)


def test_documents_that_edit_tools_differently_never_share_a_union():
    from types import SimpleNamespace
    first = SimpleNamespace(profile="p", rev=1, snapshot=_Snapshot({"p": {"tools": {"edits": []}}}))
    second = SimpleNamespace(profile="p", rev=1, snapshot=_Snapshot({"p": {"tools": {"edits": [{"hide": "c"}]}}}))
    assert _key(first) != _key(second)
    legacy = SimpleNamespace(profile=None, rev=3, snapshot=None)
    assert _key(legacy)[6] == ""
