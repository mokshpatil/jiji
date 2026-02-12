import pytest
from jiji.core.crypto import generate_keypair
from jiji.core.chain import Blockchain
from jiji.core.config import block_reward
from jiji.core.transaction import Post, Endorse, Transfer, Coinbase
from jiji.core.validation import ValidationError
from jiji.mining.mempool import Mempool
from tests.test_chain import build_block


def make_keys():
    return generate_keypair()


def setup_chain():
    """Create a chain with genesis and return chain, miner key pair."""
    priv, pub = make_keys()
    chain = Blockchain()
    chain.initialize_genesis(pub, timestamp=1000000)
    return chain, priv, pub


def fund_accounts(chain, miner_priv, miner_pub, recipients, ts=1000015):
    """Transfer tokens from miner to recipients. recipients = [(pub, amount), ...]"""
    txs = []
    nonce = chain.state.get_account(miner_pub).nonce
    for pub, amount in recipients:
        tx = Transfer(sender=miner_pub, recipient=pub, amount=amount, nonce=nonce, gas_fee=1)
        tx.sign_tx(miner_priv)
        txs.append(tx)
        nonce += 1
    block = build_block(chain, txs, miner_pub, ts)
    chain.add_block(block, current_time=ts + 5)


class TestMempoolAdd:
    def test_add_valid_post(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=0, timestamp=1000010, body="hello", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        tx_hash = pool.add(post)
        assert tx_hash == post.tx_hash()
        assert pool.size == 1
        assert tx_hash in pool

    def test_add_valid_transfer(self):
        chain, priv, pub = setup_chain()
        _, pub2 = make_keys()
        pool = Mempool(chain)
        tx = Transfer(sender=pub, recipient=pub2, amount=5, nonce=0, gas_fee=1)
        tx.sign_tx(priv)
        pool.add(tx)
        assert pool.size == 1

    def test_reject_coinbase(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        cb = Coinbase(recipient=pub, amount=50, height=1)
        with pytest.raises(ValidationError, match="coinbase"):
            pool.add(cb)

    def test_reject_duplicate(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=0, timestamp=1000010, body="dup", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        pool.add(post)
        with pytest.raises(ValidationError, match="already in mempool"):
            pool.add(post)

    def test_reject_already_confirmed(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=0, timestamp=1000015, body="confirmed", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        block = build_block(chain, [post], pub, 1000015)
        chain.add_block(block, current_time=1000020)
        with pytest.raises(ValidationError, match="already confirmed"):
            pool.add(post)

    def test_reject_invalid_format(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=0, timestamp=1000010, body="hi", reply_to=None, gas_fee=0)
        post.sign_tx(priv)
        with pytest.raises(ValidationError, match="gas fee"):
            pool.add(post)

    def test_reject_invalid_signature(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=0, timestamp=1000010, body="hi", reply_to=None, gas_fee=1)
        post.signature = b"\x00" * 64
        with pytest.raises(ValidationError, match="signature"):
            pool.add(post)

    def test_reject_wrong_nonce(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=99, timestamp=1000010, body="bad", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        with pytest.raises(ValidationError, match="nonce"):
            pool.add(post)

    def test_reject_insufficient_balance(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        _, pub2 = make_keys()
        tx = Transfer(sender=pub, recipient=pub2, amount=9999, nonce=0, gas_fee=1)
        tx.sign_tx(priv)
        with pytest.raises(ValidationError, match="insufficient"):
            pool.add(tx)


class TestMempoolEviction:
    def test_evicts_lowest_fee_when_full(self):
        chain, priv, pub = setup_chain()
        priv_a, pub_a = make_keys()
        priv_b, pub_b = make_keys()
        priv_c, pub_c = make_keys()
        fund_accounts(chain, priv, pub, [(pub_a, 10), (pub_b, 10), (pub_c, 10)])
        pool = Mempool(chain, max_size=2)
        # add two txs with low fees from different accounts
        p1 = Post(author=pub_a, nonce=0, timestamp=1000030, body="lo", reply_to=None, gas_fee=1)
        p1.sign_tx(priv_a)
        pool.add(p1)
        p2 = Post(author=pub_b, nonce=0, timestamp=1000030, body="md", reply_to=None, gas_fee=2)
        p2.sign_tx(priv_b)
        pool.add(p2)
        assert pool.size == 2
        # higher fee tx should evict lowest (p1)
        p3 = Post(author=pub_c, nonce=0, timestamp=1000030, body="hi", reply_to=None, gas_fee=3)
        p3.sign_tx(priv_c)
        pool.add(p3)
        assert pool.size == 2
        assert p1.tx_hash() not in pool
        assert p3.tx_hash() in pool

    def test_reject_when_full_and_fee_too_low(self):
        chain, priv, pub = setup_chain()
        priv_b, pub_b = make_keys()
        fund_accounts(chain, priv, pub, [(pub_b, 10)])
        pool = Mempool(chain, max_size=1)
        p1 = Post(author=pub, nonce=1, timestamp=1000030, body="high", reply_to=None, gas_fee=5)
        p1.sign_tx(priv)
        pool.add(p1)
        p2 = Post(author=pub_b, nonce=0, timestamp=1000030, body="low", reply_to=None, gas_fee=1)
        p2.sign_tx(priv_b)
        with pytest.raises(ValidationError, match="fee too low"):
            pool.add(p2)


class TestMempoolRemove:
    def test_remove_single(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=0, timestamp=1000010, body="rm", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        pool.add(post)
        pool.remove(post.tx_hash())
        assert pool.size == 0

    def test_remove_confirmed(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=0, timestamp=1000015, body="block", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        pool.add(post)
        assert pool.size == 1
        block = build_block(chain, [post], pub, 1000015)
        chain.add_block(block, current_time=1000020)
        pool.remove_confirmed(block)
        assert pool.size == 0


class TestMempoolRevalidate:
    def test_purges_when_nonce_advanced(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        # add a tx at nonce 0
        p1 = Post(author=pub, nonce=0, timestamp=1000010, body="a", reply_to=None, gas_fee=1)
        p1.sign_tx(priv)
        pool.add(p1)
        # confirm a different tx at nonce 0 on chain (bypassing mempool)
        alt = Post(author=pub, nonce=0, timestamp=1000015, body="alt", reply_to=None, gas_fee=1)
        alt.sign_tx(priv)
        block = build_block(chain, [alt], pub, 1000015)
        chain.add_block(block, current_time=1000020)
        # p1 now has stale nonce (chain expects 1)
        removed = pool.revalidate()
        assert p1.tx_hash() in removed
        assert pool.size == 0

    def test_keeps_valid_transactions(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=0, timestamp=1000010, body="ok", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        pool.add(post)
        removed = pool.revalidate()
        assert len(removed) == 0
        assert pool.size == 1


class TestMempoolGetPending:
    def test_sorted_by_gas_fee_descending(self):
        chain, priv, pub = setup_chain()
        priv_a, pub_a = make_keys()
        priv_b, pub_b = make_keys()
        fund_accounts(chain, priv, pub, [(pub_a, 10), (pub_b, 10)])
        pool = Mempool(chain)
        miner_nonce = chain.state.get_account(pub).nonce
        p1 = Post(author=pub, nonce=miner_nonce, timestamp=1000030, body="lo", reply_to=None, gas_fee=1)
        p1.sign_tx(priv)
        p2 = Post(author=pub_a, nonce=0, timestamp=1000030, body="hi", reply_to=None, gas_fee=5)
        p2.sign_tx(priv_a)
        p3 = Post(author=pub_b, nonce=0, timestamp=1000030, body="md", reply_to=None, gas_fee=3)
        p3.sign_tx(priv_b)
        pool.add(p1)
        pool.add(p2)
        pool.add(p3)
        pending = pool.get_pending()
        fees = [tx.gas_fee for tx in pending]
        assert fees == [5, 3, 1]

    def test_limit(self):
        chain, priv, pub = setup_chain()
        # fund 4 more accounts
        keys = [make_keys() for _ in range(4)]
        fund_accounts(chain, priv, pub, [(k[1], 10) for k in keys])
        pool = Mempool(chain)
        # add 1 tx from miner + 4 from funded accounts = 5 total
        miner_nonce = chain.state.get_account(pub).nonce
        p0 = Post(author=pub, nonce=miner_nonce, timestamp=1000030, body="tx0", reply_to=None, gas_fee=1)
        p0.sign_tx(priv)
        pool.add(p0)
        for i, (pk, pb) in enumerate(keys):
            p = Post(author=pb, nonce=0, timestamp=1000030, body=f"tx{i+1}", reply_to=None, gas_fee=i + 2)
            p.sign_tx(pk)
            pool.add(p)
        assert pool.size == 5
        assert len(pool.get_pending(limit=2)) == 2

    def test_get_by_hash(self):
        chain, priv, pub = setup_chain()
        pool = Mempool(chain)
        post = Post(author=pub, nonce=0, timestamp=1000010, body="find", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        pool.add(post)
        found = pool.get_by_hash(post.tx_hash())
        assert found is not None
        assert found.body == "find"
        assert pool.get_by_hash(b"\xff" * 32) is None
