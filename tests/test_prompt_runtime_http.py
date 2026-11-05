"""The prompt hooks of proxy._completion over real HTTP: plan, refusals, failures, pass 2, generation, protected jobs."""
import collections
import copy
import json

import pytest

import apierr
import client_context
import direct
import prompt_body
import prompt_policy
import validate
from prompt_bodies import (CLAUDE_SDK_HEADERS, OPENCODE_HEADERS, OPENCODE_SUMMARY, OPENCODE_TITLE_V1,
                           OPENCODE_TITLE_V2, filler, opencode_body, service_body)
from prompt_support import (PATHS, FakePlan, FakePrompts, account, client_request, fake_relay, head_of,  # noqa: F401
                            profile, prompt_relay)

DIALECTS = ["chat", "anthropic", "responses"]
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]
MINE = "{{text:rules}}\n\n{{client_system}}"
RULES = "MY_RULES: answer in Russian."


def mine(store, **options):
    """Publish one revision whose default profile puts the user's rules before the client's system."""
    store.publish(1, {"mine": profile(MINE, texts={"rules": RULES}, **options)}, default_profile="mine")
    return store


def affinity(name="dialog-1"):
    return {"x-session-affinity": name}


class Spy:
    """Delegates to a real store and keeps what the runtime asked of it."""

    def __init__(self, store):
        self.store, self.inputs, self.plans, self.order = store, [], [], []

    def maybe_reload(self):
        self.order.append("reload")
        return self.store.maybe_reload()

    def plan(self, inp):
        self.order.append("plan")
        self.inputs.append(inp)
        self.plans.append(self.store.plan(inp))
        return self.plans[-1]

    def __getattr__(self, name):
        return getattr(self.store, name)


def kinds(events):
    return collections.Counter(event["kind"] for event in events)


# -- no prompts file: nothing changes -----------------------------------------------------------------------------
@pytest.mark.parametrize("dialect", DIALECTS)
def test_without_a_prompts_file_the_head_the_headers_and_the_journal_are_what_they_were(prompt_relay, dialect):
    relay = prompt_relay()
    assert relay.runtime.prompts.snapshot() is None
    request = client_request(dialect, USER, system="CLIENT_SYSTEM", tools=False)
    runner = relay.runtime.runner

    # The layer absent altogether is the baseline of the code before this feature.
    relay.runtime.prompts = runner.prompts = None
    mark = relay.mark()
    status_before, _, headers_before = relay.post(dialect, request)
    relay.settle(headers_before)
    events_before = relay.events(mark)

    spy = relay.prompts = relay.runtime.prompts = runner.prompts = Spy(prompt_policy.PromptStore(relay.runtime.cfg))
    mark = relay.mark()
    status_after, _, headers_after = relay.post(dialect, request)
    relay.settle(headers_after)
    events_after = relay.events(mark)

    assert status_before == status_after == 200
    assert len(relay.posts) == 2 and relay.posts[0] == relay.posts[1]
    assert head_of(relay.posts[1]) == "CLIENT_SYSTEM"
    assert kinds(events_before) == kinds(events_after)
    assert not relay.events(mark, "prompt.")
    skip = {"x-request-id", "request-id", "date", "content-length"}
    assert ({k.lower(): v for k, v in headers_before.items() if k.lower() not in skip}
            == {k.lower(): v for k, v in headers_after.items() if k.lower() not in skip})
    # The store is asked every time, file or not: the pin a conversation gets there decides what a later file means.
    assert spy.order == ["reload", "plan"] and spy.plans[0].legacy and spy.plans[0].profile is None


def test_a_legacy_plan_leaves_the_request_and_its_trace_untouched(prompt_relay, tmp_path):
    relay = prompt_relay(client_trace_dir=str(tmp_path / "traces"))
    spy = relay.prompts = relay.runtime.prompts = relay.runtime.runner.prompts = Spy(relay.runtime.prompts)
    status, _, _ = relay.post("chat", client_request("chat", USER, tools=False))
    assert status == 200 and spy.plans[0].legacy
    timelines = list((tmp_path / "traces").glob("*/timeline.jsonl"))
    assert timelines
    validated = [json.loads(line) for line in timelines[0].read_text(encoding="utf-8").splitlines()
                 if '"validated_request"' in line]
    # The prompt layer's attributes (prompt_*) are not part of what the client validated.
    assert validated and not any(key.startswith("prompt_") for key in validated[0]["parsed"])
    assert "request_purpose" in validated[0]["parsed"]


