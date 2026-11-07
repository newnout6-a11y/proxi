"""The system prompt of a client is held as its conversation has it where the client changes it by itself.

OpenCode 1.x lists a skill that exists in two scanned folders from whichever folder the process met first, and that
changes from process to process (six fresh `opencode run` processes in one folder: five different system prompts,
Opus dialog of 2026-11-06: seven of twelve turns started with native_history.reset and a rewrite of 68k to 196k
tokens, 7 to 21 credits each, 47% of what the build agent cost). The date line of the same prompt flips at midnight."""
import copy
import json
import threading

import pytest

import prompt_stabilizer
from prompt_stabilizer import PinnedDate, SkillLocations, Stabilizer
from prompt_support import after_tool_call, client_request, head_of, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_conversation_state import signatures_in, stored
from test_native_history import tool_reply
from test_review_fixes_pr19 import END, text

AGENTS = "C:\\Users\\someone\\.agents\\skills\\%s\\SKILL.md"
CLAUDE = "C:\\Users\\someone\\.claude\\skills\\%s\\SKILL.md"
DESCRIPTION = {"stageshow": "Build faux product UI that looks like a real product.\nUse it for hero shots.",
               "unslop": "Turns vibecoded, AI-looking UI into a designed interface."}


def system(locations, date="Fri Nov 06 2026", folder="C:\\work\\project", extra=""):
    skills = "".join("  <skill>\n    <name>%s</name>\n    <description>%s</description>\n    <location>%s</location>\n  </skill>\n"
                     % (name, DESCRIPTION[name], location) for name, location in locations.items())
    return ("You are an AI agent running in OpenCode.\n\nSkills provide specialized instructions.\n<available_skills>\n"
            + skills + "</available_skills>\n" + extra
            + "\nHere is some useful information about the environment you are running in:\n<env>\n  Working directory: "
            + folder + "\n  Platform: win32\n  Today's date: " + date + "\n</env>\n")


FIRST = {"stageshow": AGENTS % "stageshow", "unslop": CLAUDE % "unslop"}
OTHER = {"stageshow": CLAUDE % "stageshow", "unslop": AGENTS % "unslop"}


# -- the memory of skill locations ---------------------------------------------------------------------------------

def test_the_first_location_of_a_skill_is_the_one_every_later_prompt_gets():
    memory = SkillLocations(exists=lambda path: True)
    assert memory.stabilize(system(FIRST)) == (system(FIRST), [])
    held, replaced = memory.stabilize(system(OTHER))
    assert held == system(FIRST) and replaced == ["stageshow", "unslop"]
    assert memory.stabilize(system(FIRST)) == (system(FIRST), [])


def test_only_the_location_changes_never_the_name_the_description_or_the_rest_of_the_prompt():
    memory = SkillLocations(exists=lambda path: True)
    memory.stabilize(system(FIRST))
    held, _ = memory.stabilize(system(OTHER, extra="A rule the client added.\n"))
    assert held == system(FIRST, extra="A rule the client added.\n")
    assert held.count("<skill>") == 2 and "Use it for hero shots." in held


def test_a_skill_with_another_description_or_in_another_working_folder_is_another_skill():
    memory = SkillLocations(exists=lambda path: True)
    memory.stabilize(system(FIRST))
    edited = copy.deepcopy(DESCRIPTION)
    try:
        DESCRIPTION["stageshow"] = "A newer description."
        assert memory.stabilize(system(OTHER))[1] == ["unslop"]
    finally:
        DESCRIPTION.update(edited)
    assert memory.stabilize(system(OTHER, folder="C:\\elsewhere")) == (system(OTHER, folder="C:\\elsewhere"), [])


def test_a_location_that_is_not_a_file_path_is_left_alone():
    built_in = {"stageshow": "&lt;built-in&gt;", "unslop": CLAUDE % "unslop"}
    memory = SkillLocations(exists=lambda path: True)
    memory.stabilize(system(built_in))
    assert memory.stabilize(system({"stageshow": AGENTS % "stageshow", "unslop": CLAUDE % "unslop"}))[1] == []


def test_a_remembered_file_that_is_gone_gives_way_to_the_one_that_exists():
    present = {FIRST["stageshow"], FIRST["unslop"], OTHER["stageshow"], OTHER["unslop"]}
    memory = SkillLocations(exists=lambda path: path in present)
    memory.stabilize(system(FIRST))
    present -= {FIRST["stageshow"]}
    held, replaced = memory.stabilize(system(OTHER))
    assert replaced == ["unslop"] and OTHER["stageshow"] in held and FIRST["unslop"] in held
    assert memory.stabilize(system(OTHER))[1] == ["unslop"]  # and the new one is the one remembered now


