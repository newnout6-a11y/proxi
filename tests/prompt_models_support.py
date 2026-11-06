"""Fakes for the prompt layer, built to the contract in prompt_policy (plan 4.3 items 5 and 9).

P6 reads four accessors of a compiled revision: profile_names(), profile(name), codex_base(model) and
codex_overrides(model). The real Snapshot is P1's; these fakes keep the tests of this package independent
of it (tests that use the real engine skip while its accessors raise NotImplementedError).
"""
import fnmatch

import pytest

import prompt_policy


class FakeSnapshot:
    """A compiled revision with the profiles and Codex settings the test gives it.

    profiles: {name: normalized-profile fields, e.g. {"publish_as_model": True}} in document order.
    codex_base: None (built-in text), a string, or {model glob: text}.
    codex_models: {model glob: {override key: value}} in document order; the first matching glob wins.
    """

    def __init__(self, profiles=None, *, codex_base=None, codex_models=None, rev=1):
        self.rev = rev
        self.document = {}
        self.source_path = None
        self._profiles = dict(profiles or {})
        self._codex_base = codex_base
        self._codex_models = dict(codex_models or {})

    def profile_names(self):
        return ["passthrough", *self._profiles]

    def profile(self, name):
        if name == "passthrough":
            return {"description": "the client's system prompt, unchanged", "head": "{{client_system}}",
                    "publish_as_model": False}
        return {"head": "{{client_system}}", "publish_as_model": False, **self._profiles[name]}

    def relay_text(self, name):
        return None

    def regex(self, pattern, flags=""):
        raise NotImplementedError

    def codex_base(self, model):
        setting = self._codex_base
        if isinstance(setting, str):
            return setting
        for glob, text in (setting or {}).items():
            if fnmatch.fnmatchcase(model.lower(), glob.lower()):
                return text
        return None

    def codex_overrides(self, model):
        for glob, overrides in self._codex_models.items():
            if fnmatch.fnmatchcase(model.lower(), glob.lower()):
                return dict(overrides)
        return {}


class FakeStore:
    """Stands for runtime.prompts (and for the PromptStore a sync script builds): only what P6 reads."""

    def __init__(self, snapshot=None, *, path="prompts.json", errors=()):
        self.current = snapshot
        self.path = path
        self._errors = list(errors)
        self.constructed = []
        self.reloads = 0

    def snapshot(self):
        return self.current

    def maybe_reload(self):
        self.reloads += 1

    def errors(self):
        return list(self._errors)

    def factory(self):
        """A stand-in for the PromptStore class: remembers the (cfg, path) it was built with."""
        def build(cfg, path=None):
            self.constructed.append((cfg, path))
            return self
        return build


@pytest.fixture
def store(monkeypatch, tmp_path):
    """The PromptStore of everything built in the test (the relay's runtime.prompts, a sync script's store).

    Set store.current to a snapshot to publish a revision. Its path is a file that does not exist, so a test
    never reads a prompts file of the machine; create it to say that a file is there.
    """
    store = FakeStore(path=tmp_path / "prompts.json")
    monkeypatch.setattr(prompt_policy, "PromptStore", store.factory())
    return store

