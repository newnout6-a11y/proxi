import asyncio
import json
from types import SimpleNamespace

import pytest

import apierr
import minara


def chunk(delta=None, finish=None, usage=None):
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return json.dumps(value)


def test_sse_fragmented_utf8_crlf_multiline_and_comments():
    raw = ': ping\r\ndata: {"a":\r\ndata: "я"}\r\n\r\ndata: [DONE]\n\n'.encode()
    assert list(minara.sse_data([raw[i:i+1] for i in range(len(raw))])) == ['{"a":\n"я"}', '[DONE]']


@pytest.mark.parametrize('raw', [b'data: x', b'data: x\n', b'data: \xff\n\n'])
def test_sse_refuses_truncated_or_invalid_events(raw):
    with pytest.raises(apierr.ApiError):
        list(minara.sse_data([raw]))


def test_sse_event_and_line_limits():
    with pytest.raises(apierr.ApiError):
        list(minara.sse_data([b'data: 123456\n\n'], max_bytes=5))
    with pytest.raises(apierr.ApiError):
        list(minara.sse_data([b'data: a\ndata: b\n\n'], max_bytes=1))


def test_build_payload_keeps_roles_images_and_tool_schema_without_mutation():
    history = [
        {"role": "user", "content": [{"type": "image", "imageData": "YWJj", "mimeType": "image/png"}]},
        {"role": "assistant", "content": [{"type": "thinking", "signature": "private"}, {"type": "text", "text": "run"},
            {"type": "tool_call", "toolCallId": "call1", "toolName": "echo", "toolInput": {"word": "OK"}}]},
        {"role": "tool", "content": {"toolCallId": "call1", "toolResult": "OK"}}]
    tool = {"name": "echo", "parameters": {"type": "object", "properties": {"word": {"type": "string", "enum": ["OK"]}}}, "strict": True}
    before = json.dumps(history)
    body = minara.build_body({}, {"model": "minara/openai/gpt-oss-20b", "reasoning_effort": "low"}, 'head', 'ignored', [tool], 'echo', 100, session=SimpleNamespace(_native_client_messages=history))
    assert body['model'] == 'openai/gpt-oss-20b'
    assert [m['role'] for m in body['messages']] == ['system', 'user', 'assistant', 'tool']
    assert body['messages'][1]['content'][0]['image_url']['url'] == 'data:image/png;base64,YWJj'
    assert body['messages'][2]['tool_calls'][0]['function']['arguments'] == '{"word": "OK"}'
    assert 'private' not in json.dumps(body)
    assert body['tools'][0]['function']['parameters'] == tool['parameters']
    assert body['tool_choice']['function']['name'] == 'echo'
    assert body['reasoning_effort'] == 'low'
    assert json.dumps(history) == before


def test_reasoning_is_not_duplicated_and_tool_fragments_are_cumulative():
    parser = minara.EventParser()
    events = parser.feed(chunk({'reasoning': 'think', 'reasoning_details': [{'text': 'think'}], 'tool_calls': [
        {'index': 1, 'id': 'c', 'function': {'name': 'echo', 'arguments': '{"a":'}}]}))
    assert [e['type'] for e in events] == ['thinking_start', 'thinking', 'tool_call']
    assert events[-1]['arguments'] == '{"a":'
    events = parser.feed(chunk({'tool_calls': [{'index': 1, 'function': {'arguments': '1}'}}]}, 'tool_calls'))
    assert events[-1]['arguments'] == '{"a":1}'
    assert parser.feed(chunk(finish='tool_calls', usage={'prompt_tokens': 4, 'completion_tokens': 2, 'cost': 0.01})) == [
        {'type': 'usage', 'usage': {'input_tokens': 4, 'output_tokens': 2, 'provider_cost': 0.01,
                                    'provider_cost_currency': 'USD'}}]
    assert parser.feed('[DONE]') == [{'type': 'tool_call', 'id': 'c', 'name': 'echo', 'arguments': '{"a":1}'}, {'type': 'end', 'reason': 'tool_calls'}]


@pytest.mark.parametrize('data', ['bad json', '[]', '{"error":{"message":"secret"}}', '[DONE]', chunk({'tool_calls': [{'index': -1}]}), chunk(finish='unexpected')])
def test_parser_refuses_invalid_and_incomplete_stream(data):
    with pytest.raises(apierr.ApiError):
        minara.EventParser().feed(data)