def test_the_memory_expires_is_bounded_and_survives_a_restart(tmp_path):
    now = [1000.0]
    memory = SkillLocations(ttl=100, max_entries=2, clock=lambda: now[0], exists=lambda path: True)
    memory.stabilize(system(FIRST))
    now[0] += 101
    assert memory.stabilize(system(OTHER))[1] == []  # forgotten: the next one seen is the first
    for number in range(4):
        memory.stabilize(system(FIRST, folder="C:\\p%d" % number))
    assert len(memory._memory) <= 2
    path = str(tmp_path / prompt_stabilizer.FILE_NAME)
    kept = SkillLocations(path, exists=lambda path: True)
    kept.stabilize(system(FIRST))
    assert json.loads((tmp_path / prompt_stabilizer.FILE_NAME).read_text(encoding="utf-8"))["version"] == 1
    again = SkillLocations(path, exists=lambda path: True)
    assert again.stabilize(system(OTHER)) == (system(FIRST), ["stageshow", "unslop"])


def test_a_damaged_memory_file_is_ignored(tmp_path):
    path = tmp_path / prompt_stabilizer.FILE_NAME
    for content in ("not json", '{"skills": 7}', '{"skills": [[1, 2]]}'):
        path.write_text(content, encoding="utf-8")
        memory = SkillLocations(str(path))
        assert memory.stabilize(system(FIRST)) == (system(FIRST), [])


def test_a_prompt_with_no_skills_is_returned_as_it_is():
    assert SkillLocations().stabilize("You are an agent.") == ("You are an agent.", [])


def test_requests_that_learn_skills_at_the_same_time_leave_the_newest_memory_on_disk(tmp_path, monkeypatch):
    """Two request threads wrote one temporary file at once: the rename of one failed while the other held the file open
    (Windows), and the file put in place was the older document written over the start of the newer one."""
    path = tmp_path / prompt_stabilizer.FILE_NAME
    memory = SkillLocations(str(path), exists=lambda path: True)
    writing, go = threading.Event(), threading.Event()
    real_open = open

    class SlowDisk:  # the first write of the file waits, as a slow disk makes the thread writing it wait
        def __init__(self, handle):
            self.handle = handle

        def write(self, data):
            if not writing.is_set():
                writing.set()
                assert go.wait(10)
            return self.handle.write(data)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.handle.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.handle, name)

    monkeypatch.setattr(prompt_stabilizer, "open", lambda *args, **kwargs: SlowDisk(real_open(*args, **kwargs)),
                        raising=False)
    first = threading.Thread(target=memory.stabilize, args=(system(FIRST),))
    first.start()
    assert writing.wait(10)
    second = threading.Thread(target=memory.stabilize, args=(system(FIRST, folder="C:\\work\\other"),))
    second.start()
    second.join(10)  # it does not wait for the disk: its change is left to the thread that is writing
    go.set()
    first.join(10)
    assert not first.is_alive() and not second.is_alive()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert sorted((folder, name) for folder, name, *_ in document["skills"]) == [
        ("C:\\work\\other", "stageshow"), ("C:\\work\\other", "unslop"),
        ("C:\\work\\project", "stageshow"), ("C:\\work\\project", "unslop")]
    assert list(tmp_path.glob("*.tmp")) == []


def test_half_of_a_surrogate_pair_in_a_skill_fails_neither_the_prompt_nor_the_file(tmp_path, monkeypatch):
    """A client that cuts a description inside an emoji sends half of the pair as a JSON escape: encoding it for the digest
    raised, and every request of that client went unstabilized."""
    monkeypatch.setitem(DESCRIPTION, "stageshow", "Build faux product UI \ud83d")
    cut = {"stageshow": AGENTS % "stageshow\ud83d", "unslop": CLAUDE % "unslop"}
    path = tmp_path / prompt_stabilizer.FILE_NAME
    assert SkillLocations(str(path), exists=lambda path: True).stabilize(system(cut)) == (system(cut), [])
    again = SkillLocations(str(path), exists=lambda path: True)
    moved = {"stageshow": CLAUDE % "stageshow", "unslop": AGENTS % "unslop"}
    assert again.stabilize(system(moved)) == (system(cut), ["stageshow", "unslop"])


# -- the date --------------------------------------------------------------------------------------------------------

