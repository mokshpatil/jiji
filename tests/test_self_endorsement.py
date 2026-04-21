"""Hard-fork rule: an author cannot endorse their own post."""
import pytest

from jiji.core.block import Block, BlockHeader
from jiji.core.chain import Blockchain
from jiji.core.config import (
    GENESIS_DIFFICULTY,
    PROTOCOL_VERSION,
    block_reward,
)
from jiji.core.crypto import generate_keypair
from jiji.core.merkle import merkle_root
from jiji.core.transaction import Coinbase, Endorse, Post
from jiji.core.validation import ValidationError, validate_block


def _build_block(chain, txs, miner_pub, timestamp):
    height = chain.height + 1
    cb = Coinbase(recipient=miner_pub, amount=block_reward(height), height=height)
    all_txs = [cb] + txs
    tx_root = merkle_root([tx.tx_hash() for tx in all_txs])
    working = chain.state.copy()
    working_authors = dict(chain.post_authors)
    for tx in all_txs:
        target_author = None
        if isinstance(tx, Endorse) and tx.amount > 0:
            target_author = working_authors.get(tx.target)
        working.apply_transaction(tx, miner_pub, target_author)
        if isinstance(tx, Post):
            working_authors[tx.tx_hash()] = tx.author
    header = BlockHeader(
        PROTOCOL_VERSION, height, chain.tip.block_hash(), timestamp,
        miner_pub, GENESIS_DIFFICULTY, 0, tx_root, working.state_root(),
        len(all_txs),
    )
    block = Block(header=header, transactions=all_txs)
    while not block.meets_difficulty():
        block.header.nonce += 1
    return block


def test_self_endorsement_rejected():
    priv, pub = generate_keypair()
    chain = Blockchain()
    chain.initialize_genesis(pub, timestamp=1_000_000)

    # First, mine a block that contains a post from `pub`.
    post = Post(author=pub, nonce=0, timestamp=1_000_010,
                body="my own post", reply_to=None, gas_fee=1)
    post.sign_tx(priv)
    b1 = _build_block(chain, [post], pub, 1_000_020)
    chain.add_block(b1)

    # Now try to endorse my own post — must be rejected by the hard-fork rule.
    endorse = Endorse(
        author=pub, nonce=1, target=post.tx_hash(), amount=0,
        message="selfie", gas_fee=1,
    )
    endorse.sign_tx(priv)
    b2 = _build_block(chain, [endorse], pub, 1_000_030)
    with pytest.raises(ValidationError, match="self-endorsement"):
        validate_block(b2, chain, current_time=1_000_030)


def test_endorsing_someone_elses_post_ok():
    priv_a, pub_a = generate_keypair()
    priv_b, pub_b = generate_keypair()
    chain = Blockchain()
    chain.initialize_genesis(pub_a, timestamp=1_000_000)

    # Fund B with a transfer so B can pay gas for the endorse.
    from jiji.core.transaction import Transfer
    xfer = Transfer(sender=pub_a, recipient=pub_b, amount=10,
                    nonce=0, gas_fee=1)
    xfer.sign_tx(priv_a)
    b0 = _build_block(chain, [xfer], pub_a, 1_000_005)
    chain.add_block(b0)

    # A posts; B endorses A's post.
    post = Post(author=pub_a, nonce=1, timestamp=1_000_010,
                body="hello world", reply_to=None, gas_fee=1)
    post.sign_tx(priv_a)
    b1 = _build_block(chain, [post], pub_a, 1_000_020)
    chain.add_block(b1)

    endorse = Endorse(
        author=pub_b, nonce=0, target=post.tx_hash(), amount=0,
        message="nice", gas_fee=1,
    )
    endorse.sign_tx(priv_b)
    b2 = _build_block(chain, [endorse], pub_a, 1_000_030)
    validate_block(b2, chain, current_time=1_000_030)  # should not raise
