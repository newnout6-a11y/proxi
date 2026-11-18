"""A Minara account that refuses a request (402: no credits; 401/403: its token) leaves the pool, and the request moves
to another Minara account before the client sees anything (the cache is shared between Minara accounts).

The relay, its accounts and the Minara SSE transport are real; only the POST to Minara and /auth/me are fakes."""
import http.client
import json
import threading
import types

import pytest

import accounts
import apierr
import config
import console_log
import journal
import models
import orchestrator
import proxy

KEY = 'failover-local-key-0123456789012345678'
MODEL = 'minara/openai/failover-test'


class Response:
    def __init__(self, status, body=b'', headers=None):
        self.status_code, self._body = status, body
        self.headers = headers or {'content-type': 'text/event-stream' if status == 200 else 'application/json'}

    def iter_content(self, **_):
        yield self._body

    @property
    def content(self):
        return self._body

    @property
    def text(self):
        return self._body.decode()

    def close(self):
        pass


def ok_stream(text):
    data = {'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': 2}}
    return ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode()


@pytest.fixture
def relay(tmp_path, monkeypatch):
    prior = dict(models._MINARA_MODELS)
    for key in ('RETOOL_BASE', 'RETOOL_COOKIE', 'RETOOL_XSRF', 'RETOOL_PAGE_UUID', 'RELAY_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({
        'accounts': [{'account_id': 'failover-%d' % n, 'label': 'Minara %d' % n, 'upstream': 'minara',
                      'bearer_token': 'synthetic-bearer-%d' % n, 'minara_models': [{'id': 'openai/failover-test'}]}
                     for n in (1, 2)],
        'model': MODEL, 'relay_api_key': KEY, 'workspace_root': str(tmp_path), 'project_context': False,
        'exec_policy': 'off', 'conversation_state': False, 'cache_keepwarm_seconds': 0, 'session_refresh_interval': 0,
        'account_balance_interval': 0, 'minara_channel_retry_delays': [], 'journal_path': str(tmp_path / 'journal.jsonl'),
        'prompts_file': str(tmp_path / 'absent.json')}), encoding='utf-8')
    runtime = proxy.Runtime(config.load(str(path)))
    state = types.SimpleNamespace(first=None, refusal=402, posts=[], balance={'plan': '200', 'extra': '0'})

    def make(backend):
        def post(url, **kwargs):
            state.posts.append(backend.cfg['label'])
            if state.first is None:
                state.first = backend
            if backend is state.first and state.refusal:
                return Response(state.refusal, b'{"error":{"message":"refused"}}')
            return Response(200, ok_stream('FAILOVER_OK'))

        def auth_me(url, **kwargs):
            if backend is state.first and state.refusal in (401, 403):
                return types.SimpleNamespace(status_code=state.refusal, json=lambda: {})
            return types.SimpleNamespace(status_code=200, json=lambda: {'creditBalanceBreakdown': dict(state.balance)})
        return post, auth_me

    for backend in runtime.accounts.backends:
        backend._minara_post, backend._minara_get = make(backend)
    server = proxy.BoundedServer(('127.0.0.1', 0), proxy.make_handler(runtime), threading.BoundedSemaphore(8))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def ask(conversation):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=30)
        connection.request('POST', '/v1/chat/completions', json.dumps({
            'model': MODEL, 'messages': [{'role': 'user', 'content': 'hi'}]}),
            {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json', 'x-conversation-id': conversation})
        reply = connection.getresponse()
        body = json.loads(reply.read().decode())
        connection.close()
        return reply.status, body
    def ask_with(conversation, **fields):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=30)
        connection.request('POST', '/v1/chat/completions', json.dumps(dict({
            'model': MODEL, 'messages': [{'role': 'user', 'content': 'weather?'}]}, **fields)),
            {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json', 'x-conversation-id': conversation})
        reply = connection.getresponse()
        body = json.loads(reply.read().decode())
        connection.close()
        return reply.status, body

    def get(route):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=30)
        connection.request('GET', route, headers={'Authorization': 'Bearer ' + KEY})
        reply = connection.getresponse()
        body = json.loads(reply.read().decode() or '{}')
        connection.close()
        return reply.status, body
    yield types.SimpleNamespace(runtime=runtime, ask=ask, state=state, get=get, ask_with=ask_with)
    server.shutdown()
    server.server_close()
    runtime.close()
    models._MINARA_MODELS.clear()
    models._MINARA_MODELS.update(prior)


def test_a_402_moves_the_request_to_another_account_and_takes_the_first_out(relay):
    before = journal.JOURNAL.last_seq
    status, body = relay.ask('conv-402')
    assert status == 200, body
    assert body['choices'][0]['message']['content'] == 'FAILOVER_OK'
    assert len(relay.state.posts) == 2 and relay.state.posts[0] != relay.state.posts[1]
    assert relay.state.first.unavailable['reason'] == 'exhausted' and relay.state.first.unavailable['refused']
    events = [event for event in journal.JOURNAL.query(since=before) if event['kind'] == 'account.failover']
    assert len(events) == 1 and events[0]['data']['error'] == 'insufficient_credits'
    # The next conversation goes to the account that still has credits, without asking the refused one.
    relay.state.posts.clear()
    assert relay.ask('conv-next')[0] == 200 and relay.state.posts == [relay.state.posts[0]]
    assert relay.state.posts[0] != relay.state.first.cfg['label']


def test_a_rejected_token_moves_the_request_and_names_minara(relay):
    relay.state.refusal = 401
    status, body = relay.ask('conv-401')
    assert status == 200 and body['choices'][0]['message']['content'] == 'FAILOVER_OK'
    dead = relay.state.first.unavailable
    assert dead['reason'] == 'auth_dead' and dead['error'] == 'token_rejected'


def test_every_account_refused_says_minara_not_retool(relay):
    relay.state.refusal = 402
    relay.state.first = relay.runtime.accounts.backends[0]
    other = relay.runtime.accounts.backends[1]
    relay.runtime.accounts.mark_minara_refusal(other, apierr.ApiError(402, 'insufficient_credits', 'x'))
    status, body = relay.ask('conv-none')
    assert status == 402 and 'Minara' in body['error']['message'] and 'Retool' not in body['error']['message']


def test_request_sent_is_no_output_for_the_failover():
    out = orchestrator._FailoverOut(types.SimpleNamespace(put=lambda event: None))
    out.put({'type': 'request_sent'})
    out.put({'type': 'usage_start'})
    assert out.started is False
    out.put({'type': 'text', 'delta': 'x'})
    assert out.started is True


@pytest.mark.parametrize('status', [{'type': 'thinking_status', 'source': 'upstream'},
                                    {'type': 'thinking_status', 'source': 'relay'},
                                    {'type': 'thinking_heartbeat', 'elapsed': 6.5}])
def test_a_display_status_is_no_output_for_the_failover(status):
    # A02-01: a status says that the model reasons (or that time passes), and it carries nothing the reply is made of.
    # Counted as output, it made every unbilled refusal that came after it final: Retool's hourly limit inside a 200
    # after its thinking start, and every Minara 402/401/403 later than the relay's 1.5 s timer (16 of 26 live ones).
    out = orchestrator._FailoverOut(types.SimpleNamespace(put=lambda event: None))
    out.put({'type': 'usage_start'})
    out.put(status)
    assert out.started is False
    out.put({'type': 'thinking_start'})  # reasoning content follows: the client has the block it opens
    assert out.started is True


def test_a_402_that_comes_after_a_silence_still_moves_the_request(relay):
    # The refusal comes later than the 1.5 s after which 54b1023's timer took the turn for started (A02-01).
    def slow(post):
        def wrapper(url, **kwargs):
            import time
            time.sleep(1.7)
            return post(url, **kwargs)
        return wrapper

    for backend in relay.runtime.accounts.backends:
        backend._minara_post = slow(backend._minara_post)
    status, body = relay.ask('conv-slow-402')
    assert status == 200, body
    assert body['choices'][0]['message']['content'] == 'FAILOVER_OK'
    assert len(relay.state.posts) == 2 and relay.state.first.unavailable['reason'] == 'exhausted'


def manager(balance):
    return accounts.MinaraAccount({'bearer_token': 't'}, fetch=lambda: {'creditBalanceBreakdown': dict(balance)})


def test_a_402_before_the_first_read_holds_the_account_until_a_top_up():
    balance = {'plan': '-3', 'extra': '200'}
    account = manager(balance)
    account.note_refusal()  # before any read: the mark is unknown
    assert account.get_balance(force_refresh=True)['available'] == 0  # the first read sets it: held
    assert account.get_balance(force_refresh=True)['refused']
    balance['plan'] = '300'
    assert account.get_balance(force_refresh=True)['available'] == 500  # topped up: back


def test_a_second_402_never_lowers_the_mark():
    balance = {'plan': '-3', 'extra': '200'}
    account = manager(balance)
    assert account.get_balance()['available'] == 197
    account.note_refusal()
    assert account.get_balance(force_refresh=True)['available'] == 0
    account.note_refusal()  # another 402 while held: the shown 0 is no new mark
    assert account.get_balance(force_refresh=True)['available'] == 0 and account.get_balance()['refused']


class Backend:
    cfg = {'upstream': 'minara'}
    unavailable = None
    refresher = None


def test_a_rejected_token_on_a_balance_read_takes_the_account_out_and_an_accepted_one_brings_it_back():
    backend = Backend()
    answer = {'status': 401}

    def fetch():
        if answer['status'] != 200:
            raise apierr.upstream_auth_failed('Minara rejected the account token')
        return {'creditBalanceBreakdown': {'plan': '100', 'extra': '0'}}
    account = accounts.MinaraAccount({'bearer_token': 't'}, fetch=fetch)
    dispatcher = accounts.AccountDispatcher([backend], {'minara_min_credits': 15}, {backend: account}, clock=lambda: 1000)
    dispatcher.check_balance(backend)
    assert backend.unavailable['reason'] == 'auth_dead' and backend.unavailable['error'] == 'token_rejected'
    answer['status'] = 200
    dispatcher.check_balance(backend)
    assert backend.unavailable is None


def test_a_reported_cost_is_journaled_as_priced_and_an_uncached_claude_answer_is_said(relay):
    relay.state.refusal = None
    usage = {'prompt_tokens': 3000, 'completion_tokens': 2, 'cost': 0.01,
             'prompt_tokens_details': {'cached_tokens': 0, 'text_tokens': 0}}  # Bedrock: no cache_write_tokens

    def post(url, **kwargs):
        data = {'choices': [{'index': 0, 'delta': {'content': 'PRICED'}, 'finish_reason': 'stop'}], 'usage': usage}
        return Response(200, ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode())
    for backend in relay.runtime.accounts.backends:
        backend._minara_post = post
    entry = models.minara_entry(MODEL, relay.runtime.cfg)
    entry['cache'] = 'explicit'  # as a Claude route
    before = journal.JOURNAL.last_seq
    assert relay.ask('conv-priced')[0] == 200
    events = journal.JOURNAL.query(since=before, limit=500)
    priced = [event for event in events if event['kind'] == 'upstream.usage']
    assert priced and all(event['level'] == 'info' for event in priced)
    assert priced[-1]['data']['provider_cost'] == 0.01 and 'USD' in priced[-1]['message']
    assert [event['kind'] for event in events].count('cache.uncached_channel') == 1


def test_the_balance_of_a_minara_only_relay_is_an_answer(relay):
    status, body = relay.get('/v1/balance')
    assert status == 200, body
    assert body['available'] is None and body['minara']['available'] == 400  # 2 accounts x 200 credits


def test_a_marked_claude_reply_that_read_the_cache_and_wrote_nothing_is_said(relay, monkeypatch):
    """Minara bills a reply that writes nothing to the cache at the input price for its whole prompt, the part it read
    included (15 of 36 turns of one Fable 5.2 session, 2026-11-13): such a reply is a warning in the journal and a line
    on the console. A reply that wrote its tail is not."""
    relay.state.refusal = None
    lines = []
    monkeypatch.setattr(console_log, 'write', lambda line, **kwargs: lines.append(line))
    answers = [{'prompt_tokens': 120_000, 'completion_tokens': 2,
                'prompt_tokens_details': {'cached_tokens': 89_557, 'cache_write_tokens': 0}},
               {'prompt_tokens': 120_000, 'completion_tokens': 2,
                'prompt_tokens_details': {'cached_tokens': 89_557, 'cache_write_tokens': 30_439}}]

    def post(url, **kwargs):
        data = {'choices': [{'index': 0, 'delta': {'content': 'OK'}, 'finish_reason': 'stop'}], 'usage': answers.pop(0)}
        return Response(200, ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode())
    for backend in relay.runtime.accounts.backends:
        backend._minara_post = post
    models.minara_entry(MODEL, relay.runtime.cfg)['cache'] = 'explicit'  # as a Claude route
    before = journal.JOURNAL.last_seq
    assert relay.ask('conv-unwritten')[0] == 200 and relay.ask('conv-written')[0] == 200
    said = [event for event in journal.JOURNAL.query(since=before, limit=500) if event['kind'] == 'cache.tail_unwritten']
    assert len(said) == 1 and said[0]['level'] == 'warn'
    assert said[0]['data']['input_tokens'] == 30_443 and said[0]['data']['cache_read'] == 89_557
    assert said[0]['message'].endswith('Minara ничего не записала в кэш: 30,443 токенов промпта оплачены по цене входа')
    shown = [line for line in lines if '[cache.tail_unwritten]' in line]
    assert len(shown) == 1 and '30,443' in shown[0]


def test_each_request_of_a_forced_choice_asked_in_words_keeps_its_own_cost(relay):
    relay.state.refusal = None
    tools = [{'type': 'function', 'function': {'name': 'get_weather', 'parameters': {'type': 'object', 'properties': {}}}}]
    answers = [
        {'choices': [{'index': 0, 'delta': {'content': 'It is sunny.'}, 'finish_reason': 'stop'}],
         'usage': {'prompt_tokens': 100, 'completion_tokens': 5, 'cost': 0.40}},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'c1', 'function': {
            'name': 'get_weather', 'arguments': '{}'}}]}, 'finish_reason': 'tool_calls'}],
         'usage': {'prompt_tokens': 120, 'completion_tokens': 5, 'cost': 0.02}}]

    def post(url, **kwargs):
        data = answers.pop(0)
        return Response(200, ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode())
    for backend in relay.runtime.accounts.backends:
        backend._minara_post = post
    entry = models.minara_entry(MODEL, relay.runtime.cfg)
    entry['forced_tool_choice'] = False  # asked in words, as on every Claude route
    status, body = relay.ask_with('conv-forced', tools=tools, tool_choice='required')
    assert status == 200 and body['choices'][0]['message']['tool_calls'][0]['function']['name'] == 'get_weather'
    usage = [manager.usage() for manager in (relay.runtime.accounts.managers[backend]
                                             for backend in relay.runtime.accounts.backends)]
    spent = sum(item.get('provider_cost') or 0 for item in usage)
    attempts = sum(item.get('attempts') or 0 for item in usage)
    assert spent == pytest.approx(0.42) and attempts == 2