def test_usage_unknown_not_zero_and_bad_counts_filtered():
    assert minara.normalize_usage({}) == {}
    assert minara.normalize_usage({'prompt_tokens': True, 'completion_tokens': -1, 'cost': float('inf')}) == {}
    assert minara.normalize_usage({'cost': 0, 'prompt_tokens_details': {'cached_tokens': 0}, 'completion_tokens_details': {'reasoning_tokens': 3}}) == {'provider_cost': 0, 'provider_cost_currency': 'USD', 'cache_read': 0, 'reasoning_tokens': 3}


class Response:
    status_code = 200
    headers = {'content-type': 'text/event-stream'}
    def __init__(self, frames):
        self.frames = frames
        self.closed = False
    def iter_content(self, chunk_size):
        yield from self.frames
    def close(self):
        self.closed = True


async def collect(response, **cfg):
    seen = []
    def post(url, **kwargs):
        seen.append((url, kwargs))
        return response
    session = SimpleNamespace(backend=SimpleNamespace(_minara_post=post), _cancelled=False)
    events = [event async for event in minara.iter_events({'bearer_token': 'secret', **cfg}, session, {'model': 'minara/openai/x'}, 'hi', '', None, None, 10)]
    return events, seen, session


@pytest.mark.asyncio
async def test_transport_chrome_bearer_no_redirect_no_retries_and_closes():
    response = Response([('data: '+chunk({'content':'OK'}, 'stop')+'\n\ndata: [DONE]\n\n').encode()])
    events, seen, session = await collect(response)
    assert events == [{'type':'usage_start'}, {'type':'text','delta':'OK'}, {'type':'end','reason':'stop'}]
    assert response.closed
    assert len(seen) == 1
    url, kwargs = seen[0]
    assert url == 'https://api.minara.ai/v1/chat/completions'
    assert kwargs['headers']['Authorization'] == 'Bearer secret'
    assert kwargs['impersonate'] == 'chrome131' and kwargs['stream'] is True
    assert kwargs['allow_redirects'] is False
    assert session._active_direct_abort is None


@pytest.mark.asyncio
async def test_transport_truncation_is_error_not_stop():
    with pytest.raises(apierr.ApiError, match='before DONE'):
        await collect(Response([('data: '+chunk({'content':'partial'})+'\n\n').encode()]))


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code', [(401,'upstream_auth_error'), (403,'upstream_auth_error'), (429,'rate_limit_error'), (402,'insufficient_credits'), (500,'service_unavailable'), (302,'upstream_error')])
async def test_http_error_does_not_leak_body_or_retry(status, code):
    response = Response([])
    response.status_code = status
    with pytest.raises(apierr.ApiError) as raised:
        await collect(response)
    assert raised.value.code == code
    assert 'secret' not in raised.value.message
    assert response.closed
    if status == 500:
        # An empty 500 is one of Minara's channel windows: unbilled, retried shortly, then a 503 with Retry-After.
        assert raised.value.retry_after == minara.CHANNEL_RETRY_AFTER


@pytest.mark.asyncio
async def test_transport_cancellation_closes_response():
    response = Response([('data: '+chunk({'content':'OK'})+'\n\n').encode()])
    session = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: response), _cancelled=False)
    stream = minara.iter_events({'bearer_token':'secret'},session,{'model':'x'},'hi','',None,None,5)
    assert (await anext(stream))['type'] == 'usage_start'
    assert (await anext(stream))['type'] == 'text'
    await stream.aclose()
    assert response.closed
    assert session._active_direct_abort is None

def test_generated_followup_is_not_discarded_by_canonical_cold_replay():
    session = SimpleNamespace(_native_client_messages=[{'role': 'user', 'content': [{'type': 'text', 'text': 'original'}]}],
                              _native_allow_cold_replay=False)
    body = minara.build_body({}, {'model': 'minara/openai/x'}, '', 'result UNIQUE_RESULT', None, 'none', 10,
                             ['original', 'Assistant: command', 'result UNIQUE_RESULT'], session)
    assert [message['content'] for message in body['messages']] == [
        'original', 'Assistant: command', 'result UNIQUE_RESULT']


