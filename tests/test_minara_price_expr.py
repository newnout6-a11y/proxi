"""Minara's prices are new-api's billing expressions (its `tiered_expr` mode): the relay reads them itself to price every
Minara turn and to budget the catalog refresh. A variable or function it did not know left a model unmeasured and its
turns unpriced, said nowhere (Gemini 4 Carbon and Hydrogen's `ai`, 2026-11-13); now the reader knows new-api's whole
chat language, and a price it still cannot read comes with a reason (minara.price_problem)."""
import pytest

import credits
import minara


def tiered(expression, **extra):
    return dict({"quota_type": 0, "billing_mode": "tiered_expr", "billing_expr": expression, "group_ratio": 1}, **extra)


CARBON = tiered("p * 2 + c * 10 + cr * 0.1 + cc * 0.1875 + ai * 2")
HYDROGEN = tiered("p * 1.5 + c * 7.5 + cr * 0.075 + cc * 0.0833333333333333 + ai * 1.5")
SONNET_45 = tiered("len > 200000 ? (p * 6 + c * 22.5 + cr * 0.6 + cc * 7.5 + cc1h * 12) : "
                   "(p * 3 + c * 15 + cr * 0.3 + cc * 3.75 + cc1h * 6)")
GROK = tiered("len > 200000 ? (p * 4 + c * 12 + cr * 1) : (p * 2 + c * 6 + cr * 0.5)")
TURN = {"input_tokens": 4, "output_tokens": 20, "cache_read": 89_557, "cache_write": 30_439}


def test_gemini_4_prices_with_audio_input_are_read():
    assert minara.price_problem(CARBON) is None and minara.price_problem(HYDROGEN) is None
    # No audio in the prompt: ai is 0 and p is the uncached input, as for any other expression.
    assert minara.estimated_cost(CARBON, TURN) == pytest.approx((4 * 2 + 20 * 10 + 89_557 * 0.1 + 30_439 * 0.1875) / 1e6)
    assert minara.estimated_cost(HYDROGEN, TURN) == pytest.approx(
        (4 * 1.5 + 20 * 7.5 + 89_557 * 0.075 + 30_439 * 0.0833333333333333) / 1e6)


