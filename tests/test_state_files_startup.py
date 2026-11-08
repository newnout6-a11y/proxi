"""A file the relay keeps beside the stored conversations that it cannot use never keeps the relay from starting."""
import json

import pytest

import compaction_tools
import prompt_stabilizer
import tool_union

BAD = [
    json.dumps({"version": 1, "lists": [], "chats": [["header:a", "m", "d", 1.0]], "clients": [[["k"], []]],
                "skills": [["f", "n", "d", "l", 1.0]]}),            # another layout
    json.dumps({"version": 1, "lists": {"d": [{"type": "function"}]}, "chats": [["header:a", "m", "d", 10 ** 400]],
                "clients": [[["k"], [["a", {}, 10 ** 400]]]], "skills": [["f", "n", "d", "l", 10 ** 400]]}),  # a huge number
    "[" * 100000,                                                       # nesting past the decoder's limit
]


@pytest.mark.parametrize("text", BAD, ids=["another-layout", "huge-number", "deep-nesting"])
def test_the_chat_tools_the_tool_union_and_the_skill_locations_start_empty_from_a_file_they_cannot_use(tmp_path, text):
    for name in ("chat-tools.json", "tool-union.json", "skills.json"):
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert len(compaction_tools.ChatTools(path=str(tmp_path / "chat-tools.json"))._chats) == 0
    assert len(tool_union.ToolUnion(path=str(tmp_path / "tool-union.json"))) == 0
    assert len(prompt_stabilizer.SkillLocations(str(tmp_path / "skills.json"))._memory) == 0