@pytest.mark.asyncio
@pytest.mark.parametrize('during_stream', [False, True])
async def test_curl_timeout_is_sanitized_504_without_retry(during_stream):
    from curl_cffi.requests.exceptions import Timeout
    calls = []
    class TimedOut(Response):
        def iter_content(self, chunk_size):
            raise Timeout('Authorization: Bearer private-token')
            yield
    def post(*args, **kwargs):
        calls.append(1)
        if not during_stream:
            raise Timeout('Authorization: Bearer private-token')
        return TimedOut([])
    session = SimpleNamespace(backend=SimpleNamespace(_minara_post=post), _cancelled=False)
    with pytest.raises(apierr.ApiError) as caught:
        async for _ in minara.iter_events({'bearer_token': 'test'}, session, {'model': 'x'}, 'hi', '', None, None, 5):
            pass
    assert (caught.value.status, caught.value.code) == (504, 'upstream_timeout')
    assert 'private-token' not in caught.value.message
    assert calls == [1]


@pytest.mark.asyncio
async def test_real_curl_cancellation_does_not_block_other_conversations():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from curl_cffi import requests
    release = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(('data: ' + chunk({'content': 'hello'}) + '\n\n').encode())
            self.wfile.flush()
            release.wait(2)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    closed = threading.Event()
    def post(*args, **kwargs):
        response = requests.post('http://127.0.0.1:%d/' % server.server_port, **kwargs)
        close = response.close
        def observed_close():
            try:
                close()
            finally:
                closed.set()
        response.close = observed_close
        return response
    session = SimpleNamespace(backend=SimpleNamespace(_minara_post=post), _cancelled=False)
    stream = minara.iter_events({'bearer_token': 'synthetic', 'upstream_timeout': 3}, session,
                               {'model': 'x'}, 'hi', '', None, None, 5)
    try:
        await anext(stream)
        assert (await anext(stream))['delta'] == 'hello'
        # A timer on the same event loop must run while curl still waits for the
        # peer; the old synchronous close waited the server's full two seconds.
        loop = asyncio.get_running_loop()
        heartbeat = loop.create_future()
        loop.call_later(0.05, lambda: heartbeat.set_result(True) if not heartbeat.done() else None)
        start = loop.time()
        await stream.aclose()
        await heartbeat
        assert loop.time() - start < 0.5
        assert session._active_direct_abort is None
    finally:
        release.set()
        await stream.aclose()
        await asyncio.to_thread(closed.wait, 3)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        serving.join(2)
    assert closed.is_set()

@pytest.mark.parametrize('read,write,uncached', [(80, 0, 20), (60, 20, 20), (0, 0, 100)])
@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_cached_prompt_normalization_preserves_total_in_all_dialects(read, write, uncached, dialect):
    import adapters
    import credits
    usage = minara.normalize_usage({'prompt_tokens': 100, 'completion_tokens': 4,
                                   'prompt_tokens_details': {'cached_tokens': read, 'cache_write_tokens': write}})
    assert usage['input_tokens'] == uncached
    assert credits.cost_breakdown(usage, 'minara/openai/x')['prompt_tokens'] == 100
    parsed = SimpleNamespace(system='', prompt='', tools=[], model='minara/openai/x', show_thinking=False)
    adapter = {'chat': adapters.ChatAdapter, 'responses': adapters.ResponsesAdapter,
               'anthropic': adapters.AnthropicAdapter}[dialect](parsed)
    adapter.feed({'type': 'usage', 'usage': usage})
    result = adapter.payload('stop')['usage']
    if dialect == 'chat':
        assert result['prompt_tokens'] == 100
        assert result['total_tokens'] == 104
    elif dialect == 'responses':
        assert result['input_tokens'] == 100
        assert result['total_tokens'] == 104
    else:
        assert result['input_tokens'] == uncached
        assert result['input_tokens'] + result['cache_read_input_tokens'] + result['cache_creation_input_tokens'] == 100


