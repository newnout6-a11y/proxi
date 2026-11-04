"""Check installed Codex's strict Responses parser with no upstream inference.

Three synthetic one-request cases cover an unknown reasoning count, real zero,
and a positive count. User configuration is ignored for this process; no tools
are emitted, and no persistent provider/auth settings are changed.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adapters

MARKER = 'PROXI_CODEX_RESPONSES_OK'


def response_events(reasoning_tokens):
    class Writer:
        def __init__(self):
            self.events = []

        def event(self, name, payload):
            self.events.append((name, payload))

        def done(self):
            pass

    writer = Writer()
    parsed = SimpleNamespace(model='gpt-6-sol', show_thinking=True, system='', prompt='probe', tools=[])
    adapter = adapters.ResponsesAdapter(parsed, writer)
    adapter.begin()
    adapter.feed({'type': 'text', 'delta': MARKER})
    usage = {'input_tokens': 10, 'output_tokens': 12}
    if reasoning_tokens is not None:
        usage['reasoning_tokens'] = reasoning_tokens
    adapter.feed({'type': 'usage', 'usage': usage})
    adapter.end('stop')
    return writer.events


def assess_case(reasoning_tokens, exit_code, requests, stdout, stderr):
    parsing_error = 'failed to parse ResponseCompleted' in stdout + stderr
    return {'reasoning_tokens': reasoning_tokens, 'exit_code': exit_code, 'requests': requests,
            'parsing_error': parsing_error,
            'passed': exit_code == 0 and requests == 1 and MARKER in stdout and not parsing_error}


def probe(executable='codex', timeout=45):
    state = {'reasoning_tokens': None, 'requests': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            state['requests'] += 1
            self.rfile.read(int(self.headers.get('content-length', 0)))
            events = response_events(state['reasoning_tokens'])
            raw = (''.join('event: ' + name + '\ndata: ' + json.dumps(payload) + '\n\n'
                           for name, payload in events) + 'data: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('content-type', 'text/event-stream')
            self.send_header('content-length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    env = dict(os.environ, PROXI_OFFLINE_CODEX_KEY='offline-probe-key', PYTHONUTF8='1')
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix='proxi-codex-probe-') as work:
            args = [executable, 'exec', '--ignore-user-config', '--ephemeral', '--json',
                    '--skip-git-repo-check', '-C', work, '-m', 'gpt-6-sol',
                    '-c', 'model_provider="proxi_probe"', '-c', 'features.multi_agent=false',
                    '-c', 'features.remote_plugin=false',
                    '-c', 'model_providers.proxi_probe.name="Proxi offline parser probe"',
                    '-c', 'model_providers.proxi_probe.base_url=' + json.dumps(base + '/v1'),
                    '-c', 'model_providers.proxi_probe.wire_api="responses"',
                    '-c', 'model_providers.proxi_probe.env_key="PROXI_OFFLINE_CODEX_KEY"',
                    '-c', 'model_providers.proxi_probe.supports_websockets=false',
                    '-c', 'model_providers.proxi_probe.request_max_retries=0',
                    '-c', 'model_providers.proxi_probe.stream_max_retries=0',
                    'Return ' + MARKER + ' without using tools.']
            for count in (None, 0, 7):
                state.update(reasoning_tokens=count, requests=0)
                try:
                    result = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                            text=True, encoding='utf-8', errors='replace', timeout=timeout)
                    rows.append(assess_case(count, result.returncode, state['requests'],
                                            result.stdout, result.stderr))
                except subprocess.TimeoutExpired:
                    rows.append({'reasoning_tokens': count, 'timeout': True,
                                 'requests': state['requests'], 'passed': False})
    finally:
        server.shutdown()
        server.server_close()
    return {'client': 'codex', 'cases': rows, 'passed': all(row['passed'] for row in rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', default='codex')
    parser.add_argument('--timeout', type=float, default=45)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    result = probe(args.executable, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
