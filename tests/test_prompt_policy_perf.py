"""I11: choosing the plan and rendering the head cost at most a millisecond, and nothing compiles on the request path."""
import re
import statistics
import time

import pytest

import prompt_policy as pp
from test_prompt_policy_common import Sources, document, inp, make_store


def realistic_system(size=17_500):
    parts = ["You are an AI agent running in OpenCode, a coding agent harness.\n\n# Harness\n- Markdown.\n\n"
             "# Communication\nBe concise.\n"]
    index = 0
    while sum(map(len, parts)) < size:
        index += 1
        parts.append("\n# Section %d\nSome paragraph about topic %d. %s\n```sh\n# comment %d\nls -la\n```\n"
                     % (index, index, "Lorem ipsum dolor sit amet. " * 12, index))
        if index % 5 == 0:
            parts.append("\n<available_skills>\n%s</available_skills>\n" % ("  <skill>name %d</skill>\n" % index * 10))
    parts.append("\n# Code comments\nMatch the density.\n\n# Tail\nToday's date: x\n<env>\nWorking directory: C:\\x\n</env>\n")
    return "".join(parts)


EDITS = [{"op": "remove_section", "heading": "# Code comments"}, {"op": "remove_tag", "tag": "available_skills"},
         {"op": "replace_text", "find": "Lorem ipsum", "with": "LOREM"},
         {"op": "remove_regex", "pattern": "<env>.*?</env>", "flags": "s"},
         {"op": "remove_between", "start": "# Section 3", "end": "# Section 4"}, {"op": "append", "text": "TAIL"}]


def realistic_document(rules=20, profiles=10):
    return document(
        texts={"mine": "My rules.\nBe nice."},
        profiles={"p%d" % n: {"head": "{{text:mine}}\n\n{{client_system}}\n\n{{structured}}",
                              "client_system": {"edits": EDITS}} for n in range(profiles)},
        rules=[{"name": "r%d" % i, "if": {"model": "gpt-%d*" % i, "client": ["opencode", "codex"], "tools": True},
                "use": "p%d" % (i % profiles)} for i in range(rules - 1)]
        + [{"name": "opencode", "if": {"client": "opencode", "agent": "build"}, "use": "p3"}])


def timed(function, runs=300):
    times = []
    for _ in range(runs):
        started = time.perf_counter()
        function()
        times.append((time.perf_counter() - started) * 1000)
    return min(times), statistics.median(times)


@pytest.fixture(scope="module")
def system():
    return realistic_system()


def test_a_realistic_document_costs_under_a_millisecond_per_request(tmp_path, system):
    store = make_store(tmp_path, realistic_document())
    request = inp(conversation_key="conv-1")
    plan = store.plan(request)
    assert (plan.rule, plan.profile) == ("opencode", "p3")

    def work():
        store.plan(request).compose_head(system, Sources())

    work()
    fastest, median = timed(work)
    assert len(system) > 17_000
    assert fastest < 1.0, "plan + head took %.3f ms at best" % fastest
    assert median < 3.0, "median %.3f ms" % median
    head, parts, warnings = plan.compose_head(system, Sources())
    assert "available_skills" not in head and "<env>" not in head and warnings == [] and head.endswith("TAIL")


def test_choosing_a_plan_alone_is_cheap_even_with_many_rules(tmp_path):
    store = make_store(tmp_path, realistic_document(rules=120, profiles=10))
    request = inp(conversation_key="conv-1", model="gpt-9999", client="codex", agent="x")
    store.plan(request)
    fastest, median = timed(lambda: store.plan(request), runs=500)
    assert fastest < 0.5 and median < 1.5, (fastest, median)


def test_nothing_is_compiled_on_the_request_path(tmp_path, system, monkeypatch):
    store = make_store(tmp_path, realistic_document())
    request = inp(conversation_key="conv-1")
    store.plan(request).compose_head(system, Sources())
    with monkeypatch.context() as patched:
        def boom(*args, **kwargs):
            raise AssertionError("a pattern was compiled while serving a request")

        patched.setattr(re, "compile", boom)
        patched.setattr(re, "_compile", boom)
        for _ in range(3):
            plan = store.plan(request)
            plan.compose_head(system, Sources())
            store.plan(inp(conversation_key="other", model="gpt-3-x", client="codex"))


def test_the_regexes_of_a_revision_are_compiled_with_it(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {"client_system": {"edits": [
        {"op": "remove_regex", "pattern": "a+b"}]}, "history": {"user_blocks": [
            {"op": "drop_tag", "tag": "t", "pattern": "x+y"}]}}}))
    snapshot = store.snapshot()
    assert snapshot.regex("a+b") is snapshot.regex("a+b") and snapshot.regex("x+y") is snapshot.regex("x+y")
    assert len(snapshot._data.patterns) == 2 and not snapshot._extra_patterns


def test_compiling_a_realistic_document_is_quick(tmp_path):
    document_ = realistic_document()
    started = time.perf_counter()
    for _ in range(5):
        pp.compile_document(document_, tmp_path, rev=1)
    assert (time.perf_counter() - started) / 5 < 0.5


def test_edits_on_a_large_system_prompt_stay_linear(tmp_path):
    big = realistic_system(1_000_000)
    store = make_store(tmp_path, realistic_document())
    plan = store.plan(inp())
    started = time.perf_counter()
    head, _parts, _warnings = plan.compose_head(big, Sources())
    assert time.perf_counter() - started < 5.0
    assert "<env>" not in head and "available_skills" not in head
