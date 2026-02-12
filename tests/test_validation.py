import pytest
from jiji.core.crypto import generate_keypair
from jiji.core.block import Block, BlockHeader
from jiji.core.chain import Blockchain
from jiji.core.config import (
    GENESIS_DIFFICULTY, MINIMUM_GAS_FEE, POST_BODY_LIMIT,
    ENDORSE_MESSAGE_LIMIT, PROTOCOL_VERSION, block_reward,
)
from jiji.core.merkle import merkle_root
from jiji.core.transaction import Post, Endorse, Transfer, Coinbase
from jiji.core.validation import (
    ValidationError,
    validate_post_format,
    validate_endorse_format,
    validate_transfer_format,
    validate_coinbase_format,
    validate_transaction_state,
    validate_block,
)


def make_keys():
    return generate_keypair()


def make_chain():
    priv, pub = make_keys()
    chain = Blockchain()
    chain.initialize_genesis(pub, timestamp=1000000)
    return chain, priv, pub


def build_block(chain, txs, miner_pub, timestamp):
    """Helper to build a valid block from transactions."""
    height = chain.height + 1
    cb = Coinbase(recipient=miner_pub, amount=block_reward(height), height=height)
    all_txs = [cb] + txs
    tx_root = merkle_root([tx.tx_hash() for tx in all_txs])
    working = chain.state.copy()
    for tx in all_txs:
        target_author = None
        if isinstance(tx, Endorse) and tx.amount > 0:
            target_author = chain.post_authors.get(tx.target)
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


# -- Format validation --