def test_balance_reads_coalesce_failures_are_cached_and_passive_reports_stay_off_the_network():
    import threading as _threading
    import time as _time
    reads = []
    gate = _threading.Event()

    def fetch():
        reads.append(1)
        gate.wait(2)
        return {'creditBalanceBreakdown': {'plan': '100', 'extra': '0'}}
    account = accounts.MinaraAccount({'bearer_token': 't'}, fetch=fetch)
    threads = [_threading.Thread(target=account.get_balance) for _ in range(6)]
    for thread in threads:
        thread.start()
    _time.sleep(0.3)
    gate.set()
    for thread in threads:
        thread.join()
    assert len(reads) == 1  # six callers, one read
    failing = []

    def broken():
        failing.append(1)
        raise ConnectionError('down')
    down = accounts.MinaraAccount({'bearer_token': 't'}, fetch=broken)
    for _ in range(5):
        assert down.get_balance()['error'] == 'ConnectionError'
    assert len(failing) == 1  # a failure is answered from memory for CACHE_SECONDS
    down.get_balance(force_refresh=True)
    assert len(failing) == 2  # the dispatcher's own reads still go


def test_the_402_recheck_reads_only_the_requests_provider_in_parallel():
    class Backend:
        def __init__(self, upstream):
            self.cfg = {'upstream': upstream}
            self.unavailable = None
            self.refresher = None
    minara_ones = [Backend('minara') for _ in range(6)]
    retool = Backend('retool')
    asked = []
    dispatcher = accounts.AccountDispatcher([*minara_ones, retool], {}, {b: object() for b in [*minara_ones, retool]},
                                            clock=lambda: 1000)
    for backend in [*minara_ones, retool]:
        dispatcher.state[backend]['reasons']['exhausted'] = {'available': 0}

    def check(backend):
        asked.append(backend)
        import time as _time
        _time.sleep(0.2)
        return True  # still spent
    dispatcher.check_balance_now = check
    import time as _time
    started = _time.monotonic()
    assert dispatcher.recheck_exhausted({'upstream': 'minara'}) is False
    assert set(asked) == set(minara_ones) and _time.monotonic() - started < 1.0  # in parallel, Retool's never read