def test_cache_without_total_remains_unknown_and_inconsistent_details_do_not_inflate_total():
    assert minara.normalize_usage({'prompt_tokens_details': {'cached_tokens': 8}}) == {'cache_read': 8}
    assert minara.normalize_usage({'prompt_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 8}}) == {'input_tokens': 5}


@pytest.mark.parametrize('raw', ['[]', 'null', '123', '{"x":NaN}', '{"x":Infinity}', '{"x":1e400}'])
def test_nonobject_or_nonfinite_tool_arguments_are_not_marked_complete(raw):
    parser = minara.EventParser()
    parser.feed(chunk({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'echo', 'arguments': raw}}]}, 'tool_calls'))
    assert parser.feed('[DONE]')[0]['invalid'] is True

@pytest.mark.asyncio
async def test_canceled_preheader_burst_keeps_physical_worker_budget_and_recovers():
    import threading
    release = threading.Event()
    entered = threading.Event()
    active = peak = posts = 0
    lock = threading.Lock()
    class Delayed(Response):
        def close(self):
            super().close()
    def post(*args, **kwargs):
        nonlocal active, peak, posts
        with lock:
            active += 1
            posts += 1
            peak = max(peak, active)
        entered.set()
        release.wait(3)
        with lock:
            active -= 1
        return Delayed([('data: ' + chunk({'content': 'OK'}, 'stop') + '\n\ndata: [DONE]\n\n').encode()])
    async def begin():
        # A different backend each time proves this is a runtime, not account, budget.
        session = SimpleNamespace(backend=SimpleNamespace(_minara_post=post), _cancelled=False)
        stream = minara.iter_events({'bearer_token': 'test', 'max_workers': 1}, session,
                                   {'model': 'x'}, 'hi', '', None, None, 5)
        await anext(stream)
        return stream, asyncio.create_task(anext(stream))
    stream, task = await begin()
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()
        for _ in range(24):
            queued, waiting = await begin()
            await asyncio.sleep(0.002)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            await queued.aclose()
        assert posts == peak == active == 1
        release.set()
        for _ in range(100):
            if not active:
                break
            await asyncio.sleep(0.01)
        recovered, pending = await begin()
        first = await asyncio.wait_for(pending, 2)
        assert first == {'type': 'text', 'delta': 'OK'}
        await recovered.aclose()
        assert posts == 2 and peak == 1
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_worker_slot_stays_reserved_until_slow_response_close_finishes():
    import threading
    entered_close = threading.Event()
    release_close = threading.Event()
    posts = 0
    class SlowClose(Response):
        def close(self):
            entered_close.set()
            release_close.wait(3)
            super().close()
    def post(*args, **kwargs):
        nonlocal posts
        posts += 1
        return SlowClose([('data: ' + chunk({'content': 'OK'}, 'stop') + '\n\ndata: [DONE]\n\n').encode()])
    def stream():
        return minara.iter_events({'bearer_token': 'test', 'max_workers': 1},
            SimpleNamespace(backend=SimpleNamespace(_minara_post=post), _cancelled=False),
            {'model': 'x'}, 'hi', '', None, None, 5)
    first, second = stream(), stream()
    try:
        assert [e['type'] async for e in first] == ['usage_start', 'text', 'end']
        assert await asyncio.to_thread(entered_close.wait, 2)
        assert (await anext(second))['type'] == 'usage_start'
        waiting = asyncio.create_task(anext(second))
        await asyncio.sleep(.05)
        assert posts == 1 and not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        release_close.set()
        await first.aclose()
        await second.aclose()

@pytest.mark.asyncio
async def test_expired_generator_does_not_start_worker_or_bill_post():
    import threading
    from unittest.mock import patch
    calls = []
    session = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: calls.append(1)), _cancelled=False)
    stream = minara.iter_events({'bearer_token': 'test', 'turn_timeout': .001}, session,
                               {'model': 'x'}, 'hi', '', None, None, 5)
    await anext(stream)
    await asyncio.sleep(.01)
    with patch.object(threading.Thread, 'start') as start:
        with pytest.raises(apierr.ApiError) as caught:
            await anext(stream)
        assert caught.value.status == 504
        start.assert_not_called()
    assert calls == []
    slots = asyncio.get_running_loop()._minara_worker_slots
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_delayed_worker_checks_deadline_before_sending_post():
    import time
    import threading
    from unittest.mock import patch
    calls = []
    session = SimpleNamespace(backend=SimpleNamespace(_minara_post=lambda *a, **kw: calls.append(1)), _cancelled=False)
    start = threading.Thread.start
    def delayed_start(thread):
        if thread.name == 'minara-stream':
            time.sleep(.02)
        return start(thread)
    stream = minara.iter_events({'bearer_token': 'test', 'turn_timeout': .01}, session,
                               {'model': 'x'}, 'hi', '', None, None, 5)
    await anext(stream)
    with patch.object(threading.Thread, 'start', delayed_start):
        with pytest.raises(apierr.ApiError) as caught:
            await anext(stream)
    assert caught.value.status == 504
    await asyncio.sleep(.03)
    assert calls == []


