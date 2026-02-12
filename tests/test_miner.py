import pytest
from jiji.core.crypto import generate_keypair
from jiji.core.chain import Blockchain
from jiji.core.config import GENESIS_DIFFICULTY, PROTOCOL_VERSION, block_reward
from jiji.core.transaction import Post, Endorse, Transfer, Coinbase
from jiji.core.validation import ValidationError, validate_block
from jiji.mining.mempool import Mempool
from jiji.mining.miner import Miner
from tests.test_chain import build_block


def make_keys():
    return generate_keypair()


def setup_all():
    """Create chain, mempool, miner, and return with key pair."""
    priv, pub = make_keys()
    chain = Blockchain()
    chain.initialize_genesis(pub, timestamp=1000000)
    pool = Mempool(chain)
    miner = Miner(chain, pool, pub)
    return chain, pool, miner, priv, pub


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


class TestCreateBlockTemplate:
    def test_produces_valid_structure(self):
        chain, pool, miner, priv, pub = setup_all()
        block = miner.create_block_template()
        assert block.header.height == 1
        assert block.header.version == PROTOCOL_VERSION
        assert block.header.prev_hash == chain.tip.block_hash()
        assert block.header.miner == pub
        assert block.header.nonce == 0
        assert len(block.transactions) >= 1

    def test_includes_correct_coinbase(self):
        chain, pool, miner, priv, pub = setup_all()
        block = miner.create_block_template()
        cb = block.transactions[0]
        assert isinstance(cb, Coinbase)
        assert cb.recipient == pub
        assert cb.amount == block_reward(1)
        assert cb.height == 1

    def test_includes_mempool_transactions(self):
        chain, pool, miner, priv, pub = setup_all()
        post = Post(author=pub, nonce=0, timestamp=1000010, body="included", reply_to=None, gas_fee=2)
        post.sign_tx(priv)
        pool.add(post)
        block = miner.create_block_template()
        assert len(block.transactions) == 2
        assert block.transactions[1].tx_hash() == post.tx_hash()

    def test_orders_by_gas_fee(self):
        chain, pool, miner, priv, pub = setup_all()
        priv_a, pub_a = make_keys()
        priv_b, pub_b = make_keys()
        fund_accounts(chain, priv, pub, [(pub_a, 10), (pub_b, 10)])
        # recreate miner against updated chain
        miner = Miner(chain, pool, pub)
        p_lo = Post(author=pub_a, nonce=0, timestamp=1000030, body="lo", reply_to=None, gas_fee=1)
        p_lo.sign_tx(priv_a)
        p_hi = Post(author=pub_b, nonce=0, timestamp=1000030, body="hi", reply_to=None, gas_fee=5)
        p_hi.sign_tx(priv_b)
        pool.add(p_lo)
        pool.add(p_hi)
        block = miner.create_block_template()
        # higher fee should come first after coinbase
        non_cb = block.transactions[1:]
        assert len(non_cb) == 2
        assert non_cb[0].gas_fee >= non_cb[1].gas_fee

    def test_skips_invalid_transactions(self):
        chain, pool, miner, priv, pub = setup_all()
        # add a valid tx
        p1 = Post(author=pub, nonce=0, timestamp=1000010, body="ok", reply_to=None, gas_fee=1)
        p1.sign_tx(priv)
        pool.add(p1)
        # manually inject an invalid tx (author has no account)
        priv2, pub2 = make_keys()
        bad = Post(author=pub2, nonce=0, timestamp=1000010, body="bad", reply_to=None, gas_fee=1)
        bad.sign_tx(priv2)
        pool._txs[bad.tx_hash()] = bad
        block = miner.create_block_template()
        # only coinbase + p1 should be included (bad tx skipped)
        assert len(block.transactions) == 2
        tx_hashes = [tx.tx_hash() for tx in block.transactions]
        assert p1.tx_hash() in tx_hashes

    def test_computes_correct_roots(self):
        chain, pool, miner, priv, pub = setup_all()
        post = Post(author=pub, nonce=0, timestamp=1000010, body="roots", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        pool.add(post)
        block = miner.create_block_template()
        assert block.header.tx_merkle_root == block.compute_tx_merkle_root()
        assert block.header.tx_count == len(block.transactions)


class TestMineBlock:
    def test_solves_pow(self):
        chain, pool, miner, priv, pub = setup_all()
        template = miner.create_block_template()
        block = miner.mine_block(template)
        assert block is not None
        assert block.meets_difficulty()

    def test_max_iterations_finds_at_low_difficulty(self):
        chain, pool, miner, priv, pub = setup_all()
        template = miner.create_block_template()
        # at GENESIS_DIFFICULTY=1, any hash meets it
        result = miner.mine_block(template, max_iterations=1000)
        assert result is not None
        assert result.meets_difficulty()

    def test_nonce_increments(self):
        chain, pool, miner, priv, pub = setup_all()
        template = miner.create_block_template()
        original_nonce = template.header.nonce
        block = miner.mine_block(template)
        assert block.header.nonce >= original_nonce


class TestMineNext:
    def test_advances_chain(self):
        chain, pool, miner, priv, pub = setup_all()
        block = miner.mine_next()
        assert block is not None
        assert chain.height == 1

    def test_drains_mempool(self):
        chain, pool, miner, priv, pub = setup_all()
        post = Post(author=pub, nonce=0, timestamp=1000010, body="drain", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        pool.add(post)
        assert pool.size == 1
        miner.mine_next()
        assert pool.size == 0

    def test_mined_block_passes_validation(self):
        chain, pool, miner, priv, pub = setup_all()
        post = Post(author=pub, nonce=0, timestamp=1000010, body="valid", reply_to=None, gas_fee=2)
        post.sign_tx(priv)
        pool.add(post)
        # mine_next calls chain.add_block which runs validate_block internally
        block = miner.mine_next()
        assert block is not None
        assert chain.height == 1

    def test_state_updated_correctly(self):
        chain, pool, miner, priv, pub = setup_all()
        _, pub2 = make_keys()
        tx = Transfer(sender=pub, recipient=pub2, amount=10, nonce=0, gas_fee=1)
        tx.sign_tx(priv)
        pool.add(tx)
        balance_before = chain.state.get_account(pub).balance
        miner.mine_next()
        # pub: balance_before - 10 - 1(gas) + 50(reward) + 1(gas as miner)
        expected = balance_before - 10 - 1 + block_reward(1) + 1
        assert chain.state.get_account(pub).balance == expected
        assert chain.state.get_account(pub2).balance == 10


class TestMultipleBlocks:
    def test_sequential_mining(self):
        chain, pool, miner, priv, pub = setup_all()
        priv2, pub2 = make_keys()
        # block 1: transfer to pub2
        tx1 = Transfer(sender=pub, recipient=pub2, amount=20, nonce=0, gas_fee=1)
        tx1.sign_tx(priv)
        pool.add(tx1)
        miner.mine_next()
        assert chain.height == 1
        assert chain.state.get_account(pub2).balance == 20

        # block 2: pub2 posts
        post = Post(author=pub2, nonce=0, timestamp=1000030, body="hi", reply_to=None, gas_fee=1)
        post.sign_tx(priv2)
        pool.add(post)
        miner.mine_next()
        assert chain.height == 2
        assert post.tx_hash() in chain.known_posts

        # block 3: pub endorses pub2's post with tip
        endorse = Endorse(author=pub, nonce=1, target=post.tx_hash(), amount=5, message="nice", gas_fee=1)
        endorse.sign_tx(priv)
        pool.add(endorse)
        miner.mine_next()
        assert chain.height == 3
        # pub2: 20 - 1(gas for post) + 5(tip) = 24
        assert chain.state.get_account(pub2).balance == 24

    def test_empty_blocks(self):
        chain, pool, miner, priv, pub = setup_all()
        # mine 3 empty blocks (coinbase only)
        for _ in range(3):
            miner.mine_next()
        assert chain.height == 3
        # miner should have: genesis reward + 3 block rewards
        expected = block_reward(0) + block_reward(1) + block_reward(2) + block_reward(3)
        assert chain.state.get_account(pub).balance == expected
