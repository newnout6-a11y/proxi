"""The paid effort probe: explicit opt-in, read-only accounts, no retry, redacted evidence. Offline fixtures only."""
import io
import json
from pathlib import Path
import urllib.error

import pytest

import models
from scripts import probe_effort_effect as probe

GLM = 'zai-org/GLM-5.3'


def account(**extra):
    return dict(base='https://tenant.retool.com', page_uuid='app', account_id='account',
                cookie='accessToken=private-cookie; xsrfToken=private-xsrf', xsrf='private-xsrf', ua='test',
                provider_resource_name='__managed__', retool_fast_provider_resource_name='__managed__', **extra)


def stream(*events):
    response = io.BytesIO(b'\n'.join(json.dumps(event, ensure_ascii=False).encode() for event in events))
    response.status = 200
    return response


def answer(text='208', *, thinking=('abcd', 'ef'), usage=None):
    return stream(*[{'event': 'content_block_delta', 'type': 'thinking', 'partialThinking': piece}
                    for piece in thinking],
                  {'event': 'content_block_delta', 'type': 'text', 'partialText': text},
                  {'event': 'metadata', 'usage': usage or {'inputTokens': 60, 'outputTokens': 41, 'creditsUsed': 0.02}},
                  {'event': 'end_of_stream'})


def test_paid_probe_requires_the_flag_before_reading_any_account(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'load_accounts', lambda _: pytest.fail('must reject before the config is read'))
    with pytest.raises(SystemExit) as caught:
        probe.main(['--output', str(tmp_path / 'out.jsonl')])
    assert caught.value.code == 2 and not list(tmp_path.iterdir())


@pytest.mark.parametrize('flag', [['--repeats', '0'], ['--repeats', '9'], ['--max-credits', '0'],
                                  ['--timeout', 'nan'], ['--max-tokens', '10']])
def test_out_of_range_limits_are_rejected_before_any_request(tmp_path, monkeypatch, flag):
    monkeypatch.setattr(probe, 'load_accounts', lambda _: pytest.fail('must reject before the config is read'))
    with pytest.raises(SystemExit) as caught:
        probe.main(['--allow-paid', '--output', str(tmp_path / 'out.jsonl')] + flag)
    assert caught.value.code == 2


def test_observation_counts_thinking_tokens_and_checks_the_answer(monkeypatch):
    seen = []

    def http(request, timeout):
        seen.append(json.loads(request.data))
        return answer()

    monkeypatch.setattr(probe.urllib.request, 'urlopen', http)
    row = probe.observe(account(), models.resolve(GLM)[0], 'high')
    assert row['http'] == 200 and row['thinking_events'] == 2 and row['thinking_chars'] == 6
    assert row['usage']['outputTokens'] == 41 and row['usage']['creditsUsed'] == 0.02
    assert row['answer'] == '208' and row['correct'] is True and row['surface'] == 'app-builder'
    assert seen[0]['reasoningEffort'] == 'high' and 'metadata' in seen[0] and len(seen) == 1


def test_assist_models_use_the_assist_surface_and_an_omitted_level_sends_no_effort(monkeypatch):
    seen = []
    monkeypatch.setattr(probe.urllib.request, 'urlopen',
                        lambda request, timeout: seen.append(json.loads(request.data)) or answer('5631', thinking=()))
    row = probe.observe(account(), models.resolve('claude-haiku-4-5-20251001')[0], None, question='digits')
    assert row['surface'] == 'assist' and 'metadata' not in seen[0] and 'reasoningEffort' not in seen[0]
    assert row['correct'] is True and row['thinking_events'] == 0 and row['question'] == 'digits'


def test_a_wrong_answer_is_recorded_not_hidden(monkeypatch):
    monkeypatch.setattr(probe.urllib.request, 'urlopen', lambda request, timeout: answer('**196**'))
    row = probe.observe(account(), models.resolve(GLM)[0], 'low')
    assert row['correct'] is False and row['answer'] == '**196**'
    monkeypatch.setattr(probe.urllib.request, 'urlopen', lambda request, timeout: answer('**208**'))
    assert probe.observe(account(), models.resolve(GLM)[0], 'low')['correct'] is True


