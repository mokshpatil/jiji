"""Tests for SQLite block storage."""
import pytest

from jiji.core.block import Block, BlockHeader
from jiji.core.config import GENESIS_DIFFICULTY, PROTOCOL_VERSION, block_reward
from jiji.core.crypto import generate_keypair
from jiji.core.merkle import merkle_root
from jiji.core.state import WorldState
from jiji.core.transaction import Coinbase
from jiji.storage.store import BlockStore


def make_test_block(height: int, prev_hash: bytes, miner_pubkey: bytes) -> Block:
    """Helper to create a valid test block."""
    reward = block_reward(height)
    coinbase = Coinbase(recipient=miner_pubkey, amount=reward, height=height)

    state = WorldState()
    state.apply_transaction(coinbase, miner_pubkey)

    header = BlockHeader(
        version=PROTOCOL_VERSION,
        height=height,
        prev_hash=prev_hash,
        timestamp=1000000 + height,
        miner=miner_pubkey,
        difficulty=GENESIS_DIFFICULTY,
        nonce=0,
        tx_merkle_root=merkle_root([coinbase.tx_hash()]),
        state_root=state.state_root(),
        tx_count=1,
    )

    block = Block(header=header, transactions=[coinbase])
    while not block.meets_difficulty():
        block.header.nonce += 1
    return block


