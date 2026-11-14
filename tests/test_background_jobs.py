"""A tool-less request of Codex for a strict schema is one of its own one-shot jobs: the memory extraction of an old thread.

Codex run of 2026-11-07: three memory jobs of the owner's Codex Desktop (c5e63ab5, 82de8310, 63e5b00f: 80 and 81 messages, no
tools, 192k-211k tokens each) ran on one pool session one after another, and the third was kept warm by three pings of its
209k-token body (0.63 credits) until the pool evicted it; nothing ever continued it. Such a job is now not kept warm, keeps
no branch, writes nothing past what it reads and leaves the session it runs on as it was."""
import client_context
from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_compaction_cache import Chat, count
from test_compaction_tail_cache import flags
from test_native_history import tool_reply
from test_review_fixes_pr19 import END, text

CODEX = {"originator": "Codex Desktop",
         "user-agent": "Codex Desktop/0.167.0-alpha.3.2 (Windows 10.0.26300; x86_64) unknown (Codex Desktop; 26.1103.31037)"}
DESKTOP = {**CODEX, "session-id": "memory-session"}
SCHEMA = {"type": "object", "properties": {"raw_memory": {"type": "string"}, "rollout_summary": {"type": "string"}},
          "required": ["raw_memory", "rollout_summary"], "additionalProperties": False}
VALID = '{"raw_memory":"notes","rollout_summary":"summary"}'
STAGE_ONE = "You are a memory extraction agent. Read the rollout below and write down what is worth remembering."
# A request without tools runs on the text session of its conversation (the pool keeps it apart from the one with tools).
TEXT_SESSION = ("header:memory-session", "text")


def job(thread, *, model="gpt-6.1-terra", schema=True):
    rollout = [{"role": "user", "content": "rollout %s, turn %d: %s" % (thread, number, "details " * 40)}
               for number in range(6)]
    extra = {"text": {"format": {"type": "json_schema", "name": "codex_output_schema", "strict": True, "schema": SCHEMA}}}
    return client_request("responses", rollout, system=STAGE_ONE, tools=False, model=model, **(extra if schema else {}))


def run_job(relay, thread, headers=DESKTOP, **options):
    relay.scripts.append([text(VALID), *END])
    status, payload, answer = relay.post("responses", job(thread, **options), headers)
    assert status == 200, payload
    relay.settle(answer)
    return relay.posts[-1]


def tasks(relay, mark):
    return [event["data"].get("client_task") for event in relay.events(mark, "turn.begin")]


def state(session):
    history = getattr(session, "_native_history", None)
    return (session._warm_body, list(history.branches) if history else None, getattr(session, "_last_cache_key", None),
            getattr(session, "_turn_outcome", None), list(getattr(session, "_cached_turn_ends", [])),
            getattr(session, "_warm_blocked", None))


def test_memory_jobs_are_neither_kept_warm_nor_kept_and_write_nothing_past_the_head(prompt_relay):
    relay = prompt_relay(cache_keepwarm_seconds=240)
    mark = relay.mark()
    bodies = [run_job(relay, "t1"), run_job(relay, "t2")]
    assert tasks(relay, mark) == [client_context.BACKGROUND] * 2
    session = relay.runtime.pool._conversations[TEXT_SESSION]
    history = getattr(session, "_native_history", None)
    assert not (history and history.branches)
    assert session._warm_body is None and getattr(session, "_warm_task", None) is None
    assert getattr(session, "_turn_outcome", None) is None and not getattr(session, "_cached_turn_ends", [])
    for body in bodies:
        # the head may be cached (every job shares it); the rollout is plain input, read by nobody again
        assert "F" not in "".join(flags(body)[1:])
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "cache.key_changed") == 0


def test_a_job_leaves_the_session_it_runs_on_as_it_was(prompt_relay):
    """A tool-less exchange on the text session of the conversation, then a job on the same session, then the exchange
    goes on: the job reset nothing, moved no cache key and took no keepwarm body, and the exchange resumes."""
    relay = prompt_relay(cache_keepwarm_seconds=240)
    system = "You answer in one line."
    messages = [{"role": "user", "content": "first question"}]

    def ask(script):
        relay.scripts.append(script)
        status, payload, answer = relay.post("responses", client_request(
            "responses", messages, system=system, tools=False, model="claude-sonnet-5-5"), DESKTOP)
        assert status == 200, payload
        relay.settle(answer)
        return payload, relay.posts[-1]

    payload, first = ask([text("first answer"), *END])
    session = relay.runtime.pool._conversations[TEXT_SESSION]
    before = state(session)
    assert before[0] is not None and before[2] and before[4]  # the exchange is kept warm, with its key and cache points
    mark = relay.mark()
    run_job(relay, "old-thread", model="claude-sonnet-5-5")
    assert tasks(relay, mark) == [client_context.BACKGROUND]
    assert state(session) == before
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "cache.key_changed") == 0
    messages += [{"role": "assistant", "content": payload["output_text"]}, {"role": "user", "content": "second question"}]
    mark = relay.mark()
    _, second = ask([text("second answer"), *END])
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "cache.key_changed") == 0
    assert second["openAICacheKey"] == first["openAICacheKey"]
    assert len(session._warm_body["messages"]) == len(second["messages"])


def test_a_job_does_not_touch_the_chat_of_the_same_codex_session(prompt_relay):
    relay = prompt_relay(cache_keepwarm_seconds=240)
    chat = Chat(relay, "responses", effort="high", headers={**CODEX, "session-id": "shared"}, model="claude-sonnet-5-5")
    first, second = chat.work(2)
    session = relay.runtime.pool._conversations["header:shared"]
    before = state(session)
    mark = relay.mark()
    run_job(relay, "old-thread", headers={**CODEX, "session-id": "shared"}, model="claude-sonnet-5-5")
    assert tasks(relay, mark) == [client_context.BACKGROUND] and state(session) == before
    mark = relay.mark()
    third = chat.ask(tool_reply("c3", "opaque-3"))
    assert count(relay, mark, "native_history.resume") == 1
    assert count(relay, mark, "native_history.reset") == 0 and count(relay, mark, "cache.key_changed") == 0
    assert third["openAICacheKey"] == second["openAICacheKey"] == first["openAICacheKey"]
    assert len(session._warm_body["messages"]) == len(third["messages"])


def test_what_is_no_one_shot_job_keeps_its_task(prompt_relay):
    relay = prompt_relay()
    mark = relay.mark()
    run_job(relay, "t1", schema=False)  # a tool-less request of Codex without a schema: not known to be a job
    opencode = {"user-agent": "opencode/latest/2.2.5/desktop", "x-proxi-client": "opencode", "x-session-affinity": "s1"}
    run_job(relay, "t2", headers=opencode)  # a schema without tools from another client
    assert tasks(relay, mark) == [None, None]


def test_a_codex_chat_turn_with_tools_is_kept_and_warmed_as_before(prompt_relay):
    relay = prompt_relay(cache_keepwarm_seconds=240)
    chat = Chat(relay, "responses", effort="high", headers={**CODEX, "session-id": "chat"}, model="claude-sonnet-5-5")
    mark = relay.mark()
    _, second = chat.work(2)
    assert tasks(relay, mark) == [None, None]
    session = relay.runtime.pool._conversations["header:chat"]
    assert session._native_history.branches and len(session._warm_body["messages"]) == len(second["messages"])
