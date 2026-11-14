"""A normal stop of the relay writes the conversation pins of prompts.json to disk (Runtime.close -> PromptStore.close)."""
import json

import journal
import prompt_policy
import proxy
from test_shutdown import runtime_config


def plan_input(key):
    return prompt_policy.PlanInput(dialect="chat", client="opencode", agent="", purpose="client-tools",
                                   request_class="", model="claude-opus-5-5", requested_model="claude-opus-5-5",
                                   virtual_profile=None, header_profile=None, tools=True, structured=False,
                                   has_client_system=True, conversation_key=key)


def test_the_pins_a_stop_finds_unwritten_are_on_disk_after_it(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL", journal.Journal())
    state = tmp_path / "conversations"
    cfg = runtime_config(tmp_path)
    cfg.update(conversation_state=True, conversation_state_dir=str(state), prompts_file=str(tmp_path / "prompts.json"))
    (tmp_path / "prompts.json").write_text(json.dumps({"version": 1, "profiles": {"mine": {"head": "{{client_system}}"}},
                                                       "default_profile": "mine"}), encoding="utf-8")
    runtime = proxy.Runtime(cfg)
    try:
        plan = runtime.prompts.plan(plan_input("header:stop-me"))
        assert plan.profile == "mine" and plan.rev is not None
    finally:
        runtime.close()
    stored = json.loads((state / prompt_policy.PINS_DIR / prompt_policy.PINS_FILE).read_text(encoding="utf-8"))
    assert len(stored["pins"]) == 1
    (record,) = stored["pins"].values()
    assert record["purpose"] == "client-tools" and record["profile"] == "mine" and record["rev"] not in (0, None)


def test_a_failing_pins_write_never_keeps_the_relay_from_stopping(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL", journal.Journal())
    runtime = proxy.Runtime(runtime_config(tmp_path))

    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(runtime.prompts, "close", broken)
    runtime.close()
    assert not runtime._loop_thread.is_alive()
