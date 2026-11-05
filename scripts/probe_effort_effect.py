"""Does a reasoning-effort control change anything observable on the Retool route? (paid, explicit)

The same short counting question is sent at every requested level through the surface the
relay itself uses for the model: the Assist list, without the agent marker, for models in
direct._ASSIST_ONLY, the App Builder list otherwise. Each row records what the route emitted:
thinking events and characters, reported tokens, seconds and credits.

Accounts are read once and read-only (audit_models.load_accounts). Sessions are never
renewed, requests are never retried and a 401/403 stops the run. Rows hold statuses and
counts only, plus the last line of the answer.
"""
import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import direct
import journal
import models
from auth import upstream_headers
from redact import redact
from scripts.audit_models import load_accounts, route_available

HEAD = 'You solve math questions. Think carefully, then answer with only the final integer.'
QUESTIONS = {
    'count': ('How many positive integers less than 1000 are divisible by 7 or by 11, but not by both? '
              'Answer with only the number.', '208'),
    'digits': ('How many integers from 1 to 100000 have a digit sum of exactly 25? '
               'Answer with only the number.', '5631'),
}
# Raw route controls to try, independent of what the catalog currently publishes.
LEVELS = {'anthropic': ('none', 'low', 'medium', 'high', 'max'), 'openai': ('low', 'medium', 'high'),
          'retool-fast': ('low', 'medium', 'high', 'max')}
DEFAULT_MODELS = ('claude-haiku-4-5-20251001', 'zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash',
                  'gpt-5-mini', 'gpt-5-nano')


