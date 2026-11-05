"""Shared fixtures of the prompt_policy tests: documents, requests, fake relay sources, a fake clock."""
import json
import pathlib

import prompt_policy as pp

OPENCODE_SYSTEM = (
    "You are an AI agent running in OpenCode, a coding agent harness.\n\n"
    "# Harness\n- Responses are rendered as GitHub-flavored Markdown.\n\n"
    "# Communication\n- Keep responses clear and concise.\n\n"
    "# Code comments\nBy default, match the surrounding comment density.\n\n"
    "# Tail\nToday's date: Thu Nov 05 2026\n"
    "<env>\n  Working directory: C:\\work\n  Platform: win32\n</env>\n")


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Sources:
    """HeadSources that count how often each method is asked for (the runtime computes them lazily)."""

    def __init__(self, client_owned=True, **texts):
        self.client_owned = client_owned
        self.texts = {"relay_role": "BUILTIN-ROLE", "native_role": "BUILTIN-NATIVE", "relay_task": "RELAY-TASK",
                      "local_exec": "LOCAL-EXEC", "project_context": "PROJECT-CONTEXT",
                      "exec_reminder": "EXEC-REMINDER", "structured": ""}
        self.texts.update(texts)
        self.calls = []

    def _ask(self, name):
        self.calls.append(name)
        return self.texts[name]

    def relay_role(self):
        return self._ask("relay_role")

    def native_role(self):
        return self._ask("native_role")

    def relay_task(self):
        return self._ask("relay_task")

    def local_exec(self):
        return self._ask("local_exec")

    def project_context(self):
        return self._ask("project_context")

    def exec_reminder(self):
        return self._ask("exec_reminder")

    def structured(self):
        return self._ask("structured")


def document(**sections):
    """A valid prompts document with the given top-level sections."""
    return {"version": 1, **sections}


def profile_doc(head="{{client_system}}", **extra):
    return document(profiles={"p": {"head": head, **extra}})


def inp(**fields):
    """A PlanInput for an OpenCode request with tools; override any field."""
    values = dict(dialect="chat", client="opencode", agent="build", purpose="client-tools", request_class="",
                  model="claude-opus-5-5", requested_model="claude-opus-5-5", virtual_profile=None,
                  header_profile=None, tools=True, structured=False, has_client_system=True,
                  conversation_key=None)
    values.update(fields)
    return pp.PlanInput(**values)


def compile_doc(doc, tmp_path, rev=1):
    return pp.compile_document(doc, pathlib.Path(tmp_path), rev=rev)


def plan_for(doc, tmp_path, profile="p", **plan_fields):
    """A Plan of the profile `profile` of `doc`, built by hand over a compiled snapshot."""
    snapshot = compile_doc(doc, tmp_path)
    return pp.Plan(rev=snapshot.rev, profile=profile, rule="test", snapshot=snapshot, **plan_fields)


def compose(doc, tmp_path, system, sources=None, profile="p"):
    plan = plan_for(doc, tmp_path, profile)
    return plan.compose_head(system, sources or Sources())


def write_json(path, document):
    path = pathlib.Path(path)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def make_store(tmp_path, document=None, clock=None, **cfg):
    path = pathlib.Path(tmp_path) / "prompts.json"
    if document is not None:
        write_json(path, document)
    return pp.PromptStore({"prompts_file": str(path), **cfg}, clock=clock or Clock())
