"""scripts/live_smoke.py reads the streams of the relay as they are: the last chunk carries usage and no choices."""
from scripts import live_smoke


def test_the_smoke_checks_run_to_the_end_against_a_relay(relay, monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", relay.key)
    monkeypatch.setattr(live_smoke, "BASE", relay.url)
    relay.fake.script = [{"type": "text", "delta": "README relay"}, {"type": "end", "reason": "stop"}]
    live_smoke.test_streaming_openai()
    live_smoke.test_local_exec_and_context()
