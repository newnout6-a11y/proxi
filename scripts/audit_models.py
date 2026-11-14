"""Explicit live model audit using tiny, isolated synthetic questions.

Never starts the relay, refreshes credentials, enables keepwarm or reads project
context. Output contains synthetic answers and redacted errors, never headers.
"""
import argparse
import concurrent.futures
import json
import math
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import direct
import journal
import models
from auth import upstream_headers
from redact import redact


def load_accounts(path):
    """Read without load() side effects and without inheriting another tenant's resources."""
    saved = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    result = []
    for record in saved.get('accounts') or [saved]:
        if not record.get('enabled', True):
            continue
        cfg = dict(config.DEFAULTS, **saved)
        if record.get('base', '').rstrip('/') != saved.get('base', '').rstrip('/'):
            for key in models.PROVIDER_RESOURCE_KEY.values():
                cfg.pop(key, None)
        cfg.update(record)
        cfg['provider_access'] = record.get('provider_access', {})
        cfg.pop('accounts', None)
        cfg.pop('_config_path', None)
        cfg.update(_persist_auth=False, cache_keepwarm_seconds=0, session_refresh_interval=0,
                   exec_policy='off', project_context=False, upstream_empty_retries=0)
        result.append(cfg)
    return result


def route_available(cfg, entry):
    return bool(cfg.get(models.PROVIDER_RESOURCE_KEY[entry['provider']]))


