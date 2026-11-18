"""Which account a new conversation is offered first.

Design D4 1.6 and audit A03-03: "least loaded" sent each new conversation of a model to the accounts whose plan refuses it
(they never hold a conversation of it), so every hold that ended or every restart cost the next conversations a refusal
each. Accounts that served the model come first now, and a 250-credit plan that never served a frontier model comes last."""
from accounts import AccountDispatcher
from session import Backend as PoolBackend, SessionPool
from test_account_dispatcher import Clock
from test_route_errors import M, acct, entry, pool_config

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"


class Credits:
    def __init__(self, allocation):
        self.allocation = allocation

    def balance_snapshot(self):
        return {"available": 100.0, "monthly_allocation": self.allocation}


def pool_of(*names):
    pool = SessionPool(PoolBackend(pool_config([acct(name, provider_resource_name=M) for name in names])))
    dispatcher = AccountDispatcher(pool.backends, {"session_refresh_interval": 0}, {}, clock=Clock())
    return pool, dispatcher


def busy(pool, backend, conversations):
    for index in range(conversations):
        pool._conversations["busy-%s-%d" % (backend.cfg["account_id"], index)] = type("S", (), {"backend": backend})()


def picks(pool, name, times=6):
    return {pool._next_backend(entry(name)).cfg["account_id"] for _ in range(times)}


def test_a_new_conversation_goes_first_to_the_accounts_whose_plan_served_its_model():
    pool, dispatcher = pool_of("a", "b", "c")
    a, b, c = pool.backends
    dispatcher.mark_plan_served(c, OPUS)
    assert picks(pool, OPUS) == {"c"}
    assert picks(pool, SONNET) == {"a", "b", "c"}  # another model: no preference
    # A preference, not an exclusion: once c carries more than twice the load of the others plus one, they get one.
    busy(pool, c, 2)
    assert picks(pool, OPUS) == {"a", "b"}


def test_a_small_plan_that_never_served_a_frontier_model_is_offered_it_last():
    pool, _ = pool_of("a", "b")
    a, b = pool.backends
    a.credits, b.credits = Credits(250), Credits(3000)
    assert picks(pool, OPUS) == {"b"}
    assert picks(pool, HAIKU) == {"a", "b"}  # not a frontier model: every plan has it
    busy(pool, b, 2)
    assert picks(pool, OPUS) == {"a"}
