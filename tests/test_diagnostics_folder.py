"""A test run never writes into, or prunes, the upstream-400 diagnostics of the folder it was started from."""
import os

import direct
import upstream_diagnostics


def test_the_relay_keeps_its_diagnostics_in_logs_upstream_400_and_the_suite_moves_them_away():
    source = open(upstream_diagnostics.__file__, encoding="utf-8").read()
    assert 'DEFAULT_DIR = os.path.join("logs", "upstream-400")' in source  # what a relay without upstream_error_dir uses
    assert os.path.isabs(upstream_diagnostics.DEFAULT_DIR)  # conftest: a temporary folder for the whole run


def test_a_dump_without_a_folder_of_its_own_stays_out_of_the_folder_the_run_started_in(tmp_path, monkeypatch):
    """A run from a checkout filled its logs/upstream-400 with scripted refusals and pruned it to the newest 50 sets: from
    the folder the relay works in, that deleted the real diagnostics kept there."""
    monkeypatch.chdir(tmp_path)
    stamp = direct._dump_rejected_body(b'{"messages": []}', "bad", cfg={})
    assert stamp and not (tmp_path / "logs").exists()
    assert os.path.isfile(os.path.join(upstream_diagnostics.DEFAULT_DIR, stamp + "-body.json"))
