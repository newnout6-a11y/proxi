"""Startup banner, /v1/balance, /health and discovery describe the pool as it really is."""
import journal
import models
import proxy
import account_report
import pytest
from test_review_fixes_proxy import multi  # noqa: F401
from test_shutdown import runtime_config


def info(available, allocation=500.0, **extra):
    return dict(available=available, monthly_allocation=allocation, **extra)


# -- 35: a partial balance says so --------------------------------------------------------------
def test_aggregate_balance_counts_the_accounts_it_left_out():
    result = account_report.aggregate_balance([info(100.0), info(None), info(40.0, allocation=None)])
    assert result['available'] == 140.0 and result['monthly_allocation'] == 500.0
    assert (result['accounts_total'], result['unknown_accounts'], result['partial']) == (3, 1, True)


@pytest.mark.parametrize('infos,unknown,partial', [
    ([info(100.0), info(50.0)], 0, False),
    ([info(100.0), info(50.0, allocation=None)], 0, True),
    ([info(None), info(None)], 2, False),
    ([info(100.0), {'available': None, 'error': 'boom'}], 1, True),
    ([], 0, False),
])
def test_partial_means_the_sums_cover_only_part_of_the_pool(infos, unknown, partial):
    result = account_report.aggregate_balance(infos)
    assert result['unknown_accounts'] == unknown and result['partial'] is partial
    assert result['accounts_total'] == len(infos)


def test_balance_endpoint_reports_the_partial_total(multi):
    multi.managers_[1].balance['available'] = None
    status, body, _ = multi.call('/v1/balance')
    assert status == 200 and body['available'] == 100.0
    assert (body['accounts_total'], body['unknown_accounts'], body['partial']) == (2, 1, True)
    multi.managers_[1].balance['available'] = 7.0
    assert multi.call('/v1/balance')[1]['partial'] is False


def test_startup_balance_and_banner_mark_a_partial_total(multi):
    multi.managers_[1].balance['available'] = None
    balance = proxy.startup_balance(multi.runner_.runtime)
    assert balance['partial'] is True and balance['unknown_accounts'] == 1
    lines = proxy.banner_lines(multi.runner_.runtime.cfg, balance)
    assert any(line.startswith(' balance:       100.0 / 500.0') and line.endswith('(partial: 1 of 2 accounts unknown)')
               for line in lines)


def test_banner_balance_line_is_plain_when_the_total_is_complete(multi):
    lines = proxy.banner_lines(multi.runner_.runtime.cfg, proxy.startup_balance(multi.runner_.runtime))
    assert ' balance:       107.0 / 1000.0' in lines and not any('partial' in line for line in lines)


def test_banner_names_a_missing_allocation_and_a_degraded_upstream(tmp_path):
    cfg = runtime_config(tmp_path)
    cfg.update(model='claude-opus-5-5', reasoning_effort='auto')
    partial = {'available': 5.0, 'monthly_allocation': None, 'partial': True, 'unknown_accounts': 0, 'accounts_total': 2}
    assert any(line.endswith('(partial: monthly allocation unknown for some accounts)')
               for line in proxy.banner_lines(cfg, partial))
    assert ' upstream:      DEGRADED boom' in proxy.banner_lines(cfg, {'available': None, 'error': 'boom'})


# -- 36: a hidden default model is announced ------------------------------------------------------
def test_default_model_warning_says_why_the_default_is_not_served():
    cfg = {'model': 'gpt-6-astra', 'provider_resource_name': 'r', 'openai_provider_resource_name': 'o'}
    warning = models.default_model_warning(cfg)
    assert warning.startswith("default model 'gpt-6-astra' is not published (Requires a suitable BYOK provider")
    assert warning.endswith("requests without 'model' are refused")
    assert models.default_model_warning(dict(cfg, model='claude-opus-5-5')) is None
    assert 'no enabled account sets retool_fast_provider_resource_name' in models.default_model_warning(
        dict(cfg, model='zai-org/GLM-5.3'))
    assert 'retired' in models.default_model_warning(dict(cfg, model='claude-opus-4-20250514')).lower()
    assert 'no enabled account' in models.default_model_warning(
        {'model': 'claude-opus-5-5', 'provider_resource_name': 'r', 'accounts': [{'enabled': False}]})


def test_default_model_warning_covers_an_unknown_name_and_honours_show_unrouted():
    assert "'nope' is not in the catalog" in models.default_model_warning({'model': 'nope'})
    cfg = {'model': 'zai-org/GLM-5.3', 'provider_resource_name': 'r', 'show_unrouted_models': True}
    assert models.default_model_warning(cfg) is None


def test_startup_collects_the_hidden_default_and_config_warnings(multi):
    runtime = multi.runner_.runtime
    runtime.cfg["relay_api_key"] = "relay-" + "k" * 40  # long enough to need no warning of its own
    assert proxy.startup_warnings(runtime) == []
    runtime.cfg.update(model='claude-fable-5', config_warnings=['top-level provider_access is ignored'])
    warnings = proxy.startup_warnings(runtime)
    assert warnings[0].startswith("default model 'claude-fable-5' is not published")
    assert warnings[1] == 'top-level provider_access is ignored'
    lines = proxy.banner_lines(runtime.cfg, proxy.startup_balance(runtime), warnings)
    assert [line for line in lines if line.startswith(' WARNING:')] == [' WARNING:       ' + text for text in warnings]


