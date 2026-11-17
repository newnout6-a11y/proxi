"""Per-model credit rates (credits.MODEL_RATES) and the text of the empty-turn 502.

The rates are the exact least-squares fit of the live journal of 2026-11-06 (753 Sonnet 5.5 and 120 Opus 5.5 attempts):
the estimate fields cost_* must follow the model of the turn, not the Opus default for every model. The GPT and GLM models
came from the journal of 2026-11-07, where the Opus default had made gpt-6.1-sol's cache write twice and its read twice too
dear, and GLM-5.3's read 2.8 times too cheap."""
import pytest

import credits
from upstream_attempts import AttemptBudget
import threading

USAGE = {"input_tokens": 1000, "output_tokens": 2000, "cache_read": 300000, "cache_write": 50000}


def total(breakdown):
    return round(sum(breakdown[key] for key in ("cost_input", "cost_cache_read", "cost_cache_write", "cost_output")), 5)


def test_sonnet_costs_half_of_opus_except_a_cache_read():
    sonnet, opus = credits.cost_breakdown(USAGE, "claude-sonnet-5-5"), credits.cost_breakdown(USAGE, "claude-opus-5-5")
    assert sonnet["cost_cache_read"] == opus["cost_cache_read"] == round(300000 * 4e-6, 5)
    for key in ("cost_input", "cost_cache_write", "cost_output"):
        assert sonnet[key] * 2 == opus[key], key
    # a turn of the live journal: Sonnet, 10149 read, 531959 written, 2 input, 3800 output -> 28.0
    turn = {"input_tokens": 2, "output_tokens": 3800, "cache_read": 10149, "cache_write": 531959}
    assert abs(total(credits.cost_breakdown(turn, "claude-sonnet-5-5")) - (2 * 4e-5 + 3800 * 2e-4 + 10149 * 4e-6
                                                                           + 531959 * 5e-5)) < 1e-4


def test_a_model_without_rates_of_its_own_and_no_model_keep_the_default_fit():
    default = credits.cost_breakdown(USAGE)
    assert credits.cost_breakdown(USAGE, "gpt-6-sol") == default == credits.cost_breakdown(USAGE, None)
    assert default["cost_input"] == round(1000 * credits.RATE_INPUT, 5)
    assert credits.rates("unknown")["cache_write"] == credits.RATE_CACHE_WRITE == 1e-4


def test_the_rates_are_what_the_fit_gave():
    assert credits.rates("claude-sonnet-5-5") == {"input": 4e-5, "cache_read": 4e-6, "cache_write": 5e-5, "output": 2e-4}
    assert credits.rates("claude-opus-5-5") == {"input": 8e-5, "cache_read": 4e-6, "cache_write": 1e-4, "output": 4e-4}


@pytest.mark.parametrize("model, row, billed", [
    # input, cache read, cache write and output tokens of one attempt of the live journal, and the credits Retool reported
    ("gpt-6.1-sol", (3, 4406, 29870, 256), 1.55363),
    ("gpt-6.1-terra", (3, 0, 211376, 1984), 5.48286),
    ("gpt-5.5", (246, 4608, 0, 5), 0.07368),
    ("claude-sonnet-5", (90, 46390, 148, 11), 0.29814),
    ("zai-org/GLM-5.3", (33893, 576, 0, 102), 3.83837),
    ("zai-org/GLM-5.3-Flash", (227, 41472, 0, 518), 0.61488),
])
def test_the_rates_of_the_gpt_and_glm_models_give_what_retool_billed(model, row, billed):
    usage = dict(zip(("input_tokens", "cache_read", "cache_write", "output_tokens"), row))
    assert total(credits.cost_breakdown(usage, model)) == pytest.approx(billed, abs=1e-4)
    # The default fit gave from half to four times the bill for these rows.
    assert total(credits.cost_breakdown(usage, "unknown")) != pytest.approx(billed, rel=0.05)


def test_a_model_that_reports_no_cache_write_has_the_price_of_input_as_its_write():
    for model in ("gpt-5.5", "zai-org/GLM-5.3", "zai-org/GLM-5.3-Flash"):
        rate = credits.rates(model)
        assert rate["cache_write"] == rate["input"], model


def test_the_empty_turn_502_names_a_classifier_refusal_as_a_possible_cause():
    error = AttemptBudget(threading.Event()).error()
    assert error.status == 502 and error.code == "upstream_empty_turn"
    assert error.message.startswith("upstream returned an empty turn")
    assert "classifier" in error.message and "system prompt" in error.message
    # another code keeps the message it is given
    assert AttemptBudget(threading.Event()).error("upstream_attempt_limit", "x").message == "x"
