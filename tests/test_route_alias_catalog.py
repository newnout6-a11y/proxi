"""A route alias names its targets in routes[]; what cannot be used is said, and the routes follow the catalogs.

Audits A08-04 and A12-10: a target that was no known model (a typo, another route, a Minara model no account served yet)
was dropped in silence, a route left without targets vanished (the client got "unknown model"), and the targets were
copies of the catalog entries at the relay's start: a refreshed Minara price or effort map, a model published later or
withdrawn, reached a route only after a restart. A16-01 (low): a Minara target named by its upstream id was dropped."""
import json

import config as config_module
import models
from test_route_alias_targets import MINARA, OPUS, RETOOL, load


def test_routes_that_name_unknown_targets_are_said_at_load_and_the_known_ones_are_kept(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, [RETOOL], [
        {"id": "smart", "targets": ["claude-opus-5.5x", OPUS]},
        {"id": "outer", "targets": ["smart"]},
        {"id": "empty", "targets": ["nope"]},
    ])
    assert set(cfg["_route_aliases"]) == {"smart"}
    assert [target["id"] for target in cfg["_route_aliases"]["smart"]["route_targets"]] == [OPUS]
    said = "\n".join(cfg["config_warnings"])
    for fragment in ("route 'smart': target 'claude-opus-5.5x' is no known model",
                     "route 'outer': target 'smart' is no known model", "route 'outer' has no known target",
                     "route 'empty' has no known target"):
        assert fragment in said


def test_a_route_follows_a_minara_catalog_refresh_and_a_model_published_after_the_start(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, [MINARA], [
        {"id": "fast", "targets": ["minara/claude-haiku-5-5"]},
        {"id": "late", "targets": ["minara/late-model"]},
    ])
    assert "late" not in models.route_alias_catalog(cfg)
    catalog = cfg["_minara_models"]
    before = models.resolve("fast", cfg=cfg)[0]["route_targets"][0]
    # minara_refresh.install rebuilds the registry in place: new entries under the same names.
    catalog["minara/claude-haiku-5-5"] = dict(catalog["minara/claude-haiku-5-5"], pricing={"quota_type": 0, "prompt": 99,
                                                                                           "completion": 99})
    catalog["minara/late-model"] = dict(catalog["minara/claude-haiku-5-5"], id="minara/late-model",
                                        upstream_model="vendor/late-model")
    after = models.resolve("fast", cfg=cfg)[0]["route_targets"][0]
    assert after["pricing"]["prompt"] == 99 and before["pricing"] != after["pricing"]
    assert models.resolve("late", cfg=cfg)[0]["route_targets"][0]["upstream_model"] == "vendor/late-model"
    # A model the catalog withdrew leaves the route, and the route without targets leaves the catalog.
    del catalog["minara/late-model"]
    assert "late" not in models.route_alias_catalog(cfg)


def test_a_minara_target_named_by_its_upstream_id_is_found(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, [RETOOL, MINARA],
               [{"id": "up", "targets": [OPUS, "minara/anthropic/claude-opus-5.5"]}])
    targets = cfg["_route_aliases"]["up"]["route_targets"]
    assert [target["id"] for target in targets] == [OPUS, "minara/claude-opus-5-5"]
    assert not cfg["config_warnings"] or "minara/anthropic" not in json.dumps(cfg["config_warnings"])
    assert config_module is not None
