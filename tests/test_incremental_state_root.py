"""Property test: incremental state_root() must match a from-scratch recompute.

The cache invalidation logic is easy to get wrong (forget a mutation site,
miss a copy(), etc.), so this exercises state mutations over many random
sequences and compares each to a clean-rebuild oracle.
"""
import random

from jiji.core.state import WorldState


def _fresh_root(ws: WorldState) -> bytes:
    """Recompute the state root with an empty leaf cache."""
    ws._leaf_cache.clear()
    ws._dirty.clear()
    return ws.state_root()


def _random_pubkey(rng: random.Random, pool: list[bytes]) -> bytes:
    # With some probability reuse an existing key so we actually touch dirty cached leaves.
    if pool and rng.random() < 0.5:
        return rng.choice(pool)
    pk = rng.randbytes(32)
    pool.append(pk)
    return pk


def test_incremental_matches_full_recompute():
    rng = random.Random(42)
    ws = WorldState()
    pool: list[bytes] = []
    for _ in range(500):
        op = rng.randint(0, 2)
        if op == 0:
            # create/get account
            ws.get_or_create(_random_pubkey(rng, pool))
        elif op == 1 and pool:
            # mutate balance directly via a synthetic coinbase-like bump
            pk = rng.choice(pool)
            acct = ws.get_or_create(pk)
            acct.balance += rng.randint(1, 100)
            ws._invalidate(pk)
        elif op == 2 and pool:
            pk = rng.choice(pool)
            acct = ws.get_or_create(pk)
            acct.nonce += 1
            ws._invalidate(pk)

        # After every step, the cached root must equal a fresh recompute.
        cached_root = ws.state_root()
        snapshot = ws.copy()
        fresh_root = _fresh_root(snapshot)
        assert cached_root == fresh_root


def test_copy_preserves_cache_correctness():
    rng = random.Random(7)
    ws = WorldState()
    for i in range(20):
        acct = ws.get_or_create(rng.randbytes(32))
        acct.balance = i
    root_a = ws.state_root()  # populates cache
    copy = ws.copy()
    assert copy.state_root() == root_a
    # Mutate the copy; original must not drift.
    pk = list(copy.accounts.keys())[0]
    copy.accounts[pk].balance = 99999
    copy._invalidate(pk)
    assert copy.state_root() != root_a
    assert ws.state_root() == root_a


def test_empty_state_root_stable():
    ws = WorldState()
    r1 = ws.state_root()
    r2 = ws.state_root()
    assert r1 == r2
