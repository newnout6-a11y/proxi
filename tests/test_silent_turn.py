import asyncio
import types

import apierr
from orchestrator import TurnRunner

ERROR_MSG = {"senderRole": "sandpack", "content": {"type": "error",
                                                  "error": "You've used all AI credits included in your plan."}}
TEXT_MSG = {"senderRole": "sandpack", "content": {"type": "text", "text": "ok"}}
USER_MSG = {"senderRole": "user", "content": {"type": "text", "text": "hi"}}


def build(messages):
    async def get_messages(thread_id):
        return {"messages": messages}

    pool = types.SimpleNamespace(backend=types.SimpleNamespace(get_messages=get_messages))
    runner = TurnRunner({"workspace_root": ".", "project_context": False, "exec_policy": "off",
                         "context_max_bytes": 100, "max_exec_turns": 0, "cookie_values": ()}, None, pool)
    return runner


class Session:
    thread_id = "t1"


def test_credit_error_from_newest_first_messages():
    runner = build([USER_MSG, ERROR_MSG, TEXT_MSG])
    err = asyncio.run(runner._explain_empty({"session": Session()}))
    assert isinstance(err, apierr.ApiError)
    assert err.status == 402 and err.code == "insufficient_credits"


def test_newer_text_wins_over_older_error():
    runner = build([TEXT_MSG, ERROR_MSG])
    assert asyncio.run(runner._explain_empty({"session": Session()})) is None


def test_no_session_returns_none():
    runner = build([ERROR_MSG])
    assert asyncio.run(runner._explain_empty({})) is None