def test_a_revision_no_rule_of_which_matches_is_the_legacy_path(prompt_relay):
    relay = fake_relay(prompt_relay)
    # a file that has a profile and a rule, but no default profile and a rule this request does not meet
    relay.prompts.publish(1, {"mine": profile(MINE, texts={"rules": RULES})},
                          rules=[("mine", lambda inp: inp.client == "opencode")])
    request = client_request("chat", USER, system="CLIENT_SYSTEM", tools=False)
    store, relay.runtime.prompts, relay.runtime.runner.prompts = relay.prompts, None, None
    assert relay.post("chat", request, {"user-agent": "other/1"})[0] == 200  # the code of before the layer
    relay.runtime.prompts = relay.runtime.runner.prompts = store
    mark = relay.mark()
    assert relay.post("chat", request, {"user-agent": "other/1", **affinity()})[0] == 200
    assert relay.posts[1]["messages"] == relay.posts[0]["messages"] and head_of(relay.posts[1]) == "CLIENT_SYSTEM"
    (plan,) = store.plans
    assert plan.legacy and plan.rev == 1 and not relay.events(mark, "prompt.plan") and store.records == []
    assert ("header:dialog-1", "generation") in store.pins  # the conversation is pinned all the same


def test_the_store_is_reloaded_and_then_asked_for_every_request(prompt_relay):
    relay = fake_relay(prompt_relay)
    for _ in range(2):
        assert relay.post("chat", client_request("chat", USER, tools=False))[0] == 200
    assert relay.prompts.order == ["reload", "plan"] * 2


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_plan_is_asked_for_with_what_the_request_is(prompt_relay, dialect):
    relay = fake_relay(prompt_relay)
    request = client_request(dialect, USER, system="CLIENT_SYSTEM")
    headers = {**affinity("ses-1"), "user-agent": "opencode/2.2.5", "x-proxi-client": "opencode",
               "x-proxi-agent": "build", "x-proxi-profile": "mine"}
    assert relay.post(dialect, request, headers)[0] == 200
    (inp,) = relay.prompts.inputs
    assert inp == prompt_policy.PlanInput(
        dialect=dialect, client="opencode", agent="build", purpose="client-tools", request_class="",
        model="claude-opus-5-5", requested_model="claude-opus-5-5", virtual_profile=None, header_profile="mine",
        tools=True, structured=False, has_client_system=True, conversation_key="header:ses-1")
    assert relay.posts[0]["messages"][0]["role"] == "system"


def test_the_plan_sees_the_prompt_purpose_and_the_legacy_purpose_stays_what_the_journal_says(prompt_relay):
    relay = fake_relay(prompt_relay)
    mark = relay.mark()
    body = service_body(OPENCODE_TITLE_V2)
    assert relay.post("chat", body, {"user-agent": "opencode/2.2.5"})[0] == 200
    (inp,) = relay.prompts.inputs
    assert inp.purpose == "client-service" and inp.client == "opencode" and not inp.tools
    (begin,) = relay.events(mark, "turn.begin")
    assert begin["data"]["request_purpose"] == "client-text"


# -- refusals of the engine reach the client ----------------------------------------------------------------------
REFUSALS = [
    pytest.param(lambda inp: apierr.ApiError(400, "invalid_request_error",
                                             "x-proxi-profile names the profile 'ghost', which does not exist",
                                             param="x-proxi-profile"), 400, "invalid_request_error",
                 "x-proxi-profile", id="header-profile-is-missing"),
    pytest.param(lambda inp: apierr.ApiError(404, "not_found_error",
                                             "the model names the profile 'ghost', which does not exist",
                                             param="model"), 404, "not_found_error", "model",
                 id="virtual-profile-is-missing"),
]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("refuse,status,error_type,param", REFUSALS)
def test_a_refusal_of_the_engine_is_the_http_status_before_anything_goes_upstream(
        prompt_relay, refuse, status, error_type, param, dialect, stream):
    relay = fake_relay(prompt_relay)
    relay.prompts.refuse = refuse
    mark = relay.mark()
    answer, body, headers = relay.post(dialect, client_request(dialect, USER, stream=stream), affinity())
    assert answer == status and "json" in headers["content-type"]
    error = body["error"]
    assert error["message"].endswith("does not exist")
    if dialect == "anthropic":
        assert body["type"] == "error" and error["type"] == error_type
    else:
        assert error["type"] == error_type and error["param"] == param
    assert relay.posts == [] and not relay.runtime.pool._active
    assert not relay.events(mark, "prompt.runtime_error")


