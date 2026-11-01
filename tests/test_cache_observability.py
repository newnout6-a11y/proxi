"""Tests for measured Retool rates and per-turn cost breakdown observability."""
import credits


def test_cost_breakdown_matches_measured_rates():
    usage = {"input_tokens": 4, "output_tokens": 1000, "cache_read": 22471, "cache_write": 302}
    b = credits.cost_breakdown(usage)
    assert b["prompt_tokens"] == 4 + 22471 + 302
    assert b["cost_input"] == round(4 * credits.RATE_INPUT, 5)
    assert b["cost_cache_read"] == round(22471 * credits.RATE_CACHE_READ, 5)
    assert b["cost_cache_write"] == round(302 * credits.RATE_CACHE_WRITE, 5)
    assert b["cost_output"] == round(1000 * credits.RATE_OUTPUT, 5)
    assert b["cache_hit_ratio"] == round(22471 / 22777, 4)


def test_cost_breakdown_handles_missing_fields():
    b = credits.cost_breakdown({})
    assert b["prompt_tokens"] == 0
    assert b["cache_hit_ratio"] is None
    assert b["cost_output"] == 0
    assert b["cost_cache_write"] == 0


def test_breakdown_reproduces_record_turn_credit_cost():
    """The 46.53-credit cache-collapse turn from the journal
    (write=462838, output=606) must be reproducible from the fitted rates."""
    b = credits.cost_breakdown({"input_tokens": 4, "output_tokens": 606,
                                "cache_read": 0, "cache_write": 462838})
    total = b["cost_input"] + b["cost_cache_read"] + b["cost_cache_write"] + b["cost_output"]
    assert abs(total - 46.53) < 0.05