class TestPostFormat:
    def test_valid(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="hello", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        validate_post_format(post)  # should not raise

    def test_empty_body(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        with pytest.raises(ValidationError, match="non-empty"):
            validate_post_format(post)

    def test_oversized_body(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="x" * (POST_BODY_LIMIT + 1), reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        with pytest.raises(ValidationError, match="exceeds"):
            validate_post_format(post)

    def test_low_gas(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="hi", reply_to=None, gas_fee=0)
        post.sign_tx(priv)
        with pytest.raises(ValidationError, match="gas fee"):
            validate_post_format(post)

    def test_bad_author_key_length(self):
        priv, pub = make_keys()
        post = Post(author=b"\x00" * 16, nonce=0, timestamp=1000, body="hi", reply_to=None, gas_fee=1)
        with pytest.raises(ValidationError, match="32 bytes"):
            validate_post_format(post)

    def test_invalid_signature(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="hi", reply_to=None, gas_fee=1)
        post.signature = b"\x00" * 64
        with pytest.raises(ValidationError, match="signature"):
            validate_post_format(post)


class TestEndorseFormat:
    def test_oversized_message(self):
        priv, pub = make_keys()
        e = Endorse(author=pub, nonce=0, target=b"\x00" * 32, amount=0,
                     message="x" * (ENDORSE_MESSAGE_LIMIT + 1), gas_fee=1)
        e.sign_tx(priv)
        with pytest.raises(ValidationError, match="message exceeds"):
            validate_endorse_format(e)

    def test_negative_amount(self):
        priv, pub = make_keys()
        e = Endorse(author=pub, nonce=0, target=b"\x00" * 32, amount=-1, message="", gas_fee=1)
        e.sign_tx(priv)
        with pytest.raises(ValidationError, match="non-negative"):
            validate_endorse_format(e)


class TestTransferFormat:
    def test_same_sender_recipient(self):
        priv, pub = make_keys()
        tx = Transfer(sender=pub, recipient=pub, amount=10, nonce=0, gas_fee=1)
        tx.sign_tx(priv)
        with pytest.raises(ValidationError, match="differ"):
            validate_transfer_format(tx)

    def test_zero_amount(self):
        priv, pub = make_keys()
        _, r = make_keys()
        tx = Transfer(sender=pub, recipient=r, amount=0, nonce=0, gas_fee=1)
        tx.sign_tx(priv)
        with pytest.raises(ValidationError, match="positive"):
            validate_transfer_format(tx)


class TestCoinbaseFormat:
    def test_wrong_amount(self):
        _, pub = make_keys()
        cb = Coinbase(recipient=pub, amount=999, height=0)
        with pytest.raises(ValidationError, match="amount"):
            validate_coinbase_format(cb, 0)

    def test_wrong_height(self):
        _, pub = make_keys()
        cb = Coinbase(recipient=pub, amount=50, height=5)
        with pytest.raises(ValidationError, match="height"):
            validate_coinbase_format(cb, 0)


# -- State validation --

class TestStateValidation:
    def test_wrong_nonce(self):
        chain, priv, pub = make_chain()
        post = Post(author=pub, nonce=99, timestamp=1000015, body="bad", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        with pytest.raises(ValidationError, match="nonce"):
            validate_transaction_state(post, chain.state, chain.known_posts)

    def test_insufficient_balance(self):
        chain, priv, pub = make_chain()
        post = Post(author=pub, nonce=0, timestamp=1000015, body="expensive", reply_to=None, gas_fee=999)
        post.sign_tx(priv)
        with pytest.raises(ValidationError, match="insufficient"):
            validate_transaction_state(post, chain.state, chain.known_posts)

    def test_unknown_reply_to(self):
        chain, priv, pub = make_chain()
        post = Post(author=pub, nonce=0, timestamp=1000015, body="reply", reply_to=b"\xff" * 32, gas_fee=1)
        post.sign_tx(priv)
        with pytest.raises(ValidationError, match="unknown post"):
            validate_transaction_state(post, chain.state, chain.known_posts)

    def test_unknown_endorse_target(self):
        chain, priv, pub = make_chain()
        e = Endorse(author=pub, nonce=0, target=b"\xff" * 32, amount=0, message="", gas_fee=1)
        e.sign_tx(priv)
        with pytest.raises(ValidationError, match="not a known post"):
            validate_transaction_state(e, chain.state, chain.known_posts)

    def test_transfer_insufficient(self):
        chain, priv, pub = make_chain()
        _, r = make_keys()
        tx = Transfer(sender=pub, recipient=r, amount=9999, nonce=0, gas_fee=1)
        tx.sign_tx(priv)
        with pytest.raises(ValidationError, match="insufficient"):
            validate_transaction_state(tx, chain.state, chain.known_posts)

    def test_nonexistent_sender(self):
        chain, _, _ = make_chain()
        priv2, pub2 = make_keys()
        _, r = make_keys()
        tx = Transfer(sender=pub2, recipient=r, amount=1, nonce=0, gas_fee=1)
        tx.sign_tx(priv2)
        with pytest.raises(ValidationError, match="does not exist"):
            validate_transaction_state(tx, chain.state, chain.known_posts)


# -- Block validation --

class TestBlockValidation:
    def test_valid_block_accepted(self):
        chain, priv, pub = make_chain()
        post = Post(author=pub, nonce=0, timestamp=1000015, body="valid", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        block = build_block(chain, [post], pub, 1000015)
        chain.add_block(block, current_time=1000020)
        assert chain.height == 1

    def test_wrong_prev_hash(self):
        chain, priv, pub = make_chain()
        block = build_block(chain, [], pub, 1000015)
        block.header.prev_hash = b"\xff" * 32
        with pytest.raises(ValidationError, match="prev_hash"):
            chain.add_block(block, current_time=1000020)

    def test_wrong_height(self):
        chain, priv, pub = make_chain()
        block = build_block(chain, [], pub, 1000015)
        block.header.height = 99
        with pytest.raises(ValidationError, match="height"):
            chain.add_block(block, current_time=1000020)

    def test_future_timestamp(self):
        chain, priv, pub = make_chain()
        block = build_block(chain, [], pub, 9999999999)
        with pytest.raises(ValidationError, match="future"):
            chain.add_block(block, current_time=1000015)

    def test_wrong_coinbase_amount(self):
        chain, priv, pub = make_chain()
        cb = Coinbase(recipient=pub, amount=9999, height=1)
        tx_root = merkle_root([cb.tx_hash()])
        working = chain.state.copy()
        working.apply_transaction(cb, pub)
        header = BlockHeader(
            PROTOCOL_VERSION, 1, chain.tip.block_hash(), 1000015,
            pub, GENESIS_DIFFICULTY, 0, tx_root, working.state_root(), 1,
        )
        block = Block(header=header, transactions=[cb])
        with pytest.raises(ValidationError, match="amount"):
            chain.add_block(block, current_time=1000020)

    def test_duplicate_transaction(self):
        chain, priv, pub = make_chain()
        post = Post(author=pub, nonce=0, timestamp=1000015, body="dup", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        # include the same post twice
        cb = Coinbase(recipient=pub, amount=block_reward(1), height=1)
        all_txs = [cb, post, post]
        tx_root = merkle_root([tx.tx_hash() for tx in all_txs])
        working = chain.state.copy()
        working.apply_transaction(cb, pub)
        working.apply_transaction(post, pub)
        header = BlockHeader(
            PROTOCOL_VERSION, 1, chain.tip.block_hash(), 1000015,
            pub, GENESIS_DIFFICULTY, 0, tx_root, working.state_root(), 3,
        )
        block = Block(header=header, transactions=all_txs)
        with pytest.raises(ValidationError, match="duplicate"):
            chain.add_block(block, current_time=1000020)

    def test_wrong_merkle_root(self):
        chain, priv, pub = make_chain()
        block = build_block(chain, [], pub, 1000015)
        block.header.tx_merkle_root = b"\x00" * 32
        with pytest.raises(ValidationError, match="merkle"):
            chain.add_block(block, current_time=1000020)

    def test_wrong_state_root(self):
        chain, priv, pub = make_chain()
        block = build_block(chain, [], pub, 1000015)
        block.header.state_root = b"\x00" * 32
        with pytest.raises(ValidationError, match="state_root"):
            chain.add_block(block, current_time=1000020)
