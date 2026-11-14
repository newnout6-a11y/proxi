"""Owner's rule: the relay never lowers a reasoning effort and never swaps a request to a cheaper or weaker model.

The only effort mapping of the Retool-limits work is xhigh on a Claude route (high by default, max by the admin's choice). The
other parts (the emulated forced choice and its retry, the output cap, WebSearch, the retries after Retool's 429) send the model
and the effort that the client asked for, on every POST they make; none of them has a fallback model."""
import ast
from pathlib import Path

import pytest

import direct
import forced_choice
import models
import validate
import web_search
from test_forced_choice import READ, call, drive as drive_forced
from test_native_context_reuse import FIRST, PARSE_CFG
from test_native_history import tool_reply
from test_rate_limit_retry import ANSWER, REFUSED, glm_turn
from test_review_fixes_pr19 import END, text
from test_web_search import FakeSearch
from test_web_search_loop import answers, asks, drive as drive_search, search_request

EFFORTS = ["low", "medium", "high", "max"]
CLAUDE = ["claude-opus-5-5", "claude-sonnet-5-5"]
ROOT = Path(__file__).resolve().parents[1]


def chat(model, effort, **extra):
    data = {"model": model, "reasoning_effort": effort, "messages": FIRST[:] + [], **extra}
    parsed = validate.parse_chat(data, PARSE_CFG)
    parsed.conversation_key = "header:rule"
    return parsed


def same_model_and_effort(bodies, model, effort):
    assert bodies and all(body["model"] == model for body in bodies), [body["model"] for body in bodies]
    assert [body.get("reasoningEffort") for body in bodies] == [effort] * len(bodies)


# -- the emulated forced choice -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "read_file"}}])
@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("model", CLAUDE)
def test_the_forced_choice_and_its_firmer_retry_keep_the_model_and_the_effort(tmp_path, monkeypatch, model, effort, choice):
    miss = [text("I would rather not."), *END]
    _, sent, _ = drive_forced(
        tmp_path, monkeypatch, [miss, tool_reply()],
        lambda _: chat(model, effort, tools=[READ], tool_choice=choice), model=model)
    assert len(sent) == 2 and "toolChoice" not in sent[0] and "toolChoice" not in sent[1]
    same_model_and_effort(sent, model, effort)  # the retry is a firmer wording, never another model or another effort


def test_the_retry_asks_for_the_same_call_in_the_same_body_but_for_its_last_turn(tmp_path, monkeypatch):
    miss = [text("no"), *END]
    _, sent, _ = drive_forced(tmp_path, monkeypatch, [miss, tool_reply()],
                              lambda _: chat("claude-opus-5-5", "max", tools=[READ], tool_choice="required"))
    first, second = sent
    assert {key: value for key, value in first.items() if key not in ("messages", "rit")} == {
        key: value for key, value in second.items() if key not in ("messages", "rit")}
    assert first["messages"][:-1] == second["messages"][:-1] and first["messages"][-1] != second["messages"][-1]


def test_the_helper_that_adds_the_instruction_changes_nothing_but_the_messages():
    body = {"model": "claude-opus-5-5", "reasoningEffort": "max", "messages": [{"role": "user", "content": []}],
            "tools": [{"name": "t"}], "rit": "x", "openAICacheKey": "k", "maxCompletionTokens": 99, "metadata": {"a": 1}}
    changed = direct._asked_for_call(body, "call it")
    assert {key: value for key, value in changed.items() if key not in ("messages", "rit")} == {
        key: value for key, value in body.items() if key not in ("messages", "rit")}
    assert len(changed["messages"]) == 2 and changed["messages"][-1]["content"][0]["text"] == "call it"


# -- the output cap -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("model", CLAUDE)
def test_the_output_cap_is_cut_in_the_relay_and_sends_the_same_model_and_effort(tmp_path, monkeypatch, model, effort):
    _, sent, _ = drive_forced(tmp_path, monkeypatch, [[text("ok"), *END], [text("ok"), *END]],
                                    lambda _: chat(model, effort, max_tokens=100),
                                    lambda _: chat(model, effort), model=model)
    capped, plain = sent
    same_model_and_effort([capped, plain], model, effort)
    assert capped["maxCompletionTokens"] == 100  # the cap still goes upstream, as it always did


