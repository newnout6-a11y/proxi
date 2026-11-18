"""An error event that opens Retool's HTTP 200, before the model gave anything, is no possible charge (A03-06, R1b P2).

Retool sends a refusal that its provider made before generating (an input past the context window: probes p21 and p31 of
2026-11-18) as HTTP 200 + {"event": "error", "isRetryable": false, ...} + end_of_stream, with no metadata: it costs
nothing. The relay took any 2xx for a possible charge and only knew better for the hourly limit: it marked the account's
balance unreported (mark_usage_unknown), told the client an unknown cost and sent the empty usage event that zeroes the
conversation's prefix size."""
import pytest

import apierr
from test_review_fixes_pr19 import text
from test_usage_truth import MARKER, drive

END = {"event": "end_of_stream"}
# The two shapes the probes saw, as they came (x-request-id left out).
OPENAI_OVERLONG = {"event": "error", "error": "Your input exceeds the context window of this model. Please adjust your input "
                   "and try again.", "isRetryable": False, "isUsingRetoolManagedKey": True, "isOnPrem": False}
ANTHROPIC_OVERLONG = {"event": "error", "error": '400 {"type":"error","error":{"type":"invalid_request_error","message":'
                      '"prompt is too long: 696320 tokens > 200000 maximum"},"request_id":"req_x"}', "isRetryable": False,
                      "statusCode": 400, "isUsingRetoolManagedKey": True, "isOnPrem": False}
INVALID = {"event": "error", "error": "Invalid tool schema", "isRetryable": False, "statusCode": 400}
THINKING_START = {"event": "content_block_start", "type": "thinking"}


@pytest.mark.parametrize("refusal", [OPENAI_OVERLONG, ANTHROPIC_OVERLONG, INVALID], ids=["p21", "p31", "invalid"])
def test_a_refusal_that_opens_the_stream_is_no_charge(tmp_path, monkeypatch, refusal):
    events, error, unknown, _ = drive(tmp_path, monkeypatch, [[refusal, END]])
    assert isinstance(error, apierr.ApiError) and error.status == 400
    assert MARKER not in events and unknown == [], "nothing was generated, nothing is billed"


def test_the_context_refusal_keeps_its_own_code(tmp_path, monkeypatch):
    _, error, _, _ = drive(tmp_path, monkeypatch, [[OPENAI_OVERLONG, END]])
    assert error.code == "context_length_exceeded"


@pytest.mark.parametrize("before", [[text("partial")], [THINKING_START]], ids=["after text", "after thinking began"])
def test_one_that_comes_after_the_model_began_stays_a_possible_charge(tmp_path, monkeypatch, before):
    events, error, unknown, _ = drive(tmp_path, monkeypatch, [[*before, INVALID, END]])
    assert error is not None and events.count(MARKER) == 1 and unknown == [True]


def test_a_read_that_fails_on_a_200_is_still_a_possible_charge(tmp_path, monkeypatch):
    # Not an error event: the connection was reset after the request had been sent and answered with a 200.
    events, error, unknown, _ = drive(tmp_path, monkeypatch, [[text("par")]])
    assert error is None or error.code == "upstream_error"
    assert events.count(MARKER) == 1 and unknown == [True]