def test_deep_sse_json_is_sanitized_upstream_error_not_internal_recursion():
    deep = '[' * 10000 + '0' + ']' * 10000
    with pytest.raises(apierr.ApiError) as caught:
        minara.EventParser().feed('{"choices": [], "extension":' + deep + '}')
    assert caught.value.status == 502


def test_deep_tool_arguments_mark_invalid_instead_of_internal_recursion():
    parser = minara.EventParser()
    raw = '{"x":' + '[' * 10000 + '0' + ']' * 10000 + '}'
    parser.feed(chunk({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'echo', 'arguments': raw}}]}, 'tool_calls'))
    final = parser.feed('[DONE]')
    assert final[0]['invalid'] is True
    assert final[-1] == {'type': 'end', 'reason': 'tool_calls'}

@pytest.mark.parametrize('depth', [1, minara.MAX_JSON_DEPTH])
def test_json_depth_budget_accepts_boundary(depth):
    raw = '[' * depth + '0' + ']' * depth
    parsed = minara._bounded_json(raw)
    assert isinstance(parsed, list)


def test_json_depth_budget_ignores_quoted_brackets_and_escaped_quotes():
    value = {'text': '[{' * 1000 + '\\"quoted\\"' + '}]' * 1000}
    assert minara._bounded_json(json.dumps(value)) == value
    with pytest.raises(ValueError, match='nesting'):
        minara._bounded_json('[' * (minara.MAX_JSON_DEPTH + 1) + '0' + ']' * (minara.MAX_JSON_DEPTH + 1))

@pytest.mark.parametrize('first', ['content_filter', 'length', 'tool_calls'])
def test_conflicting_finish_cannot_turn_filtered_or_cut_reply_into_success(first):
    parser = minara.EventParser()
    parser.feed(chunk({'content': 'partial'}, first))
    with pytest.raises(apierr.ApiError, match='conflicting finish') as caught:
        parser.feed(chunk(finish='stop'))
    assert caught.value.status == 502


@pytest.mark.parametrize('finish', ['stop', 'length', 'tool_calls', 'content_filter'])
def test_repeated_matching_finish_usage_frame_is_still_supported(finish):
    parser = minara.EventParser()
    parser.feed(chunk(finish=finish))
    parser.feed(chunk(finish=finish, usage={'prompt_tokens': 4}))
    assert parser.feed('[DONE]') == [{'type': 'end', 'reason': finish}]


@pytest.mark.parametrize('field', ['id', 'name', 'arguments', 'content', 'reasoning'])
def test_escaped_unpaired_surrogate_is_sanitized_upstream_error(field):
    if field in ('content', 'reasoning'):
        delta = {field: '\ud800'}
    else:
        part = {'index': 0, 'id': 'c', 'function': {'name': 'echo', 'arguments': '{}'}}
        if field == 'id':
            part['id'] = '\ud800'
        else:
            part['function'][field] = '\ud800'
        delta = {'tool_calls': [part]}
    with pytest.raises(apierr.ApiError) as caught:
        minara.EventParser().feed(chunk(delta, 'stop'))
    assert caught.value.status == 502
    assert '\ud800' not in caught.value.message


def test_valid_surrogate_pair_decodes_to_scalar_unicode():
    assert minara._bounded_json('"\\ud83d\\ude00"') == chr(0x1F600)

