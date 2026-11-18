"""The relay names itself to Retool by the version of Retool's current frontend (X-Retool-Client-Version).

Retool's frontend sends RETOOL_VERSION_STRING of its entry script as X-Retool-Client-Version (main-*.js of 2026-11-18:
`const RETOOL_VERSION="4.99.0"` and RETOOL_VERSION_STRING=`${RETOOL_VERSION}-dd22f13 (Build 424025)`; live answers named the
same in x-retool-api-version). The relay sent a constant of an older frontend, 4.84.0-16f3123 (Build 409813), fifteen minor
versions behind every browser. The catalog look reads the entry script anyway: it now keeps the version it names, and the
next requests carry it."""
import pytest

import auth
import retool_catalog
from test_retool_refresh import ENTRY, Frontend, isolated_catalog, refresher, registry  # noqa: F401 - a fixture

# The two places of the real entry script (main-CD0IqV1m.js @376971 and @380482), as they are written there.
ENTRY_SCRIPT = ('const RETOOL_VERSION="4.99.0";var a=1;const RETOOL_CLIENT_VERSION_HEADER="X-Retool-Client-Version",'
                'COMMIT_HASH="dd22f13",RETOOL_VERSION_STRING=`${RETOOL_VERSION}-dd22f13 (Build 424025)`,'
                'EMBEDDED_PAGE_VIEW_EVENT_X=1;import("./assets_vjs/other-1.js");import("./assets_vjs/index-Reg456.js")')
BASE = {"base": "https://tenant.invalid", "cookie": "accessToken=a", "xsrf": "x"}


@pytest.fixture(autouse=True)
def restore_version(monkeypatch):
    monkeypatch.setattr(auth, "CLIENT_VERSION", auth.CLIENT_VERSION)


def test_the_header_names_the_frontend_of_the_bundle_until_a_look_reads_a_newer_one():
    assert auth.upstream_headers(BASE)["x-retool-client-version"] == "4.99.0-dd22f13 (Build 424025)"


def test_the_version_is_read_from_the_entry_script_as_the_frontend_writes_it():
    assert retool_catalog.client_version(ENTRY_SCRIPT) == "4.99.0-dd22f13 (Build 424025)"
    assert retool_catalog.client_version("const nothing = 1;") is None


@pytest.mark.parametrize("value", [None, "", "5.0.0", "5.0.0-zzzz (Build 1)", "5.0.0-abcdef (Build 1)\r\nx-evil: 1"])
def test_only_a_version_of_the_frontend_s_shape_is_sent(value):
    assert auth.set_client_version(value) is False
    assert auth.upstream_headers(BASE)["x-retool-client-version"] == "4.99.0-dd22f13 (Build 424025)"


def test_a_look_keeps_the_version_of_the_current_frontend_and_the_requests_carry_it(tmp_path):
    frontend = Frontend(registry())
    frontend.pages[next(url for url in frontend.pages if url.endswith(ENTRY))] = ENTRY_SCRIPT.replace(
        "dd22f13 (Build 424025)", "0a1b2c3 (Build 430001)").replace('"4.99.0"', '"5.1.0"')
    value, _ = refresher(tmp_path, frontend, {})
    summary = value.run_once()
    assert summary["client_version"] == "5.1.0-0a1b2c3 (Build 430001)"
    assert auth.upstream_headers(BASE)["x-retool-client-version"] == "5.1.0-0a1b2c3 (Build 430001)"
    state = retool_catalog.read(tmp_path / retool_catalog.AUTO_NAME)
    assert state["client_version"] == "5.1.0-0a1b2c3 (Build 430001)"


def test_an_overlay_written_before_the_version_was_kept_reads_the_entry_script_once_more(tmp_path):
    frontend = Frontend(registry())
    entry_url = next(url for url in frontend.pages if url.endswith(ENTRY))
    frontend.pages[entry_url] = ENTRY_SCRIPT
    value, _ = refresher(tmp_path, frontend, {})
    value.run_once()
    path = tmp_path / retool_catalog.AUTO_NAME
    state = retool_catalog.read(path)
    del state["client_version"]  # an overlay of the relay before this change
    retool_catalog.write(path, state)
    frontend.read.clear()
    value.run_once()
    assert entry_url in frontend.read, "the entry script is read again once, for the version"
    frontend.read.clear()
    value.run_once()
    assert entry_url not in frontend.read, "and then not again while the entry script stays the same"


def test_the_overlay_gives_the_version_at_load(tmp_path):
    path = tmp_path / retool_catalog.AUTO_NAME
    retool_catalog.write(path, {"client_version": "5.2.0-feedbee (Build 431000)"})
    retool_catalog.apply_overlay(path)
    assert auth.upstream_headers(BASE)["x-retool-client-version"] == "5.2.0-feedbee (Build 431000)"
