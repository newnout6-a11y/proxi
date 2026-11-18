"""scripts/live_smoke.py reads the streams of the relay as they are: the last chunk carries usage and no choices."""
import pytest

import apierr
from scripts import live_smoke


def test_the_smoke_checks_run_to_the_end_against_a_relay(relay, monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", relay.key)
    monkeypatch.setattr(live_smoke, "BASE", relay.url)
    relay.fake.script = [{"type": "text", "delta": "README relay"}, {"type": "end", "reason": "stop"}]
    live_smoke.test_streaming_openai()
    live_smoke.test_local_exec_and_context()


@pytest.mark.parametrize("check", ["test_streaming_openai", "test_local_exec_and_context"])
def test_an_error_the_relay_ends_the_stream_with_fails_the_check(relay, monkeypatch, check):
    # The relay ends a stream that broke after some text with an error event (proxy._stream_failure); the smoke used to
    # skip it, keep the partial text and print PASS (audit A13-03).
    monkeypatch.setenv("RELAY_API_KEY", relay.key)
    monkeypatch.setattr(live_smoke, "BASE", relay.url)
    relay.fake.script = [{"type": "text", "delta": "README relay, partial"},
                         apierr.upstream_error("upstream stream ended without end_of_stream")]
    with pytest.raises(AssertionError, match="error"):
        getattr(live_smoke, check)()
