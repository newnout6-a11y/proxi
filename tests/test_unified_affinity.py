"""Unified provider affinity persists without fabricating shared cache state."""
import asyncio
from types import SimpleNamespace


import conversation_state as cs
import native_history
from session import Backend, RelaySession, SessionPool
from test_shutdown import runtime_config


def pool(tmp_path, accounts):
    cfg = runtime_config(tmp_path)
    cfg.update(accounts=accounts, session_mode='per_conversation', account_move_keeps_history=True,
               max_workers=16, max_conversations=64, conversation_wait=0)
    return SessionPool(Backend(cfg))


def retool(name):
    return {'account_id': name, 'base': 'https://same.invalid', 'page_uuid': name,
            'cookie': 'synthetic', 'xsrf': 'synthetic', 'provider_resource_name': '__managed__'}


def minara(name, **extra):
    return {'account_id': name, 'upstream': 'minara', 'base': 'https://api.minara.ai/v1',
            'bearer_token': 'synthetic', 'minara_models': ['openai/test'], **extra}


def route():
    return {'id': 'minara/openai/test', 'upstream': 'minara', 'upstream_model': 'openai/test',
            'provider': 'openai'}


def test_provider_fair_new_selection_preserves_operator_priority(tmp_path):
    p = pool(tmp_path, [*[retool(str(i)) for i in range(24)], minara('m')])
    choices = [p._next_backend().cfg.get('upstream', 'retool') for _ in range(10)]
    assert choices.count('minara') == 5
    p.backends[-1].cfg['priority'] = 5
    assert all(p._next_backend().cfg.get('upstream', 'retool') == 'retool' for _ in range(10))


def test_minara_binding_only_roundtrip_does_not_claim_cache(tmp_path):
    session = RelaySession(Backend(minara('m')), 'local')
    session._bound_route = route()
    captured = cs.capture('child', session)
    assert not captured.empty and not captured.branches and captured.cache_key is None
    blob, header, count = cs.encode(captured, 1, 100000, 100000)
    decoded_header, state = cs.decode(blob, 100000)
    assert decoded_header['binding'] == captured.binding and count == 0
    assert state.cache_key is None and state.prefix_tokens == 0 and state.warm_at == 0
    assert b'bearer' not in blob


def test_binding_restores_exact_minara_account_even_when_priority_changes(tmp_path):
    async def run():
        p = pool(tmp_path, [minara('a', priority=5), minara('b')])
        store = cs.ConversationStore(tmp_path / 'state')
        p.attach_store(store)
        first = await p.acquire('child', model_entry=route())
        selected = first.session.backend.cfg['account_id']
        first.session._bound_route = route()
        await p.release(first)
        store.settle('child')
        assert store.peek('child') is not None
        p.close_store()
        q = pool(tmp_path, [minara('a'), minara('b', priority=99)])
        q.attach_store(cs.ConversationStore(tmp_path / 'state'))
        second = await q.acquire('child', model_entry=route())
        assert second.session.backend.cfg['account_id'] == selected == 'b'
        assert second.session._native_history.branches == []
        await q.release(second)
        q.close_store()
    asyncio.run(run())


def test_changed_route_mapping_refuses_old_binding():
    backend = Backend(minara('m'))
    binding = {'upstream': 'minara', 'base': backend.cfg['base'], 'model': 'x', 'upstream_model': 'old'}
    assert not SessionPool._binding_matches(binding, backend, route())
    assert not SessionPool._binding_matches(dict(binding, upstream='retool'), backend, None)


def test_cross_vendor_history_never_carried_even_with_optin(tmp_path):
    p = pool(tmp_path, [retool('r'), minara('m')])
    old = RelaySession(p.backends[0], 'old')
    old._native_history = native_history.History()
    old._native_history.branches = [SimpleNamespace()]
    old._last_cache_key = 'retool-secret-cache'
    old._cached_turn_ends = [5]
    new = RelaySession(p.backends[1], 'new')
    assert p._carry_history('same', old, new) == 0
    assert new._native_history.branches == []
    assert not getattr(new, '_last_cache_key', None) and new._cached_turn_ends == []


def test_legacy_state_binding_not_adopted_by_reused_minara_identity(tmp_path):
    p = pool(tmp_path, [minara('same')])
    meta = SimpleNamespace(account='same', binding=None)
    p._store = SimpleNamespace(peek=lambda key: meta)
    assert p._stored_binding(['same'], None, ())[2] == 'route_changed'

def test_child_key_is_independent_but_its_followup_sticks_and_parent_is_pinned(tmp_path):
    async def run():
        p = pool(tmp_path, [retool('r'), minara('m')])
        parent = await p.acquire('main')
        assert parent.session.backend.cfg.get('upstream', 'retool') == 'retool'
        await p.release(parent)
        child = await p.acquire('child', parent_key='main')
        assert child.session.backend.cfg['upstream'] == 'minara'
        assert p._conversations['main']._cache_dependents == 1
        child_backend = child.session.backend
        await p.release(child)
        again = await p.acquire('child', parent_key='main')
        assert again.session.backend is child_backend
        assert p._conversations['main'].backend.cfg.get('upstream', 'retool') == 'retool'
        await p.release(again)
    asyncio.run(run())


def test_cross_vendor_concurrent_branch_does_not_inherit_cache_snapshots(tmp_path):
    async def run():
        p = pool(tmp_path, [retool('r'), minara('m')])
        active = await p.acquire('same')
        original = active.session
        original._cached_head = 'RETOOL_HEAD'
        original._cached_turn_ends = [5]
        original._direct = {'tools': ['RETOOL_CONVERTED_TOOL']}
        original._registered_tools = {'x': {'name': 'x'}}
        fork = await p.acquire('same', exclude=(original.backend,))
        assert fork.session.backend.cfg['upstream'] == 'minara'
        assert fork.session._cached_head is None
        assert fork.session._cached_turn_ends == []
        assert fork.session._direct is None
        assert fork.session._registered_tools == {}
        assert fork.session._native_history is not original._native_history
        await p.release(fork)
        await p.release(active)
    asyncio.run(run())

def test_route_mapping_change_clears_live_cached_state(tmp_path):
    async def run():
        p = pool(tmp_path, [minara('a')])
        first = await p.acquire('same', model_entry=route())
        old = first.session
        old._bound_route = route()
        cs.provider_binding(old)
        old._last_cache_key = 'old-cache'
        old._cached_turn_ends = [10]
        await p.release(first)
        changed = dict(route(), upstream_model='openai/new')
        p.backends[0].cfg['minara_models'] = ['openai/new']
        second = await p.acquire('same', model_entry=changed)
        assert second.session is not old
        assert second.session._cached_turn_ends == []
        assert not getattr(second.session, '_last_cache_key', None)
        await p.release(second)
    asyncio.run(run())

def test_peek_compares_other_model_scope_but_hides_changed_mapping(tmp_path):
    async def run():
        p = pool(tmp_path, [retool('r')])
        one = {'id': 'claude-opus-5-5', 'provider': 'anthropic'}
        first = await p.acquire('same', model_entry=one)
        old = first.session
        old._bound_route = one
        cs.provider_binding(old)
        await p.release(first)
        other = {'id': 'claude-sonnet-5-5', 'provider': 'anthropic'}
        assert p.peek('same', model_entry=other) is old
        changed = dict(one, upstream_model='different-route')
        assert p.peek('same', model_entry=changed) is None
    asyncio.run(run())
