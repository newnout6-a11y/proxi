"""Proxy HTTP hardening: stream-error status in journal, double-response guard."""
import apierr
import journal


def test_stream_error_is_not_logged_as_success(relay):
    """A mid-stream failure used to keep the pre-turn 200 status in the journal,
    so alerting by level never saw failed streams."""
    relay.fake.script = [{"type": "error", "error": apierr.upstream_error("boom")}]
    status, text = relay.sse("/v1/chat/completions",
                             {"model": "claude-opus-5-5", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200 and "event: error" in text
    # Ordering across the whole suite is not stable: look for the failure shape,
    # not for "the newest chat entry".
    failed = [e for e in journal.JOURNAL.query()
              if e["kind"] == "http.request" and e["data"]["path"] == "/v1/chat/completions"
              and e["data"].get("status") == 502]
    assert failed, "the mid-stream failure must be journaled with its real status"
    assert all(e["level"] == "error" for e in failed), \
        "stream failures must not be logged as success"


def test_delete_method_gets_json_405(relay):
    """Unknown methods used to fall through to a bare HTML 501 with no journal
    entry; DELETE now reaches the normal dispatch (405 with a JSON envelope)."""
    status, body = relay.json_any("/v1/models", method="DELETE")
    assert status == 405
    assert body["error"]["type"] == "method_not_allowed"
