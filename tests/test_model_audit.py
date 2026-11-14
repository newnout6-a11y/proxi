"""Live audit safety and evidence parsing, with offline HTTP fixtures only."""
import io
import json
from types import SimpleNamespace
import urllib.error

import pytest

import models
from scripts import audit_models


def account(**extra):
    return dict(base='https://tenant.retool.com', page_uuid='app', account_id='account',
                cookie='accessToken=private-cookie; xsrfToken=private-xsrf', xsrf='private-xsrf',
                ua='test', provider_resource_name='__managed__', **extra)


def test_readonly_loader_ignores_disabled_and_cannot_inherit_cross_tenant_resources(tmp_path):
    path = tmp_path / 'private.json'
    saved = dict(account(openai_provider_resource_name='tenant-a-only'), accounts=[
        account(), dict(account(), base='https://other.retool.com', provider_resource_name='other-anthropic'),
        dict(account(), enabled=False)])
    raw = json.dumps(saved)
    path.write_text(raw)
    loaded = audit_models.load_accounts(path)
    assert len(loaded) == 2
    assert loaded[0]['openai_provider_resource_name'] == 'tenant-a-only'
    assert not loaded[1].get('openai_provider_resource_name')
    assert all(row['exec_policy'] == 'off' and row['project_context'] is False
               and row['cache_keepwarm_seconds'] == row['session_refresh_interval'] == 0
               and row['_persist_auth'] is False and '_config_path' not in row for row in loaded)
    assert path.read_text() == raw
    assert list(tmp_path.iterdir()) == [path]


def test_paid_audit_requires_explicit_flag_before_loading_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_models, 'load_accounts', lambda _: pytest.fail('must reject before config read'))
    with pytest.raises(SystemExit) as caught:
        audit_models.main(['--output', str(tmp_path / 'out.jsonl')])
    assert caught.value.code == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('surface', ['sandpack', 'assist'])
def test_probe_checks_answers_usage_terminal_and_surface_without_refresh(monkeypatch, surface):
    seen = []
    monkeypatch.setattr(audit_models.uuid, 'uuid4', lambda: SimpleNamespace(hex='0123456789abc'))

    def http(req, timeout):
        seen.append(json.loads(req.data))
        response = io.BytesIO(b'\n'.join(json.dumps(event, ensure_ascii=False).encode() for event in [
            {'type': 'thinking', 'partialText': 'summary'},
            {'type': 'text', 'partialText': 'AUDIT-0123456789 | 42 | cba | Привет'},
            {'usage': {'inputTokens': 30, 'outputTokens': 20, 'creditsUsed': .05}},
            {'event': 'end_of_stream'}]))
        response.status = 200
        return response

    monkeypatch.setattr(audit_models.urllib.request, 'urlopen', http)
    result = audit_models.probe(account(), models.resolve('claude-opus-5-5')[0],
                               effort='high', surface=surface)
    assert result['success'] is True and result['thinking_events'] == 1
    assert result['usage']['creditsUsed'] == .05
    assert seen[0]['reasoningEffort'] == 'high'
    assert ('metadata' in seen[0]) == (surface == 'sandpack')
    assert len(seen) == 1


def test_http_200_terminal_error_is_not_a_success_or_a_free_answer(monkeypatch):
    def http(*args, **kwargs):
        response = io.BytesIO(b'{"event":"error","message":"overloaded"}\n{"event":"end_of_stream"}\n')
        response.status = 200
        return response
    monkeypatch.setattr(audit_models.urllib.request, 'urlopen', http)
    result = audit_models.probe(account(), models.resolve('claude-opus-5-5')[0])
    assert result['http'] == 200 and result['end_of_stream'] is True
    assert result['success'] is False and result['error'] == 'overloaded'
    assert 'creditsUsed' not in result['usage']


def test_upstream_error_is_redacted_and_not_retried(monkeypatch):
    seen = []
    def http(req, **kwargs):
        seen.append(req)
        raise urllib.error.HTTPError(req.full_url, 401, 'auth', {},
                                     io.BytesIO(b'private-cookie private-xsrf'))
    monkeypatch.setattr(audit_models.urllib.request, 'urlopen', http)
    result = audit_models.probe(account(), models.resolve('claude-opus-5-5')[0])
    assert result['http'] == 401 and result['success'] is False
    assert 'private-cookie' not in result['error'] and 'private-xsrf' not in result['error']
    assert len(seen) == 1


def test_existing_evidence_is_never_overwritten_or_replayed(tmp_path, monkeypatch):
    evidence = tmp_path / 'out.jsonl'
    evidence.write_text('existing evidence\n')
    monkeypatch.setattr(audit_models, 'load_accounts', lambda _: [account()])
    monkeypatch.setattr(audit_models, 'probe', lambda *a, **kw: pytest.fail('must not spend'))
    with pytest.raises(FileExistsError):
        audit_models.main(['--allow-paid', '--models', 'claude-opus-5-5', '--output', str(evidence)])
    assert evidence.read_text() == 'existing evidence\n'


def test_unrouted_model_is_recorded_without_aborting_other_models(tmp_path, monkeypatch):
    evidence = tmp_path / 'audit.jsonl'
    calls = []
    monkeypatch.setattr(audit_models, 'load_accounts', lambda _: [account()])
    def probe(cfg, entry, **kw):
        calls.append(entry['id'])
        return dict(model=entry['id'], provider=entry['provider'], http=200, success=True, usage={'creditsUsed': .1})
    monkeypatch.setattr(audit_models, 'probe', probe)
    result = audit_models.main(['--allow-paid', '--models', 'gemini-2.5-pro', 'claude-opus-5-5',
                               '--output', str(evidence)])
    rows = [json.loads(line) for line in evidence.read_text(encoding='utf-8').splitlines()]
    assert calls == ['claude-opus-5-5']
    assert result == 1 and len(rows) == 2
    skipped = next(row for row in rows if row['model'] == 'gemini-2.5-pro')
    assert skipped['skipped'] is True and skipped['http'] is None and skipped['success'] is False
    assert 'private' not in json.dumps(rows)