# -- WebSearch ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("model", CLAUDE)
def test_every_step_of_a_search_runs_on_the_model_and_the_effort_of_the_request(tmp_path, monkeypatch, model, effort):
    search, made = FakeSearch(), []

    def request(_):
        parsed = search_request(search, output_config={"effort": effort}, model=model)
        made.append(parsed)
        return parsed

    _, sent, _ = drive_search(tmp_path, monkeypatch, [asks("q", call_id="a"), asks("q2", call_id="b"), answers()],
                                    search, [request])
    assert len(sent) == 3 and len(search.calls) == 2
    same_model_and_effort(sent, model, effort)


def test_the_search_loop_has_no_other_model_to_turn_to(tmp_path, monkeypatch):
    # a provider that fails is an error result for the model; the answer after it is the same model's
    from web_search import SearchError
    search = FakeSearch(error=SearchError("unavailable", "HTTP 503"))
    _, sent, _ = drive_search(tmp_path, monkeypatch, [asks("q"), answers("No luck.")], search,
                                    [lambda _: search_request(search, output_config={"effort": "max"})])
    same_model_and_effort(sent, "claude-opus-5-5", "max")


# -- Retool's 429 -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("model", CLAUDE)
def test_every_retry_after_a_429_is_the_same_body_on_the_same_model(tmp_path, monkeypatch, model, effort):
    _, sent, _ = drive_forced(tmp_path, monkeypatch, [REFUSED, REFUSED, ANSWER], lambda _: chat(model, effort),
                              model=model)
    assert len(sent) == 3 and all(body == sent[0] for body in sent)
    same_model_and_effort(sent, model, effort)


def test_glm_stays_glm_and_is_given_no_effort_it_was_not_asked_for_when_the_limit_stays(tmp_path, monkeypatch):
    events, sent, _, _ = glm_turn(tmp_path, monkeypatch, [REFUSED])
    assert events[-1]["error"].status == 429 and len(sent) == 4
    assert [body["model"] for body in sent] == ["zai-org/GLM-5.3"] * 4  # no fall back to the Flash route, ever
    assert all("reasoningEffort" not in body for body in sent) and all(body == sent[0] for body in sent)


# -- the one mapping ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("settings", [{}, {"claude_xhigh_effort": "max"}, {"allow_effort_downgrade": True},
                                      {"claude_xhigh_effort": "max", "allow_effort_downgrade": True}])
@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("model", CLAUDE)
def test_the_xhigh_setting_changes_xhigh_and_no_other_level(model, effort, settings):
    parsed = validate.parse_chat({"model": model, "reasoning_effort": effort, "messages": FIRST}, dict(PARSE_CFG, **settings))
    assert (parsed.requested_effort, parsed.effort) == (effort, effort)


@pytest.mark.parametrize("replacement", ["high", "max"])
@pytest.mark.parametrize("model", CLAUDE)
def test_xhigh_becomes_what_the_admin_chose_and_nothing_below_high(model, replacement):
    parsed = validate.parse_chat({"model": model, "reasoning_effort": "xhigh", "messages": FIRST},
                                 dict(PARSE_CFG, claude_xhigh_effort=replacement))
    assert parsed.effort == replacement and models.EFFORT_ORDER.index(parsed.effort) >= models.EFFORT_ORDER.index("high")
    assert set(models.XHIGH_REPLACEMENTS) == {"high", "max"}  # the setting cannot name a lower level


def test_the_setting_does_not_reach_a_route_that_is_not_claude():
    for model in ("gpt-6-sol", "gpt-5.5"):
        parsed = validate.parse_chat({"model": model, "reasoning_effort": "high", "messages": FIRST},
                                     dict(PARSE_CFG, claude_xhigh_effort="max"))
        assert parsed.effort == "high"


# -- the modules of the new parts never touch a request's model or effort ------------------------------------------------

@pytest.mark.parametrize("module", ["forced_choice", "web_search"])
def test_the_new_modules_do_not_read_or_write_the_model_provider_or_the_effort(module):
    tree = ast.parse((ROOT / (module + ".py")).read_text(encoding="utf-8"))
    forbidden = {"reasoningEffort", "reasoning_effort", "effort", "model_provider", "providerResourceName"}
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not (strings | names) & forbidden