def test_401_stops_the_run_without_a_retry_or_a_renewal_and_the_evidence_is_redacted(tmp_path, monkeypatch):
    calls = []

    def http(request, **kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, 'auth', {}, io.BytesIO(b'private-cookie private-xsrf'))

    monkeypatch.setattr(probe.urllib.request, 'urlopen', http)
    monkeypatch.setattr(probe, 'load_accounts', lambda _: [account()])
    evidence = tmp_path / 'evidence.jsonl'
    result = probe.main(['--allow-paid', '--models', GLM, 'gpt-5-mini', '--repeats', '2', '--output', str(evidence)])
    assert result == 1 and len(calls) == 1
    rows = [json.loads(line) for line in evidence.read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 1 and rows[0]['http'] == 401 and 'private' not in json.dumps(rows)


def test_the_credit_limit_stops_the_run_after_the_request_that_reached_it(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.urllib.request, 'urlopen',
                        lambda request, timeout: answer(usage={'outputTokens': 5, 'creditsUsed': 4.0}))
    monkeypatch.setattr(probe, 'load_accounts', lambda _: [account()])
    result = probe.main(['--allow-paid', '--models', GLM, '--repeats', '3', '--max-credits', '1',
                         '--output', str(tmp_path / 'e.jsonl')])
    rows = (tmp_path / 'e.jsonl').read_text(encoding='utf-8').splitlines()
    assert result == 1 and len(rows) == 1


def strict(line):
    """A row as a strict JSON reader sees it: NaN and Infinity are not JSON."""
    def refuse(constant):
        raise ValueError('not JSON: ' + constant)
    return json.loads(line, parse_constant=refuse)


@pytest.mark.parametrize('credits', [pytest.param(float('nan'), id='nan'), pytest.param(float('inf'), id='infinity'),
                                     pytest.param(-50.0, id='negative'), pytest.param(10 ** 400, id='too-large')])
def test_a_credit_figure_that_cannot_be_added_up_stops_the_run_instead_of_disabling_the_limit(
        tmp_path, monkeypatch, capsys, credits):
    monkeypatch.setattr(probe.urllib.request, 'urlopen',
                        lambda request, timeout: answer(usage={'outputTokens': 5, 'creditsUsed': credits}))
    monkeypatch.setattr(probe, 'load_accounts', lambda _: [account()])
    result = probe.main(['--allow-paid', '--models', GLM, '--repeats', '3', '--max-credits', '1',
                         '--output', str(tmp_path / 'e.jsonl')])
    lines = (tmp_path / 'e.jsonl').read_text(encoding='utf-8').splitlines()
    assert result == 1 and len(lines) == 1
    row = strict(lines[0])
    assert row['invalid_usage'] == ['creditsUsed'] and 'creditsUsed' not in row['usage']
    assert row['usage']['outputTokens'] == 5
    summary = strict(capsys.readouterr().out.strip().splitlines()[-1])
    assert 'creditsUsed' in summary['stopped'] and summary['credits_reported'] == 0


def test_an_unusable_figure_other_than_credits_is_dropped_from_the_evidence_without_stopping_the_run(
        tmp_path, monkeypatch):
    monkeypatch.setattr(probe.urllib.request, 'urlopen',
                        lambda request, timeout: answer(usage={'outputTokens': float('nan'), 'creditsUsed': 0.1}))
    monkeypatch.setattr(probe, 'load_accounts', lambda _: [account()])
    result = probe.main(['--allow-paid', '--models', GLM, '--efforts', 'omit', '--repeats', '2', '--max-credits', '5',
                         '--output', str(tmp_path / 'e.jsonl')])
    rows = [strict(line) for line in (tmp_path / 'e.jsonl').read_text(encoding='utf-8').splitlines()]
    assert result == 0 and len(rows) == 2
    assert all(row['invalid_usage'] == ['outputTokens'] and row['usage'] == {'creditsUsed': 0.1} for row in rows)


def test_ordinary_usage_rows_carry_no_invalid_usage_marker(monkeypatch):
    monkeypatch.setattr(probe.urllib.request, 'urlopen', lambda request, timeout: answer())
    row = probe.observe(account(), models.resolve(GLM)[0], 'high')
    assert 'invalid_usage' not in row and row['usage']['creditsUsed'] == 0.02


def test_the_default_config_is_the_repository_one_whatever_the_working_directory(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(probe, 'load_accounts', lambda path: seen.append(path) or [])
    monkeypatch.setattr(probe.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('must not spend'))
    monkeypatch.chdir(tmp_path)
    assert probe.main(['--allow-paid', '--models', GLM, '--output', str(tmp_path / 'e.jsonl')]) == 0
    assert seen == [str(Path(probe.__file__).resolve().parents[1] / 'config.json')]
    probe.main(['--allow-paid', '--models', GLM, '--config', 'elsewhere.json', '--output', str(tmp_path / 'f.jsonl')])
    assert seen[-1] == 'elsewhere.json'


def test_a_model_without_a_provider_resource_is_skipped_not_probed(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('must not spend'))
    monkeypatch.setattr(probe, 'load_accounts', lambda _: [dict(account(), retool_fast_provider_resource_name='')])
    assert probe.main(['--allow-paid', '--models', GLM, '--output', str(tmp_path / 'e.jsonl')]) == 0
    assert (tmp_path / 'e.jsonl').read_text(encoding='utf-8') == ''


def test_existing_evidence_is_never_overwritten(tmp_path, monkeypatch):
    evidence = tmp_path / 'e.jsonl'
    evidence.write_text('existing evidence\n')
    monkeypatch.setattr(probe, 'load_accounts', lambda _: [account()])
    monkeypatch.setattr(probe.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('must not spend'))
    with pytest.raises(FileExistsError):
        probe.main(['--allow-paid', '--models', GLM, '--output', str(evidence)])
    assert evidence.read_text() == 'existing evidence\n'


def test_summary_groups_successful_rows_by_model_and_level():
    rows = [dict(model='m', effort=None, http=200, usage={'outputTokens': 10}, thinking_events=1, thinking_chars=5,
                 seconds=2.0, correct=True),
            dict(model='m', effort=None, http=200, usage={'outputTokens': 30}, thinking_events=3, thinking_chars=9,
                 seconds=4.0, correct=False),
            dict(model='m', effort='high', http=200, usage={'outputTokens': 50, 'reasoningTokens': 20},
                 thinking_events=0, thinking_chars=0, seconds=6.0, correct=True),
            dict(model='m', effort='high', http=429, usage={}, thinking_events=0, thinking_chars=0, seconds=0.1,
                 correct=False)]
    by_level = {item['effort']: item for item in probe.summarize(rows)}
    assert by_level['omit']['output_tokens'] == [10, 30] and by_level['omit']['median_seconds'] == 3.0
    assert by_level['omit']['correct'] == 1
    assert by_level['high']['runs'] == 1 and by_level['high']['reasoning_tokens'] == [20]