def test_a_conversation_keeps_the_date_it_began_with_until_it_has_been_idle_for_a_while():
    now = [0.0]
    dates = PinnedDate(ttl=1200, clock=lambda: now[0])
    day_one, day_two = system(FIRST, date="Fri Nov 06 2026"), system(FIRST, date="Sat Nov 07 2026")
    assert dates.pin(day_one, "dialog") == (day_one, False)
    now[0] += 600
    assert dates.pin(day_two, "dialog") == (day_one, True)
    now[0] += 1200 + 1
    assert dates.pin(day_two, "dialog") == (day_two, False)  # idle past the cache: the new day is the date now
    assert dates.pin(day_two, "another dialog") == (day_two, False)
    assert dates.pin(day_two, None) == (day_two, False)


def test_the_stabilizer_does_each_thing_only_when_asked():
    plain = Stabilizer({})
    assert plain.dates is None and plain.skills is not None
    off = Stabilizer({"stabilize_client_prompts": False})
    assert off.skills is None
    dated = Stabilizer({"stable_prompt_date": True})
    first = system(FIRST, date="Fri Nov 06 2026")
    assert dated.apply(first, "dialog") == (first, [])
    assert dated.apply(system(OTHER, date="Sat Nov 07 2026"), "dialog") == (
        system(FIRST, date="Fri Nov 06 2026"), ["skill_locations:stageshow,unslop", "date"])


# -- the request path --------------------------------------------------------------------------------------------------

OPENCODE = {"user-agent": "opencode/1.19.3 ai-sdk/provider-utils/3.0.0 runtime/bun/1.3.14", "x-session-affinity": "ses_one"}
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]


def two_processes(relay, first_system, second_system):
    messages, sent = copy.deepcopy(USER), []
    for number, (script, prompt) in enumerate(((tool_reply("c1", "opaque-one"), first_system), ([text("done"), *END], second_system))):
        relay.scripts.append(script)
        status, payload, _ = relay.post("chat", client_request("chat", messages, system=prompt), OPENCODE)
        assert status == 200, payload
        sent.append(relay.posts[-1])
        if number == 0:
            messages = after_tool_call("chat", messages, payload)
    return sent


def test_a_new_opencode_process_that_lists_the_skills_from_the_other_folder_keeps_the_signed_history(prompt_relay, tmp_path):
    relay = prompt_relay()
    mark = relay.mark()
    sent = two_processes(relay, system(FIRST), system(OTHER))
    assert head_of(sent[0]) == head_of(sent[1]) and AGENTS % "stageshow" in head_of(sent[1])
    assert len(relay.events(mark, "native_history.resume")) == 1 and not relay.events(mark, "native_history.reset")
    assert not relay.events(mark, "cache.head_changed")
    assert signatures_in(sent[1]) == ["opaque-one"]
    (done,) = stored(mark, "prompt.stabilized")
    assert done["data"]["done"] == ["skill_locations:stageshow,unslop"] and "someone" not in json.dumps(done)


def test_the_remembered_locations_survive_a_restart_of_the_relay(prompt_relay, tmp_path):
    folder = tmp_path / "state"
    relay = prompt_relay(conversation_state_dir=str(folder))
    two_processes(relay, system(FIRST), system(FIRST))
    assert (folder / prompt_stabilizer.FILE_NAME).exists()
    again = prompt_relay(conversation_state_dir=str(folder))
    sent = two_processes(again, system(OTHER), system(OTHER))
    assert AGENTS % "stageshow" in head_of(sent[0]) and head_of(sent[0]) == head_of(sent[1])


def test_switched_off_the_head_follows_the_client_and_the_history_restarts(prompt_relay):
    relay = prompt_relay(stabilize_client_prompts=False)
    mark = relay.mark()
    sent = two_processes(relay, system(FIRST), system(OTHER))
    assert head_of(sent[0]) != head_of(sent[1])
    assert relay.events(mark, "native_history.reset") and not stored(mark, "prompt.stabilized")


@pytest.mark.parametrize("asked,expected", [(False, "Sat Nov 07 2026"), (True, "Fri Nov 06 2026")])
def test_the_date_is_held_only_when_asked_for(prompt_relay, asked, expected):
    relay = prompt_relay(stable_prompt_date=asked)
    sent = two_processes(relay, system(FIRST, date="Fri Nov 06 2026"), system(FIRST, date="Sat Nov 07 2026"))
    assert "Today's date: " + expected in head_of(sent[1])
    assert (head_of(sent[0]) == head_of(sent[1])) is asked