def observe(cfg, entry, effort, *, question='count', timeout=120, max_tokens=6000):
    prompt, expected = QUESTIONS[question]
    provider = {'model': entry['id'], 'providerResourceName': models.provider_resource(cfg, entry)}
    if effort is not None:
        provider['reasoningEffort'] = effort
    body = direct.build_body(cfg, provider, HEAD, prompt, [], None, max_tokens)
    surface = 'assist' if entry['id'] in direct._ASSIST_ONLY else 'app-builder'
    if surface == 'assist':
        body.pop('metadata', None)
    headers = upstream_headers(cfg)
    headers['content-type'] = 'application/json'
    request = urllib.request.Request(cfg['base'].rstrip('/') + direct.ROUTE,
                                     data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')
    secrets = [cfg.get('xsrf', '')] + [part.strip().split('=', 1)[1]
                                       for part in cfg.get('cookie', '').split(';') if '=' in part]
    secrets += [str(cfg[key]) for key in models.PROVIDER_RESOURCE_KEY.values() if cfg.get(key)]
    row = dict(model=entry['id'], effort=effort, question=question, surface=surface,
               account=journal.tag(cfg.get('account_id') or cfg.get('page_uuid')), http=None,
               thinking_events=0, thinking_chars=0, text_chars=0, usage={})
    text = ''
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            row['http'] = response.status
            for index, raw in enumerate(response):
                if index >= 20000 or time.monotonic() - started > timeout:
                    row['error'] = 'observation limit exceeded'
                    break
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = str(event.get('type') or event.get('event') or '')
                if kind in ('thinking', 'signature') or 'reasoning' in kind:
                    row['thinking_events'] += 1
                    row['thinking_chars'] += len(str(event.get('partialThinking') or event.get('partialText') or ''))
                if kind == 'text':
                    text += str(event.get('partialText') or '')
                if event.get('event') == 'error' or kind == 'error':
                    row['error'] = redact(str(event.get('message') or event.get('error') or 'upstream error'),
                                          cookie_values=secrets)[:300]
                usage = event.get('usage')
                if isinstance(usage, dict):
                    row['usage'].update({key: value for key, value in usage.items() if type(value) in (int, float)})
                    details = usage.get('output_tokens_details')
                    if isinstance(details, dict) and type(details.get('reasoning_tokens')) in (int, float):
                        row['usage']['reasoningTokens'] = details['reasoning_tokens']
                if event.get('event') == 'end_of_stream' or kind == 'end_of_stream':
                    break
    except urllib.error.HTTPError as exc:
        row['http'] = exc.code
        row['error'] = redact(exc.read(4000).decode('utf-8', 'replace'), cookie_values=secrets)[:300]
        exc.close()
    except (OSError, TimeoutError) as exc:
        row['error'] = type(exc).__name__
    row['seconds'] = round(time.monotonic() - started, 2)
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    row['text_chars'] = len(text)
    row['answer'] = (lines[-1] if lines else '')[:24]
    row['correct'] = row['answer'].strip('*. ') == expected
    return row


def summarize(rows):
    groups = {}
    for row in rows:
        if row.get('http') == 200:
            groups.setdefault((row['model'], row['effort']), []).append(row)
    summary = []
    for (model, effort), items in groups.items():
        usage = [item['usage'] for item in items]
        summary.append(dict(
            model=model, effort=effort or 'omit', runs=len(items),
            output_tokens=[value.get('outputTokens') for value in usage],
            reasoning_tokens=[value.get('reasoningTokens') for value in usage],
            thinking_events=[item['thinking_events'] for item in items],
            thinking_chars=[item['thinking_chars'] for item in items],
            median_seconds=round(statistics.median(item['seconds'] for item in items), 2),
            correct=sum(item['correct'] for item in items)))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-paid', action='store_true', help='Required: requests can spend provider credits')
    parser.add_argument('--config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    parser.add_argument('--models', nargs='+', default=list(DEFAULT_MODELS))
    parser.add_argument('--efforts', nargs='+', help='Raw levels; "omit" sends none. Default: per-provider list')
    parser.add_argument('--question', choices=sorted(QUESTIONS), default='count',
                        help='count is a short problem; digits needs far more reasoning')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--max-credits', type=float, default=6.0, help='Stop once reported credits reach this')
    parser.add_argument('--max-tokens', type=int, default=6000)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--output', required=True, help='New JSONL evidence file; refuses to overwrite')
    args = parser.parse_args(argv)
    if not args.allow_paid:
        parser.error('--allow-paid is required')
    if (not 1 <= args.repeats <= 5 or not math.isfinite(args.timeout) or args.timeout <= 0
            or not math.isfinite(args.max_credits) or args.max_credits <= 0 or args.max_tokens < 256):
        parser.error('repeats must be 1..5; timeout, max-credits and max-tokens must be positive')
    accounts = load_accounts(args.config)
    rows, spent, stopped = [], 0.0, None
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as log:
        for index, name in enumerate(args.models):
            entry = models.resolve(name)[0]
            candidates = [cfg for cfg in accounts if route_available(cfg, entry)]
            if not candidates or stopped:
                continue
            cfg = candidates[index % len(candidates)]
            levels = args.efforts or ['omit', *LEVELS.get(entry['provider'], ())]
            for repeat in range(args.repeats):
                for effort in levels:
                    row = observe(cfg, entry, None if effort == 'omit' else effort, question=args.question,
                                  timeout=args.timeout, max_tokens=args.max_tokens)
                    row['repeat'] = repeat + 1
                    rows.append(row)
                    spent += row['usage'].get('creditsUsed', 0)
                    log.write(json.dumps(row, ensure_ascii=False) + '\n')
                    log.flush()
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                    if row['http'] in (401, 403):
                        stopped = 'authentication rejected; no renewal and no retry'
                    elif spent >= args.max_credits:
                        stopped = 'credit limit reached'
                    if stopped:
                        break
                if stopped:
                    break
    print(json.dumps({'summary': summarize(rows), 'requests': len(rows), 'credits_reported': round(spent, 5),
                      'stopped': stopped}, ensure_ascii=False), flush=True)
    return 1 if stopped else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