def test_minaras_refusal_is_logged_as_upstreams_with_its_words_and_a_request_without_a_level_reads_default(relay):
    nested = json.dumps({'error': {'message': 'Provider returned error (' + json.dumps({'raw': json.dumps(
        {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Could not process image'}})}) + ')'}})
    for backend in relay.runtime.accounts.backends:
        backend._minara_post = lambda url, **kwargs: Response(400, nested.encode())
    before = journal.JOURNAL.last_seq
    status, body = relay.ask('conv-400')
    assert status == 400 and body['error']['message'].endswith('Could not process image')
    errors = [e for e in journal.JOURNAL.query(since=before, limit=200) if e['kind'] == 'http.error']
    assert errors and errors[-1]['data']['stage'] == 'upstream' and errors[-1]['data']['upstream_status'] == 400
    assert errors[-1]['data']['effort'] in ('default', None)  # no level was sent: never "none"


def test_a_channel_retry_is_journaled(tmp_path):
    import asyncio
    import minara
    answers = [Response(503, b''), Response(200, ok_stream('AFTER'))]
    session = types.SimpleNamespace(backend=types.SimpleNamespace(_minara_post=lambda *a, **kw: answers.pop(0)),
                                    _cancelled=False)
    before = journal.JOURNAL.last_seq

    async def run():
        return [event async for event in minara.iter_events({'bearer_token': 't', 'minara_channel_retry_delays': [0]},
                                                            session, {'model': 'openai/x'}, 'hi', '', None, None, 50)]
    events = asyncio.run(run())
    assert [e['delta'] for e in events if e['type'] == 'text'] == ['AFTER']
    retries = [e for e in journal.JOURNAL.query(since=before, limit=50) if e['kind'] == 'upstream.channel_retry']
    assert len(retries) == 1 and retries[0]['data']['status'] == 503


class CutResponse(Response):
    """A 200 whose stream breaks before any event: the request may have been billed."""

    def iter_content(self, **_):
        raise ConnectionError('stream cut')
        yield b''


def test_a_request_cut_after_it_left_is_not_sent_again_on_another_account(relay):
    relay.state.refusal = None
    relay.state.balance = {'plan': '-5', 'extra': '0'}  # the first account reads as spent: it would have failed over
    posts = []
    for backend in relay.runtime.accounts.backends:
        def post(url, _label=backend.cfg['label'], **kwargs):
            posts.append(_label)
            return CutResponse(200)
        backend._minara_post = post
    status, body = relay.ask('conv-cut')
    assert status >= 500 and len(posts) == 1  # one billed attempt, never a second on another account


def test_a_403_whose_token_auth_me_accepts_moves_nothing(relay):
    relay.state.refusal = 403
    first = relay.state.first = relay.runtime.accounts.backends[0]

    def auth_me_ok(url, **kwargs):
        return types.SimpleNamespace(status_code=200, json=lambda: {'creditBalanceBreakdown': {'plan': '200', 'extra': '0'}})
    for backend in relay.runtime.accounts.backends:
        backend._minara_get = auth_me_ok
    posts = []

    def post(url, **kwargs):
        posts.append(1)
        return Response(403, b'{"error":{"message":"blocked by a rule"}}')
    for backend in relay.runtime.accounts.backends:
        backend._minara_post = post
    status, body = relay.ask('conv-403')
    assert status == 502 and len(posts) == 1  # the request's own problem: no other account asked
    assert first.unavailable is None  # the token is fine: the account stays


def test_dead_bearer_tokens_are_never_tried_as_a_last_resort(relay):
    for backend in relay.runtime.accounts.backends:
        relay.runtime.accounts.state[backend]['reasons']['auth_dead'] = {'error': 'token_expired', 'reason': 'auth_dead'}
        backend.unavailable = relay.runtime.accounts.state[backend]['reasons']['auth_dead']
    relay.state.posts.clear()
    status, body = relay.ask('conv-dead')
    assert status == 503 and relay.state.posts == []  # answered at once, no token sent to be refused


def test_a_passive_report_says_a_failing_read_and_a_huge_exp_is_no_expiry():
    import account_report
    import minara_accounts
    account = accounts.MinaraAccount({'bearer_token': 't'}, fetch=lambda: {'creditBalanceBreakdown': {'plan': '10',
                                                                                                    'extra': '0'}})
    account.get_balance()
    account._fetch = lambda: (_ for _ in ()).throw(ConnectionError('down'))
    account.get_balance(force_refresh=True)
    assert account.last_error == 'ConnectionError'
    assert hasattr(account_report, 'collect')
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({'sub': 'u', 'exp': 10 ** 400}).encode()).decode().rstrip('=')
    assert minara_accounts.expires_at('eyJhbGciOiJIUzI1NiJ9.' + payload + '.sig') is None


def test_a_forced_call_asked_in_words_cut_after_it_left_is_not_sent_again(relay):
    # the gate of a forced call asked in words held request_sent back: a billed, cut attempt looked unsent and moved on
    relay.state.refusal = None
    relay.state.balance = {'plan': '-5', 'extra': '0'}
    entry = models.minara_entry(MODEL, relay.runtime.cfg)
    entry['forced_tool_choice'] = False
    posts = []
    for backend in relay.runtime.accounts.backends:
        def post(url, _label=backend.cfg['label'], **kwargs):
            posts.append(_label)
            return CutResponse(200)
        backend._minara_post = post
    tools = [{'type': 'function', 'function': {'name': 'get_weather', 'parameters': {'type': 'object', 'properties': {}}}}]
    status, body = relay.ask_with('conv-cut-forced', tools=tools, tool_choice='required')
    assert status >= 500 and len(posts) == 1


def test_a_minara_turn_shows_its_price_on_the_balance_line_and_in_the_journal(relay, monkeypatch):
    """The [balance] line under every Minara turn said spent=? though the [cache] line above it had the price; the same
    holds for a model priced by an expression with Gemini 4's `ai` (audio input), which the relay could not read."""
    relay.state.refusal = None
    lines = []
    monkeypatch.setattr(console_log, 'write', lambda line, **kwargs: lines.append(str(line)))
    usage = {'prompt_tokens': 120_000, 'completion_tokens': 20,
             'prompt_tokens_details': {'cached_tokens': 89_557, 'cache_write_tokens': 30_439}}

    def post(url, **kwargs):
        data = {'choices': [{'index': 0, 'delta': {'content': 'OK'}, 'finish_reason': 'stop'}], 'usage': usage}
        return Response(200, ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode())
    for backend in relay.runtime.accounts.backends:
        backend._minara_post = post
    entry = models.minara_entry(MODEL, relay.runtime.cfg)
    entry['pricing'] = {'quota_type': 0, 'billing_mode': 'tiered_expr', 'group_ratio': 1,
                        'billing_expr': 'p * 2 + c * 10 + cr * 0.1 + cc * 0.1875 + ai * 2'}
    before = journal.JOURNAL.last_seq
    assert relay.ask('conv-priced-line')[0] == 200
    credits = round((4 * 2 + 20 * 10 + 89_557 * 0.1 + 30_439 * 0.1875) / 1e6 * 220, 4)
    cache = [line for line in lines if '[cache]' in line]
    balance = [line for line in lines if '[balance]' in line]
    assert len(cache) == 1 and 'credits=%s' % console_log.amount(credits) in cache[0]
    assert len(balance) == 1 and 'spent=%s' % console_log.amount(credits) in balance[0]
    priced = [event for event in journal.JOURNAL.query(since=before, limit=500) if event['kind'] == 'upstream.usage']
    assert len(priced) == 1 and priced[0]['level'] == 'info' and priced[0]['data']['minara_credits'] == credits
    trace = next(item for item in journal.JOURNAL.turns() if item['turn_id'] == priced[0]['turn_id'])
    assert trace['minara_credits'] == credits and trace['credits_complete'] is True and trace['credits_unknown'] == 0


def test_a_minara_turn_whose_price_cannot_be_read_says_why(relay, monkeypatch):
    relay.state.refusal = None
    lines = []
    monkeypatch.setattr(console_log, 'write', lambda line, **kwargs: lines.append(str(line)))
    models.minara_entry(MODEL, relay.runtime.cfg)['pricing'] = {
        'quota_type': 0, 'billing_mode': 'tiered_expr', 'billing_expr': 'p * 2 + vid * 3'}
    before = journal.JOURNAL.last_seq
    assert relay.ask('conv-unreadable')[0] == 200
    said = [event for event in journal.JOURNAL.query(since=before, limit=500) if event['kind'] == 'upstream.usage']
    assert len(said) == 1 and said[0]['level'] == 'warn'
    assert said[0]['message'].endswith('цену по прайсу Minara не посчитать: формула цены не читается: неизвестная '
                                       'переменная vid')
    assert said[0]['data']['price_problem'].endswith('неизвестная переменная vid')
    assert any('[balance]' in line and 'spent=?' in line for line in lines)  # no figure made up


def test_an_attempt_cut_before_its_usage_is_not_said_to_have_tokens():
    parsed = types.SimpleNamespace(model='claude-opus-5-5', entry={})
    assert orchestrator.TurnRunner._unpriced(parsed, {}) == (
        'Попытка оборвалась до отчёта о расходе: стоимость неизвестна', {})
    assert orchestrator.TurnRunner._unpriced(parsed, {'input_tokens': 5, 'output_tokens': 0})[0] == (
        'Токены получены; стоимость upstream не сообщена')
