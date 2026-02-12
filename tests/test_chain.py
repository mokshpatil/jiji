import pytest
from jiji.core.crypto import generate_keypair
from jiji.core.block import Block, BlockHeader
from jiji.core.chain import Blockchain
from jiji.core.config import GENESIS_DIFFICULTY, PROTOCOL_VERSION, block_reward
from jiji.core.merkle import merkle_root
from jiji.core.transaction import Post, Endorse, Transfer, Coinbase
from jiji.core.validation import ValidationError


def make_keys():
    return generate_keypair()


def build_block(chain, txs, miner_pub, timestamp):
    height = chain.height + 1
    cb = Coinbase(recipient=miner_pub, amount=block_reward(height), height=height)
    all_txs = [cb] + txs
    tx_root = merkle_root([tx.tx_hash() for tx in all_txs])
    working = chain.state.copy()
    for tx in all_txs:
        target_author = None
        if isinstance(tx, Endorse) and tx.amount > 0:
            target_author = chain.post_authors.get(tx.target)
            for prev_tx in all_txs:
                if isinstance(prev_tx, Post) and prev_tx.tx_hash() == tx.target:
                    target_author = prev_tx.author
        working.apply_transaction(tx, miner_pub, target_author)
    header = BlockHeader(
        PROTOCOL_VERSION, height, chain.tip.block_hash(), timestamp,
        miner_pub, GENESIS_DIFFICULTY, 0, tx_root, working.state_root(),
        len(all_txs),
    )
    block = Block(header=header, transactions=all_txs)
    while not block.meets_difficulty():
        block.header.nonce += 1
    return block


class TestGenesis:
    def test_height_zero(self):
        _, pub = make_keys()
        chain = Blockchain()
        chain.initialize_genesis(pub, timestamp=1000000)
        assert chain.height == 0

    def test_miner_gets_reward(self):
        _, pub = make_keys()
        chain = Blockchain()
        chain.initialize_genesis(pub, timestamp=1000000)
        assert chain.state.get_account(pub).balance == block_reward(0)

    def test_tip_is_genesis(self):
        _, pub = make_keys()
        chain = Blockchain()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)
        assert chain.tip.block_hash() == genesis.block_hash()

    def test_double_init_raises(self):
        _, pub = make_keys()
        chain = Blockchain()
        chain.initialize_genesis(pub, timestamp=1000000)
        with pytest.raises(RuntimeError):
            chain.initialize_genesis(pub, timestamp=1000001)


class TestAddBlock:
    def test_advances_height(self):
        priv, pub = make_keys()
        chain = Blockchain()
        chain.initialize_genesis(pub, timestamp=1000000)
        block = build_block(chain, [], pub, 1000015)
        chain.add_block(block, current_time=1000020)
        assert chain.height == 1

    def test_updates_state(self):
        priv, pub = make_keys()
        _, pub2 = make_keys()
        chain = Blockchain()
        chain.initialize_genesis(pub, timestamp=1000000)
        tx = Transfer(sender=pub, recipient=pub2, amount=10, nonce=0, gas_fee=1)
        tx.sign_tx(priv)
        block = build_block(chain, [tx], pub, 1000015)
        chain.add_block(block, current_time=1000020)
        # pub: 50(genesis) - 10 - 1(gas) + 50(reward) + 1(gas as miner) = 90
        assert chain.state.get_account(pub).balance == 90
        assert chain.state.get_account(pub2).balance == 10


class TestTransactionLookup:
    def test_find_by_hash(self):
        priv, pub = make_keys()
        chain = Blockchain()
        chain.initialize_genesis(pub, timestamp=1000000)
        post = Post(author=pub, nonce=0, timestamp=1000015, body="findme", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        block = build_block(chain, [post], pub, 1000015)
        chain.add_block(block, current_time=1000020)
        found = chain.get_transaction(post.tx_hash())
        assert found is not None
        assert found.body == "findme"

    def test_missing_returns_none(self):
        _, pub = make_keys()
        chain = Blockchain()
        chain.initialize_genesis(pub, timestamp=1000000)
        assert chain.get_transaction(b"\xff" * 32) is None


class TestBlockLookup:
    def test_by_height(self):
        _, pub = make_keys()
        chain = Blockchain()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)
        assert chain.get_block_by_height(0).block_hash() == genesis.block_hash()
        assert chain.get_block_by_height(1) is None

    def test_by_hash(self):
        _, pub = make_keys()
        chain = Blockchain()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)
        found = chain.get_block_by_hash(genesis.block_hash())
        assert found is not None
        assert found.header.height == 0


class TestMultiBlockScenario:
    def test_posts_endorsements_transfers(self):
        priv_m, pub_m = make_keys()
        priv_a, pub_a = make_keys()
        priv_b, pub_b = make_keys()
        chain = Blockchain()
        chain.initialize_genesis(pub_m, timestamp=1000000)

        # block 1: transfer tokens to users
        t1 = Transfer(sender=pub_m, recipient=pub_a, amount=15, nonce=0, gas_fee=1)
        t1.sign_tx(priv_m)
        t2 = Transfer(sender=pub_m, recipient=pub_b, amount=10, nonce=1, gas_fee=1)
        t2.sign_tx(priv_m)
        b1 = build_block(chain, [t1, t2], pub_m, 1000015)
        chain.add_block(b1, current_time=1000020)

        assert chain.state.get_account(pub_a).balance == 15
        assert chain.state.get_account(pub_b).balance == 10

        # block 2: user A posts, user B endorses with tip
        post = Post(author=pub_a, nonce=0, timestamp=1000030, body="hello", reply_to=None, gas_fee=1)
        post.sign_tx(priv_a)
        endorse = Endorse(author=pub_b, nonce=0, target=post.tx_hash(), amount=3, message="great", gas_fee=1)
        endorse.sign_tx(priv_b)
        b2 = build_block(chain, [post, endorse], pub_m, 1000030)
        chain.add_block(b2, current_time=1000035)

        assert post.tx_hash() in chain.known_posts
        assert chain.state.get_account(pub_a).balance == 17  # 15 - 1 + 3
        assert chain.state.get_account(pub_b).balance == 6   # 10 - 1 - 3

        # block 3: reply
        reply = Post(author=pub_a, nonce=1, timestamp=1000045, body="replying", reply_to=post.tx_hash(), gas_fee=1)
        reply.sign_tx(priv_a)
        b3 = build_block(chain, [reply], pub_m, 1000045)
        chain.add_block(b3, current_time=1000050)

        assert chain.height == 3
        assert len(chain.known_posts) == 2
        found = chain.get_transaction(reply.tx_hash())
        assert found.reply_to == post.tx_hash()


class TestRejectsInvalidBlock:
    def test_block_not_extending_tip(self):
        priv, pub = make_keys()
        chain = Blockchain()
        chain.initialize_genesis(pub, timestamp=1000000)
        b1 = build_block(chain, [], pub, 1000015)
        chain.add_block(b1, current_time=1000020)
        # try adding same height again
        with pytest.raises(ValidationError):
            chain.add_block(b1, current_time=1000025)