@pytest.mark.parametrize('image', [
    {'type': 'image', 'imageData': 'YWJj', 'mimeType': 'image/png'},
    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,YWJj'}},
])
def test_image_tool_result_is_visual_and_tool_pairing_precedes_user_images(image):
    history = [
        {'role': 'assistant', 'content': [
            {'type': 'tool_call', 'toolCallId': 'image1', 'toolName': 'view_image', 'toolInput': {}},
            {'type': 'tool_call', 'toolCallId': 'other2', 'toolName': 'echo', 'toolInput': {}}]},
        {'role': 'tool', 'content': {'toolCallId': 'image1', 'toolResult': [
            {'type': 'text', 'text': 'UNIQUE_IMAGE_DESCRIPTION'}, image]}},
        {'role': 'tool', 'content': {'toolCallId': 'other2', 'toolResult': 'OTHER_RESULT'}},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'CONTINUE'}]}]
    body = minara.build_body({}, {'model': 'x'}, '', '', None, None, 10,
                             session=SimpleNamespace(_native_client_messages=history))
    messages = body['messages']
    assert [m['role'] for m in messages] == ['assistant', 'tool', 'tool', 'user', 'user']
    assert [m['tool_call_id'] for m in messages[1:3]] == ['image1', 'other2']
    assert messages[1]['content'].count('UNIQUE_IMAGE_DESCRIPTION') == 1
    assert 'YWJj' not in messages[1]['content']
    visual = [b for b in messages[3]['content'] if b['type'] == 'image_url']
    assert visual == [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,YWJj'}}]
    assert json.dumps(messages).count('UNIQUE_IMAGE_DESCRIPTION') == 1


def test_ordinary_json_tool_results_not_treated_as_native_image_blocks():
    value = [{'type': 'image', 'custom_json_field': 'business payload'}, {'unrelated': 1}]
    result = list(minara._messages([{'role': 'tool', 'content': {'toolCallId': 'c', 'toolResult': value}}]))
    assert result == [{'role': 'tool', 'tool_call_id': 'c', 'content': json.dumps(value)}]


def test_images_from_multiple_tool_results_flush_after_all_tool_ids_are_answered():
    history = [{'role': 'tool', 'content': {'toolCallId': call, 'toolResult': [
        {'type': 'image', 'imageData': call, 'mimeType': 'image/png'}]}} for call in ('a', 'b')]
    result = list(minara._messages(history))
    assert [m['role'] for m in result] == ['tool', 'tool', 'user']
    assert [b['image_url']['url'] for b in result[-1]['content'] if b['type'] == 'image_url'] == [
        'data:image/png;base64,a', 'data:image/png;base64,b']

@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
def test_parsed_client_image_result_becomes_actual_visual_payload(dialect):
    import config
    import models
    import validate
    descriptor = {'upstream': 'minara', 'minara_models': [{'id': 'openai/image-test', 'tools': True, 'image': True}]}
    cfg = dict(config.DEFAULTS, model='minara/openai/image-test', _minara_models=models.build_minara_catalog([descriptor]))
    url = 'data:image/png;base64,YWJj'
    user = {'role': 'user', 'content': 'inspect image'}
    if dialect == 'responses':
        body = {'input': [user, {'type': 'function_call', 'call_id': 'c', 'name': 'view_image', 'arguments': '{}'},
                         {'type': 'function_call_output', 'call_id': 'c', 'output': [{'type': 'input_image', 'image_url': url}]}]}
    elif dialect == 'anthropic':
        body = {'max_tokens': 32, 'messages': [user,
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c', 'name': 'view_image', 'input': {}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'YWJj'}}]}]}]}
    else:
        body = {'messages': [user, {'role': 'assistant', 'tool_calls': [{'id': 'c', 'type': 'function', 'function': {'name': 'view_image', 'arguments': '{}'}}]},
                             {'role': 'tool', 'tool_call_id': 'c', 'content': [{'type': 'image_url', 'image_url': {'url': url}}]}]}
    parsed = getattr(validate, 'parse_' + dialect)(body, cfg)
    converted = minara.build_body(cfg, {'model': 'openai/image-test'}, '', '', None, None, 32,
                                 session=SimpleNamespace(_native_client_messages=parsed.native_messages))
    assert converted['messages'][-2]['role'] == 'tool'
    assert converted['messages'][-2]['tool_call_id'] == 'c'
    assert converted['messages'][-1]['role'] == 'user'
    assert any(block.get('image_url', {}).get('url') == url for block in converted['messages'][-1]['content'])