class TestBlockStoreBasic:
    """Basic store operations."""

    def test_create_and_close(self):
        store = BlockStore(":memory:")
        store.close()

    def test_put_and_get_block(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()
        block = make_test_block(0, bytes(32), pub)

        store.put_block(block)
        retrieved = store.get_block(block.block_hash())

        assert retrieved is not None
        assert retrieved.block_hash() == block.block_hash()
        assert retrieved.header.height == 0
        store.close()

    def test_get_nonexistent_returns_none(self):
        store = BlockStore(":memory:")
        result = store.get_block(bytes(32))
        assert result is None
        store.close()

    def test_has_block(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()
        block = make_test_block(0, bytes(32), pub)

        assert not store.has_block(block.block_hash())
        store.put_block(block)
        assert store.has_block(block.block_hash())
        store.close()

    def test_block_count(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()

        assert store.block_count() == 0

        block0 = make_test_block(0, bytes(32), pub)
        store.put_block(block0)
        assert store.block_count() == 1

        block1 = make_test_block(1, block0.block_hash(), pub)
        store.put_block(block1)
        assert store.block_count() == 2
        store.close()

    def test_put_block_idempotent(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()
        block = make_test_block(0, bytes(32), pub)

        store.put_block(block)
        store.put_block(block)  # should not error
        assert store.block_count() == 1
        store.close()


class TestBlockStoreMainChain:
    """Main chain operations."""

    def test_main_chain_hashes(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()

        block0 = make_test_block(0, bytes(32), pub)
        store.put_block(block0, on_main_chain=True)

        block1 = make_test_block(1, block0.block_hash(), pub)
        store.put_block(block1, on_main_chain=True)

        block2 = make_test_block(2, block1.block_hash(), pub)
        store.put_block(block2, on_main_chain=True)

        hashes = store.get_main_chain_hashes()
        assert len(hashes) == 3
        assert hashes[0] == block0.block_hash()
        assert hashes[1] == block1.block_hash()
        assert hashes[2] == block2.block_hash()
        store.close()

    def test_tip_hash_updates(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()

        block0 = make_test_block(0, bytes(32), pub)
        store.put_block(block0, on_main_chain=True)
        assert store.get_tip_hash() == block0.block_hash()

        block1 = make_test_block(1, block0.block_hash(), pub)
        store.put_block(block1, on_main_chain=True)
        assert store.get_tip_hash() == block1.block_hash()
        store.close()

    def test_set_main_chain(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()

        block0 = make_test_block(0, bytes(32), pub)
        block1 = make_test_block(1, block0.block_hash(), pub)
        block2 = make_test_block(2, block1.block_hash(), pub)

        # Store all with main chain flags
        store.put_block(block0, on_main_chain=True)
        store.put_block(block1, on_main_chain=True)
        store.put_block(block2, on_main_chain=True)

        # Now set main chain to only block0 and block1
        store.set_main_chain([block0.block_hash(), block1.block_hash()])

        hashes = store.get_main_chain_hashes()
        assert len(hashes) == 2
        assert hashes[0] == block0.block_hash()
        assert hashes[1] == block1.block_hash()
        assert store.get_tip_hash() == block1.block_hash()
        store.close()

    def test_get_main_chain_block_at_height(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()

        block0 = make_test_block(0, bytes(32), pub)
        block1 = make_test_block(1, block0.block_hash(), pub)

        store.put_block(block0, on_main_chain=True)
        store.put_block(block1, on_main_chain=True)

        retrieved = store.get_main_chain_block_at_height(1)
        assert retrieved is not None
        assert retrieved.block_hash() == block1.block_hash()

        assert store.get_main_chain_block_at_height(99) is None
        store.close()


class TestBlockStoreForks:
    """Fork-related operations."""

    def test_get_blocks_at_height(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()

        block0 = make_test_block(0, bytes(32), pub)
        store.put_block(block0, on_main_chain=True)

        # Two different blocks at height 1
        block1a = make_test_block(1, block0.block_hash(), pub)
        block1b_header = BlockHeader(
            version=PROTOCOL_VERSION,
            height=1,
            prev_hash=block0.block_hash(),
            timestamp=1000001,
            miner=pub,
            difficulty=GENESIS_DIFFICULTY,
            nonce=999,  # different nonce
            tx_merkle_root=block1a.header.tx_merkle_root,
            state_root=block1a.header.state_root,
            tx_count=1,
        )
        block1b = Block(header=block1b_header, transactions=block1a.transactions)
        while not block1b.meets_difficulty():
            block1b.header.nonce += 1

        store.put_block(block1a, on_main_chain=True)
        store.put_block(block1b, on_main_chain=False)

        blocks = store.get_blocks_at_height(1)
        assert len(blocks) == 2
        hashes = {b.block_hash() for b in blocks}
        assert block1a.block_hash() in hashes
        assert block1b.block_hash() in hashes
        store.close()

    def test_get_children(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()

        block0 = make_test_block(0, bytes(32), pub)
        store.put_block(block0, on_main_chain=True)

        # Two children of block0
        block1a = make_test_block(1, block0.block_hash(), pub)
        block1b_header = BlockHeader(
            version=PROTOCOL_VERSION,
            height=1,
            prev_hash=block0.block_hash(),
            timestamp=1000001,
            miner=pub,
            difficulty=GENESIS_DIFFICULTY,
            nonce=999,
            tx_merkle_root=block1a.header.tx_merkle_root,
            state_root=block1a.header.state_root,
            tx_count=1,
        )
        block1b = Block(header=block1b_header, transactions=block1a.transactions)
        while not block1b.meets_difficulty():
            block1b.header.nonce += 1

        store.put_block(block1a, on_main_chain=True)
        store.put_block(block1b, on_main_chain=False)

        children = store.get_children(block0.block_hash())
        assert len(children) == 2
        store.close()

    def test_fork_block_not_on_main_chain(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()

        block0 = make_test_block(0, bytes(32), pub)
        block1 = make_test_block(1, block0.block_hash(), pub)

        store.put_block(block0, on_main_chain=True)
        store.put_block(block1, on_main_chain=False)

        hashes = store.get_main_chain_hashes()
        assert len(hashes) == 1
        assert hashes[0] == block0.block_hash()
        store.close()

    def test_get_chain_from_to(self):
        store = BlockStore(":memory:")
        _, pub = generate_keypair()

        block0 = make_test_block(0, bytes(32), pub)
        block1 = make_test_block(1, block0.block_hash(), pub)
        block2 = make_test_block(2, block1.block_hash(), pub)

        store.put_block(block0)
        store.put_block(block1)
        store.put_block(block2)

        # Get chain from block0 to block2 (should return block1, block2)
        chain = store.get_chain_from_to(block0.block_hash(), block2.block_hash())
        assert len(chain) == 2
        assert chain[0].block_hash() == block1.block_hash()
        assert chain[1].block_hash() == block2.block_hash()
        store.close()