def probe(cfg, entry, *, effort=None, timeout=90, max_tokens=2048, surface='sandpack'):
    nonce = uuid.uuid4().hex[:10]
    expected = 'AUDIT-' + nonce + '|42|cba|Привет'
    head = ('You are answering a small API compatibility test. Answer using exactly four '
            'pipe-separated fields: marker, arithmetic answer, reversed word, copied Cyrillic word. '
            'Do not use tools or add explanations. The required marker is AUDIT-' + nonce + '.')
    prompt = 'What is 17 + 25? Reverse abc. Copy Привет. Use the system-specified marker and format.'
    provider = {'model': entry['id'], 'providerResourceName': models.provider_resource(cfg, entry)}
    if effort is not None:
        provider['reasoningEffort'] = effort
    body = direct.build_body(cfg, provider, head, prompt, [], None, max_tokens)
    if surface == 'assist':
        body.pop('metadata', None)
    headers = upstream_headers(cfg)
    headers['content-type'] = 'application/json'
    req = urllib.request.Request(cfg['base'].rstrip('/') + direct.ROUTE,
                                 data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')
    secret_values = [cfg.get('xsrf', '')] + [part.strip().split('=', 1)[1]
                      for part in cfg.get('cookie', '').split(';') if '=' in part]
    secret_values += [str(cfg[key]) for key in models.PROVIDER_RESOURCE_KEY.values() if cfg.get(key)]
    row = dict(model=entry['id'], provider=entry['provider'],
               account=journal.tag(cfg.get('account_id') or cfg.get('page_uuid')),
               sent_effort=effort, surface=surface, http=None, end_of_stream=False, text='', usage={},
               thinking_events=0, first_event_seconds=None)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            row['http'] = response.status
            for index, raw in enumerate(response):
                if index >= 10000 or time.monotonic() - started > timeout:
                    row['error'] = 'audit observation limit exceeded'
                    break
                if not raw.strip():
                    continue
                if row['first_event_seconds'] is None:
                    row['first_event_seconds'] = round(time.monotonic() - started, 3)
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = str(event.get('type') or event.get('event') or '')
                if 'thinking' in kind or 'reasoning' in kind:
                    row['thinking_events'] += 1
                if kind == 'error' or event.get('event') == 'error':
                    row['error'] = redact(str(event.get('message') or event.get('error') or 'upstream error'),
                                          cookie_values=secret_values)[:1000]
                if kind == 'text':
                    row['text'] += str(event.get('partialText') or '')
                usage = event.get('usage')
                if isinstance(usage, dict):
                    row['usage'].update({key: value for key, value in usage.items()
                                         if type(value) in (int, float)})
                if event.get('reasoningEffort'):
                    row['reported_effort'] = event['reasoningEffort']
                if kind == 'end_of_stream' or event.get('event') == 'end_of_stream':
                    row['end_of_stream'] = True
                    break
    except urllib.error.HTTPError as exc:
        row['http'] = exc.code
        row['error'] = redact(exc.read(12000).decode('utf-8', 'replace'),
                              cookie_values=secret_values)[:1000]
        exc.close()
    except (OSError, TimeoutError) as exc:
        row['error'] = type(exc).__name__
    row['seconds'] = round(time.monotonic() - started, 3)
    row['output_correct'] = '|'.join(part.strip() for part in row['text'].strip().split('|')) == expected
    row['success'] = row['http'] == 200 and row['end_of_stream'] and not row.get('error') and row['output_correct']
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-paid', action='store_true', help='Required: requests can spend provider credits')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--models', nargs='+')
    parser.add_argument('--extra-model', action='append', default=[], metavar='PROVIDER:ID',
                        help='Explicit registry candidate outside the local catalog')
    parser.add_argument('--account', help='Choose the displayed account tag, without printing credentials')
    parser.add_argument('--surface', choices=['sandpack', 'assist'], default='sandpack')
    parser.add_argument('--efforts', nargs='+', help='Raw route controls; omitted tests each model at low/provider default')
    parser.add_argument('--parallel', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--output', required=True, help='New JSONL evidence file; refuses to overwrite')
    args = parser.parse_args(argv)
    if not args.allow_paid:
        parser.error('--allow-paid is required')
    if not 1 <= args.parallel <= 8 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('parallel must be 1..8 and timeout must be positive')
    entries = [models.resolve(name)[0] for name in args.models] if args.models else list(models.catalog())
    if args.extra_model and not args.models:
        entries = []
    for candidate in args.extra_model:
        provider, separator, name = candidate.partition(':')
        if not separator or provider not in models.PROVIDER_RESOURCE_KEY or not name:
            parser.error('--extra-model must be PROVIDER:ID with a known provider')
        entries.append({'id': name, 'provider': provider, 'reasoning_effort_levels': []})
    accounts = load_accounts(args.config)
    if args.account:
        accounts = [cfg for cfg in accounts
                    if journal.tag(cfg.get('account_id') or cfg.get('page_uuid')) == args.account]
        if not accounts:
            parser.error('unknown or disabled account tag')
    jobs = []
    for index, entry in enumerate(entries):
        candidates = [cfg for cfg in accounts if route_available(cfg, entry)]
        if not candidates:
            jobs.append((None, entry, None))
            continue
        cfg = candidates[index % len(candidates)]
        efforts = args.efforts or [('low' if 'low' in entry.get('reasoning_effort_levels', []) else None)]
        jobs.extend((cfg, entry, None if effort == 'omit' else effort) for effort in efforts)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as log:
        lock = threading.Lock()

        def run(job):
            cfg, entry, effort = job
            row = (dict(model=entry['id'], provider=entry['provider'], http=None, success=False,
                        skipped=True, reason='provider resource is not configured', usage={}) if cfg is None else
                   probe(cfg, entry, effort=effort, timeout=args.timeout, surface=args.surface))
            line = json.dumps(row, ensure_ascii=False)
            with lock:
                log.write(line + '\n')
                log.flush()
                print(line, flush=True)
            return row

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as executor:
            rows = list(executor.map(run, jobs))
    print(json.dumps({'finished': len(rows), 'passed': sum(row['success'] for row in rows),
                      'skipped': sum(bool(row.get('skipped')) for row in rows),
                      'credits_reported': sum(row['usage'].get('creditsUsed', 0) for row in rows),
                      'requests_without_credit_report': sum('creditsUsed' not in row['usage'] for row in rows
                                                           if not row.get('skipped'))}), flush=True)
    return 0 if all(row['success'] for row in rows) else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
