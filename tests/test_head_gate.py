"""Requests that go at the same moment with one head nobody has cached: the first writes it, the others read it.

Claude Code starts several sub-agents of a kind in one message; each first request carries the same 23k-token head (tools
and system), and each wrote it to the cache (cc6707c87d82, 1a360b011428 in the Claude Code test run of 2026-11-07, about
1.1 credits per extra agent). A cache entry can be read only once the response that writes it has begun, so the others
wait for the first one's response to begin (at most HEAD_WAIT), then go."""
import asyncio
import threading
import time
import uuid

import direct
from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture


def key():
    return "test-head-" + uuid.uuid4().hex


def test_the_first_request_leads_and_one_at_the_same_moment_follows():
    head, now = key(), time.monotonic()
    role, gate = direct._claim_head(head, now, owner=1)
    assert role == "lead"
    assert direct._claim_head(head, now + 1, owner=2) == ("follow", gate)
    assert direct._claim_head(head, now + 1, owner=1)[0] == "lead"  # a retry of the leader's own turn never waits for itself


def test_a_head_whose_response_began_is_cached_for_the_warm_window_and_then_led_again():
    head = key()
    role, gate = direct._claim_head(head, time.monotonic(), owner=1)
    direct._head_started(gate, head)
    assert direct._claim_head(head, time.monotonic(), owner=2) == ("cached", gate)
    assert direct._claim_head(head, time.monotonic() + direct.HEAD_WARM + 1, owner=3)[0] == "lead"


def test_a_leader_that_got_no_response_releases_its_followers_and_the_next_request_leads():
    head = key()
    _, gate = direct._claim_head(head, time.monotonic(), owner=1)
    assert direct._claim_head(head, time.monotonic(), owner=2)[0] == "follow"
    direct._head_released(head, gate)
    assert asyncio.run(direct._wait_for_head(gate, 1.0)) == "leader_failed"
    assert direct._claim_head(head, time.monotonic(), owner=3)[0] == "lead"


def test_a_follower_does_not_wait_longer_than_its_limit_or_for_a_stale_leader():
    head, now = key(), time.monotonic()
    _, gate = direct._claim_head(head, now, owner=1)
    started = time.monotonic()
    assert asyncio.run(direct._wait_for_head(gate, 0.2)) == "timeout" and time.monotonic() - started < 1.0
    assert direct._claim_head(head, now + direct.HEAD_STALE + 1, owner=2)[0] == "lead"


def test_two_sub_agents_started_together_send_their_first_requests_one_after_the_others_response_began(
        prompt_relay, monkeypatch):
    relay = prompt_relay()
    posted = []
    answer = direct.urllib.request.urlopen

    def slow_first(request_, **kwargs):
        posted.append(time.monotonic())
        if len(posted) == 1:
            time.sleep(0.6)  # the leader's prompt is being processed: its response has not begun
        return answer(request_, **kwargs)

    monkeypatch.setattr(direct.urllib.request, "urlopen", slow_first)
    # a head as large as a real one (it must be big enough to be cached, direct.HEAD_MIN_CHARS)
    system = "You are a file search specialist for Claude Code. " + uuid.uuid4().hex + " " + "Search the code. " * 150
    statuses = []

    def agent(task, session):
        body = client_request("anthropic", [{"role": "user", "content": task}], system=system)
        headers = {"user-agent": "claude-cli/2.1.310 (external, cli)", "x-claude-code-session-id": session,
                   "x-claude-code-agent-id": "agent-" + session}
        statuses.append(relay.post("anthropic", body, headers)[0])

    mark = relay.mark()
    first = threading.Thread(target=agent, args=("Find the parser.", "s1"))
    second = threading.Thread(target=agent, args=("Find the tests.", "s2"))
    first.start()
    time.sleep(0.15)
    second.start()
    first.join(20)
    second.join(20)
    assert statuses == [200, 200]
    assert len(posted) == 2 and posted[1] - posted[0] >= 0.55  # the second went once the first's response had begun
    waits = relay.events(mark, "cache.head_wait")
    assert len(waits) == 1 and waits[0]["data"]["outcome"] == "leader_started"


def test_a_request_alone_or_after_the_head_was_written_does_not_wait(prompt_relay):
    relay = prompt_relay()
    system = "You are a planner for Claude Code. " + uuid.uuid4().hex
    mark = relay.mark()
    for task in ("Plan the parser.", "Plan the tests."):
        body = client_request("anthropic", [{"role": "user", "content": task}], system=system)
        assert relay.post("anthropic", body, {"x-claude-code-session-id": task})[0] == 200
    assert relay.events(mark, "cache.head_wait") == []


def _attributed(fingerprint, text):
    """Claude Code's system: the attribution line of its conversation, then the text every sub-agent of a kind shares."""
    line = "x-anthropic-billing-header: cc_version=2.1.310.%s; cc_entrypoint=cli; cc_is_subagent=true;" % fingerprint
    return [{"type": "text", "text": line}, {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _head(system, tools=None):
    body = {"model": "claude-opus-5-5", "tools": tools, "messages": [{"role": "system", "content": system}]}
    return direct._head_key(body)


def test_sub_agents_whose_attribution_lines_differ_share_one_head():
    shared = "You are a file search specialist for Claude Code. " + "Search the code. " * 150
    first = [{"type": "text", "text": block["text"], "cache": "cache_control" in block}
             for block in _attributed("2e4", shared)]
    second = [{"type": "text", "text": block["text"], "cache": "cache_control" in block}
              for block in _attributed("6cd", shared)]
    assert _head(first) is not None and _head(first) == _head(second)
    other = [dict(block) for block in second]
    other[1]["text"] = shared + " a different kind of agent"
    assert _head(other) != _head(first)


def test_a_head_too_small_to_be_cached_is_not_gated():
    tiny = [{"type": "text", "text": "Be brief.", "cache": True}]
    assert _head(tiny) is None
    # the same small system with a large list of tools is worth a cache entry, and is gated
    tools = [{"name": "tool_%d" % n, "description": "Does a thing. " * 20, "input_schema": {"type": "object"}}
             for n in range(10)]
    assert _head(tiny, tools) is not None
