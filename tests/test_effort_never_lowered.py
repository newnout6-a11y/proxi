"""A route that lacks the effort a client asked for serves the lowest level above it, never a lower one while it has a higher
one; a level past the route's top is served at that top.

Codex test run of 2026-11-07: with allow_effort_downgrade on, xhigh on gpt-6.1-sol and gpt-6.1-terra (which have max) was
served as high (4bc53834, 7c35b682), because the nearest level was chosen with ties going downwards."""
import pytest

import models


@pytest.mark.parametrize("levels,asked,served", [
    (("low", "medium", "high", "max"), "xhigh", "max"),       # a tie: upwards
    (("low", "high"), "medium", "high"),                      # a tie: upwards
    (("low", "medium", "high"), "minimal", "low"),            # below the lowest level: the lowest
    (("low", "medium", "high"), "none", "low"),
    (("low", "medium", "high"), "max", "high"),               # past the top: the top, the most the model takes
    (("low", "medium", "high"), "xhigh", "high"),
    (("none", "low", "medium", "high", "max"), "minimal", "low"),
    (("low", "medium", "high", "max"), "high", "high"),       # a level the route has is itself
])
def test_the_level_served_is_never_below_the_level_asked_while_the_route_has_one_above(levels, asked, served):
    assert models.nearest_effort(levels, asked) == served


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol"])
def test_xhigh_on_a_route_with_max_is_served_as_max_when_the_setting_maps_levels(model):
    entry, level = models.resolve_tolerant(model, "xhigh", allow_downgrade=True)
    assert entry["id"] == model and level == "max"


def test_max_on_a_route_whose_top_is_high_is_served_at_that_top():
    levels = models.resolve("gpt-5.5")[0].get("reasoning_effort_levels") or ()
    assert "max" not in levels and "high" in levels
    assert models.resolve_tolerant("gpt-5.5", "max", allow_downgrade=True)[1] == "high"