# -- any other failure of the layer: the legacy path and one error event ---------------------------------------------
def boom(stage):
    return RuntimeError("synthetic failure of the " + stage)


@pytest.mark.parametrize("stage", ["reload", "plan"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_failing_store_never_fails_the_request(prompt_relay, stage, dialect):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)
    relay.prompts.fail[stage] = boom(stage)
    mark = relay.mark()
    status, _, _ = relay.post(dialect, client_request(dialect, USER, system="CLIENT_SYSTEM"), affinity())
    assert status == 200
    assert head_of(relay.posts[0]) == "CLIENT_SYSTEM"
    (event,) = relay.events(mark, "prompt.runtime_error")
    assert event["level"] == "error" and event["data"]["stage"] == stage
    assert "synthetic failure of the " + stage in event["message"]
    assert not relay.events(mark, "prompt.plan") and not relay.prompts.records


def test_the_error_event_names_the_failure_but_not_the_request(prompt_relay):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)
    relay.prompts.fail["plan"] = ValueError("bad pattern at 0\nsecond line")
    mark = relay.mark()
    system = "PRIVATE_CLIENT_SYSTEM_TEXT"
    assert relay.post("chat", client_request("chat", USER, system=system), affinity())[0] == 200
    (event,) = relay.events(mark, "prompt.runtime_error")
    assert "ValueError" in event["message"] and "\n" not in event["message"]
    assert system not in json.dumps(event) and "ORIGINAL_TASK" not in json.dumps(event)


def test_a_failing_compose_serves_the_legacy_head_and_says_so(prompt_relay, monkeypatch):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)

    def explode(self, client_system, sources):
        raise KeyError("a variable the engine cannot name")

    monkeypatch.setattr(FakePlan, "compose_head", explode)
    mark = relay.mark()
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"), affinity())[0] == 200
    assert head_of(relay.posts[0]) == "CLIENT_SYSTEM"
    (event,) = relay.events(mark, "prompt.runtime_error")
    assert event["data"]["stage"] == "compose" and "KeyError" in event["message"]
    assert not relay.events(mark, "prompt.plan") and not relay.prompts.records


def test_a_compose_that_returns_no_text_is_a_failure_too(prompt_relay, monkeypatch):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)
    monkeypatch.setattr(FakePlan, "compose_head", lambda self, client_system, sources: (None, [], []))
    mark = relay.mark()
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"), affinity())[0] == 200
    assert head_of(relay.posts[0]) == "CLIENT_SYSTEM"
    assert "TypeError" in relay.events(mark, "prompt.runtime_error")[0]["message"]


def test_a_failing_record_costs_an_event_and_nothing_else(prompt_relay):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)
    relay.prompts.fail["record"] = boom("record")
    mark = relay.mark()
    status, _, _ = relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"), affinity())
    assert status == 200 and head_of(relay.posts[0]) == RULES + "\n\nCLIENT_SYSTEM"
    (event,) = relay.events(mark, "prompt.runtime_error")
    assert event["data"]["stage"] == "record"
    assert relay.events(mark, "prompt.plan")


# -- a profile composes the head ------------------------------------------------------------------------------------
@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_head_is_what_the_plan_composes_and_the_journal_holds_no_text(prompt_relay, dialect):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)
    mark = relay.mark()
    system = "CLIENT_SYSTEM\nPRIVATE_SECOND_LINE"
    status, _, _ = relay.post(dialect, client_request(dialect, USER, system=system), affinity())
    assert status == 200
    head = RULES + "\n\n" + system
    assert head_of(relay.posts[0]) == head
    (event,) = relay.events(mark, "prompt.plan")
    data = event["data"]
    assert event["level"] == "debug"
    assert (data["rev"], data["profile"], data["pinned"], data["protected"]) == (1, "mine", False, False)
    assert data["rule"] == "default" and data["head_chars"] == len(head)
    assert data["head_sha8"] == prompt_policy.sha8(head)
    assert data["parts"] == [["text:rules", len(RULES)], ["literal", 2], ["client", len(system)]]
    assert data["history_changes"] == data["tools_changes"] == 0 and data["warnings"] == []
    assert event["request_id"] and event["turn_id"] and event["conversation"]
    encoded = json.dumps(relay.events(mark))
    assert "PRIVATE_SECOND_LINE" not in encoded and RULES not in encoded and "ORIGINAL_TASK" not in encoded