def test_main_writes_the_warning_to_the_journal_and_the_banner(tmp_path, monkeypatch, capsys):
    import signal
    import threading
    monkeypatch.setattr(journal, 'JOURNAL', journal.Journal())
    monkeypatch.setattr(threading, 'excepthook', threading.excepthook)
    cfg = runtime_config(tmp_path)
    cfg.update(model='claude-fable-5', provider_resource_name='__managed__',
               relay_api_key='fixture-relay-key-' + 'x' * 20)  # long: the one warning here is the model's
    runtime = proxy.Runtime(cfg)
    monkeypatch.setattr(runtime.accounts, 'start', lambda: None)
    monkeypatch.setattr(runtime.credits, 'get_balance', lambda **_kw: {'available': 100})
    handlers, stopped = {}, threading.Event()

    class Server:
        def __init__(self, *_args):
            pass

        def serve_forever(self):
            handlers[signal.SIGINT](signal.SIGINT, None)
            assert stopped.wait(2)

        def shutdown(self):
            stopped.set()

        def server_close(self):
            pass

    monkeypatch.setattr(proxy, 'Runtime', lambda _cfg: runtime)
    monkeypatch.setattr(proxy, 'BoundedServer', Server)
    monkeypatch.setattr(proxy, 'startup_config', lambda _argv: cfg)
    monkeypatch.setattr(proxy.signal, 'signal', lambda sig, handler: handlers.__setitem__(sig, handler))
    try:
        proxy.main([])
    finally:
        runtime.close()
    events = [event for event in journal.JOURNAL.query() if event['kind'] == 'relay.warning']
    assert len(events) == 1 and events[0]['level'] == 'warn'
    assert "default model 'claude-fable-5' is not published" in events[0]['message']
    assert " WARNING:       default model 'claude-fable-5' is not published" in capsys.readouterr().out


# -- 63 and the /health session: counts and sessions that exist --------------------------------------
def test_health_counts_the_published_models_not_the_whole_registry(multi):
    status, health, _ = multi.call('/health', key=None)
    published = multi.call('/v1/models')[1]['data']
    assert status == 200 and health['models'] == len(published) < len(models.catalog())
    assert 'claude-fable-5' not in {row['id'] for row in published}


def test_health_models_follow_the_providers_the_accounts_can_serve(multi):
    for backend in multi.backends:
        for key in models.PROVIDER_RESOURCE_KEY.values():
            backend.cfg.pop(key, None)
        backend.cfg['provider_resource_name'] = 'anthropic'
    rows = multi.call('/v1/models')[1]['data']
    assert rows and {row['owned_by'] for row in rows} == {'anthropic'}
    assert multi.call('/health', key=None)[1]['models'] == len(rows)


def test_health_describes_the_account_that_serves_when_the_first_one_is_switched_off(multi):
    first, second = multi.backends
    first.refresher.last_success_at, second.refresher.last_success_at = 111.0, 222.0
    assert multi.call('/health', key=None)[1]['session']['last_success_at'] == 111.0
    first.cfg['enabled'] = False
    assert multi.call('/health', key=None)[1]['session']['last_success_at'] == 222.0
    first.cfg['enabled'] = True
    first.unavailable = {'reason': 'auth_dead'}
    assert multi.call('/health', key=None)[1]['session']['last_success_at'] == 222.0


def test_serving_entry_falls_back_to_an_enabled_account_and_then_to_the_first(multi):
    first, second = multi.backends
    entries = account_report.collect(multi.runner_.runtime)
    assert account_report.serving_entry(entries)['backend'] is first
    first.unavailable = {'reason': 'exhausted', 'available': 0.0}
    second.unavailable = {'reason': 'exhausted', 'available': 0.0}
    entries = account_report.collect(multi.runner_.runtime)
    assert not any(entry['routable'] for entry in entries)
    assert account_report.serving_entry(entries)['backend'] is first
    first.cfg['enabled'] = False
    entries = account_report.collect(multi.runner_.runtime)
    assert account_report.serving_entry(entries)['backend'] is second
    second.cfg['enabled'] = False
    assert account_report.serving_entry(account_report.collect(multi.runner_.runtime))['backend'] is first


# -- 37: with every account switched off, discovery is empty by design --------------------------------
@pytest.mark.parametrize('path,key', [('/v1/models', 'data'), ('/codex/models', 'models')])
def test_discovery_is_empty_while_every_account_is_switched_off(multi, path, key):
    assert multi.call(path)[1][key]
    for backend in multi.backends:
        backend.cfg['enabled'] = False
    status, body, _ = multi.call(path)
    assert status == 200 and body[key] == []
    multi.backends[1].cfg['enabled'] = True
    assert multi.call(path)[1][key]


@pytest.mark.parametrize("key", ["short-key", "x" * 31])
def test_a_short_relay_key_is_warned_about_by_its_length_and_never_shown(multi, key):
    """Wrong keys from 127.0.0.1 have no attempt budget, and a tunnel on the machine makes every client 127.0.0.1."""
    runtime = multi.runner_.runtime
    runtime.cfg["relay_api_key"] = key
    (warning,) = proxy.startup_warnings(runtime)
    assert "%d characters" % len(key) in warning and key not in warning


def test_a_relay_key_of_32_characters_or_more_needs_no_warning(multi):
    runtime = multi.runner_.runtime
    runtime.cfg["relay_api_key"] = "x" * 32
    assert proxy.startup_warnings(runtime) == []