def test_audio_input_is_taken_out_of_p_and_priced_by_ai():
    # An OpenAI-shaped usage counts audio inside prompt_tokens: an expression with ai bills it there, not twice.
    reply = {"prompt_tokens": 1_500, "completion_tokens": 10,
             "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0, "audio_tokens": 500}}
    priced = minara.normalize_usage(reply, tiered("p * 1 + c * 10 + ai * 100"))
    assert priced["provider_cost"] == pytest.approx((1_000 * 1 + 10 * 10 + 500 * 100) / 1e6)
    assert "audio_input_tokens" not in priced and priced["input_tokens"] == 1_500  # the counters stay the relay's own
    # Without ai the audio stays in p at the input price.
    assert minara.normalize_usage(reply, tiered("p * 1 + c * 10"))["provider_cost"] == pytest.approx(
        (1_500 * 1 + 10 * 10) / 1e6)
    # Audio and image output likewise leave c when ao and img_o price them.
    out = {"prompt_tokens": 10, "completion_tokens": 300,
           "completion_tokens_details": {"audio_tokens": 100, "image_tokens": 50}}
    assert minara.normalize_usage(out, tiered("p + c * 10 + ao * 20 + img_o * 30"))["provider_cost"] == pytest.approx(
        (10 + 150 * 10 + 100 * 20 + 50 * 30) / 1e6)


def test_a_cache_count_the_expression_does_not_price_is_billed_as_input():
    # new-api leaves tokens of a variable an expression does not use in p (Grok 4.7 prices no cache write).
    assert minara.estimated_cost(GROK, TURN) == pytest.approx(((4 + 30_439) * 2 + 20 * 6 + 89_557 * 0.5) / 1e6)
    assert minara.estimated_cost(tiered("p * 2 + c * 10"), TURN) == pytest.approx(
        ((4 + 89_557 + 30_439) * 2 + 20 * 10) / 1e6)


def test_the_tier_is_chosen_by_the_whole_prompt_and_writes_are_billed_at_cc():
    small = minara.estimated_cost(SONNET_45, TURN)
    assert small == pytest.approx((4 * 3 + 20 * 15 + 89_557 * 0.3 + 30_439 * 3.75) / 1e6)
    large = minara.estimated_cost(SONNET_45, dict(TURN, cache_read=189_557))  # len 220,000: the long tier
    assert large == pytest.approx((4 * 6 + 20 * 22.5 + 189_557 * 0.6 + 30_439 * 7.5) / 1e6)


@pytest.mark.parametrize("expression,expected", [
    ('tier("base", p * 2.5 + c * 15 + cr * 0.25)', 4 * 2.5 + 20 * 15 + 89_557 * 0.25 + 30_439 * 2.5),
    ('v1:tier("base", p * 2 + c * 8)', (4 + 30_439) * 2 + 20 * 8 + 89_557 * 2),
    ('len <= 32000 ? tier("short", fixed(0.01)) : tier("long", p * 2 + c * 8 + cr * 0 + cc * 0)', 4 * 2 + 20 * 8),
    ('len <= 200000 ? tier("short", fixed(0.01)) : tier("long", p)', 0.01 * 1e6),
    ("max(p, c) * 2 + min(cr, cc) * 0 + abs(-1) * ceil(0.2) * floor(1.9)", 20 * 2 + 1),
    ("len > 100000 && c > 10 || !(cr > 0) ? p * 3 + c * 0 + cr * 0 + cc * 0 : p", 4 * 3),
    ("(len > 1 and not (c < 1)) ? p + c * 0 + cr * 0 + cc * 0 : 0", 4),
    ("p * 1_000 + c * 1e3 + cr * 0 + cc * .5 * 0", 4 * 1000 + 20 * 1000),
    ("cr > 0 ? (p + c + cc) / cr * 0 + p : p / 0", 4),  # the branch not taken is never computed
    ("p * 10 % 3 + c * 0 + cr * 0 + cc * 0", 40 % 3),
    ('tier("base", p * 5 + c * 25 + cr * 0 + cc * 0)|||when(header("anthropic-beta") has "fast-mode") * 6',
     4 * 5 + 20 * 25),
    ('p * 5 + c * 25 + cr * 0 + cc * 0 ||| when(param("service_tier") == "priority") * 2 ||| (true ? 3 : 1)',
     (4 * 5 + 20 * 25) * 3),
    ('image_count * (p + c * 0 + cr * 0 + cc * 0) + img * 9 + img_cr * 9 + cc1h * 9 + cc12h * 9 + ao * 9', 4),
])
def test_new_apis_language_is_read(expression, expected):
    assert minara.price_problem(tiered(expression)) is None
    assert minara.estimated_cost(tiered(expression), TURN) == pytest.approx(expected / 1e6)


def test_the_clock_functions_read_the_time_in_their_zone():
    always = tiered('hour("Asia/Shanghai") >= 0 && minute("UTC") < 60 && weekday() <= 6 && month() >= 1 && day() >= 1 '
                    '? p * 1 : p * 1000')
    assert minara.estimated_cost(always, {"input_tokens": 1_000_000}) == pytest.approx(1.0)
    assert "часовой пояс" in minara.price_problem(tiered('hour("Mars/Olympus") > 1 ? p : c'))


@pytest.mark.parametrize("pricing,reason", [
    (tiered("p * 2 + vid * 3"), "неизвестная переменная vid"),
    (tiered('u("seconds") * 0.4'), "неизвестная функция u"),
    (tiered("__import__(1)"), "неизвестная функция __import__"),
    (tiered("p * "), "формула оборвалась"),
    (tiered("p * 2)"), "лишнее «)»"),
    (tiered("p $ 2"), "непонятный знак «$»"),
    (tiered("(" * 600 + "p" + ")" * 600), "слишком глубокая вложенность"),
    (tiered("tier(p)"), "у tier() не то число аргументов"),
    (tiered("v2:p"), "версия формулы v2"),
    (tiered('"text" * 2'), "не число"),
    (tiered(""), "нет формулы"),
    ({"quota_type": 1, "request": 0.13}, "цена за запрос"),
    ({"quota_type": 0, "billing_mode": "per_image"}, "незнакомый способ расчёта per_image"),
    ({"quota_type": 0, "prompt": None, "completion": None}, "нет цены за токены"),
    (None, "нет цены"),
])
def test_a_price_that_cannot_be_read_names_the_reason(pricing, reason):
    assert minara.estimated_cost(pricing, TURN) is None
    assert reason in minara.price_problem(pricing)


def test_the_write_of_an_expression_priced_model_is_its_cc_share_at_the_tier():
    # Sonnet 4.5 and GPT-6 are priced by expressions: their costly writes had no [WRITE COSTLY] warning at all.
    assert minara.write_cost(SONNET_45, TURN) == pytest.approx(30_439 * 3.75 / 1e6)
    assert minara.write_cost(SONNET_45, dict(TURN, cache_read=189_557)) == pytest.approx(30_439 * 7.5 / 1e6)
    assert minara.write_cost(GROK, TURN) == pytest.approx(30_439 * 2 / 1e6)  # an unpriced write is input
    assert minara.write_cost(CARBON, {"cache_read": 5}) is None
    huge = {"input_tokens": 4, "cache_write": 484_380}
    assert credits.write_cost(huge, "minara/claude-sonnet-4-5", SONNET_45) == pytest.approx(
        484_380 * 7.5 / 1e6 * 220, abs=0.01)
    assert credits.write_cost(huge, "minara/claude-sonnet-4-5", SONNET_45) >= credits.WRITE_WARN