def test_the_ring_gets_the_texts_the_parts_and_the_request_the_model_was_sent(prompt_relay):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)
    request = client_request("chat", USER, system="CLIENT_SYSTEM")
    headers = {**affinity("ses-1"), "user-agent": "opencode/2.2.5", "x-proxi-agent": "build",
               "x-opencode-session-id": "ses-1", "x-session-id": "ses-1", "originator": "x"}
    assert relay.post("chat", request, {**headers, "x-api-key": "secret", "cookie": "secret=1"})[0] == 200
    (entry,) = relay.prompts.records
    head = RULES + "\n\nCLIENT_SYSTEM"
    assert entry["head"] == head and entry["request_id"] and isinstance(entry["ts"], float)
    assert (entry["rule"], entry["profile"], entry["rev"], entry["pinned"], entry["protected"]) == (
        "default", "mine", 1, False, False)
    assert (entry["client"], entry["agent"], entry["model"], entry["purpose"], entry["dialect"]) == (
        "opencode", "build", "claude-opus-5-5", "client-tools", "chat")
    assert entry["parts"] == prompt_policy.part_spans([prompt_policy.Part("text:rules", RULES),
                                                       prompt_policy.Part("literal", "\n\n"),
                                                       prompt_policy.Part("client", "CLIENT_SYSTEM")])
    assert entry["parts"][-1]["end"] == len(head) and entry["history_changes"] == entry["tools_changes"] == []
    assert entry["request"]["body"] == request and entry["request"]["dialect"] == "chat"
    assert entry["request"]["headers"] == {
        "user-agent": "opencode/2.2.5", "originator": "x", "x-session-affinity": "ses-1", "x-session-id": "ses-1",
        "x-proxi-agent": "build", "x-opencode-session-id": "ses-1"}
    assert "authorization" not in entry["request"]["headers"]


def test_a_body_beyond_a_mebibyte_is_not_kept_for_the_preview(prompt_relay):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)
    big = USER + [{"role": "assistant", "content": "x" * (1024 * 1024)}, {"role": "user", "content": "next"}]
    assert relay.post("chat", client_request("chat", big, system="CLIENT_SYSTEM"), affinity())[0] == 200
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"), affinity("small"))[0] == 200
    large, small = relay.prompts.records
    assert "request" not in large and small["request"]["body"]["messages"][1] == USER[0]


def test_a_failover_attempt_does_not_record_the_request_twice(prompt_relay):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)
    holder = {}
    runner = relay.runtime.runner
    parsed = validate.parse_chat(client_request("chat", USER, system="CLIENT_SYSTEM"), relay.runtime.cfg)
    parsed.prompt_plan = relay.prompts.plan(prompt_policy.PlanInput.from_request(parsed, {}, {}, "chat"))
    composed = ("HEAD", [prompt_policy.Part("literal", "HEAD")], [])
    for _ in range(2):
        runner._note_plan(lambda *a, **k: None, holder, parsed, composed)
    assert len(relay.prompts.records) == 1


def test_the_structured_contract_is_part_of_every_composed_head(prompt_relay):
    relay = fake_relay(prompt_relay)
    mine(relay.prompts)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
              "additionalProperties": False}
    request = client_request("chat", USER, system="CLIENT_SYSTEM", tools=False, response_format={
        "type": "json_schema", "json_schema": {"name": "r", "schema": schema}})
    assert relay.post("chat", request, affinity())[0] in (200, 502)
    head = head_of(relay.posts[0])
    assert head.startswith(RULES + "\n\nCLIENT_SYSTEM\n\n[STRUCTURED OUTPUT FORMAT]")
    assert head.count("[STRUCTURED OUTPUT FORMAT]") == 1
