"""Tests for chain reorganization."""
import pytest

from jiji.core.block import Block, BlockHeader
from jiji.core.chain import Blockchain
from jiji.core.config import GENESIS_DIFFICULTY, MAX_REORG_DEPTH, PROTOCOL_VERSION, block_reward
from jiji.core.crypto import generate_keypair
from jiji.core.merkle import merkle_root
from jiji.core.state import WorldState
from jiji.core.transaction import Coinbase, Endorse, Post, Transfer
from jiji.core.validation import ValidationError, validate_block_structure
from jiji.storage.store import BlockStore


def build_block_on_chain(chain: Blockchain, txs: list, miner_pub: bytes, timestamp: int) -> Block:
    """Build a valid block extending chain's tip."""
    parent = chain.tip
    height = chain.height + 1
    cb = Coinbase(recipient=miner_pub, amount=block_reward(height), height=height)
    all_txs = [cb] + txs

    working = chain.state.copy()
    working_posts = set(chain.known_posts)
    working_authors = dict(chain.post_authors)

    # Apply new transactions
    for tx in all_txs:
        target_author = None
        if isinstance(tx, Endorse) and tx.amount > 0:
            target_author = working_authors.get(tx.target)
        working.apply_transaction(tx, miner_pub, target_author)
        if isinstance(tx, Post):
            working_posts.add(tx.tx_hash())
            working_authors[tx.tx_hash()] = tx.author

    tx_root = merkle_root([tx.tx_hash() for tx in all_txs])
    header = BlockHeader(
        PROTOCOL_VERSION, height, parent.block_hash(), timestamp,
        miner_pub, GENESIS_DIFFICULTY, 0, tx_root, working.state_root(),
        len(all_txs),
    )
    block = Block(header=header, transactions=all_txs)
    while not block.meets_difficulty():
        block.header.nonce += 1
    return block


class TestReorgBasic:
    """Basic reorganization tests."""

    def test_no_reorg_when_fork_shorter(self):
        chain = Blockchain()
        priv, pub = generate_keypair()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)

        # Main chain: genesis -> A1 -> A2 -> A3
        a1 = build_block_on_chain(chain, [], pub, 1000001)
        chain.add_block(a1, current_time=1000001)
        a2 = build_block_on_chain(chain, [], pub, 1000002)
        chain.add_block(a2, current_time=1000002)
        a3 = build_block_on_chain(chain, [], pub, 1000003)
        chain.add_block(a3, current_time=1000003)

        # Fork: genesis -> B1 (different timestamp to create different block)
        fork_chain = Blockchain()
        fork_chain._apply_block(genesis)
        b1 = build_block_on_chain(fork_chain, [], pub, 2000001)
        chain.store_fork_block(b1)

        # Fork is shorter, should raise
        with pytest.raises(ValueError, match="not longer"):
            chain.reorganize(b1.block_hash())

        # Main chain unchanged
        assert chain.height == 3
        assert chain.tip.block_hash() == a3.block_hash()

    def test_reorg_to_longer_fork(self):
        chain = Blockchain()
        priv, pub = generate_keypair()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)

        # Main chain: genesis -> A1 -> A2
        a1 = build_block_on_chain(chain, [], pub, 1000001)
        chain.add_block(a1, current_time=1000001)
        a2 = build_block_on_chain(chain, [], pub, 1000002)
        chain.add_block(a2, current_time=1000002)

        # Fork: genesis -> B1 -> B2 -> B3 (use different timestamps to create different blocks)
        fork_chain = Blockchain()
        fork_chain._apply_block(genesis)
        b1 = build_block_on_chain(fork_chain, [], pub, 2000001)  # different timestamp
        fork_chain._apply_block(b1)
        b2 = build_block_on_chain(fork_chain, [], pub, 2000002)
        fork_chain._apply_block(b2)
        b3 = build_block_on_chain(fork_chain, [], pub, 2000003)

        chain.store_fork_block(b1)
        chain.store_fork_block(b2)
        chain.store_fork_block(b3)

        # Perform reorg
        orphaned = chain.reorganize(b3.block_hash())

        # Verify new main chain
        assert chain.height == 3
        assert chain.tip.block_hash() == b3.block_hash()
        assert chain.main_chain[0] == genesis.block_hash()
        assert chain.main_chain[1] == b1.block_hash()
        assert chain.main_chain[2] == b2.block_hash()
        assert chain.main_chain[3] == b3.block_hash()

        # Verify orphaned blocks
        assert len(orphaned) == 2
        assert orphaned[0].block_hash() == a1.block_hash()
        assert orphaned[1].block_hash() == a2.block_hash()

class TestReorgDepthLimit:
    """Test reorg depth limit."""

    def test_rejects_deep_reorg(self):
        """Test that reorgs deeper than MAX_REORG_DEPTH are rejected."""
        chain = Blockchain()
        _, pub = generate_keypair()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)

        # Build a shorter main chain (within difficulty adjustment window)
        for i in range(50):
            block = build_block_on_chain(chain, [], pub, 1000001 + i)
            chain.add_block(block, current_time=1000001 + i)

        # Build fork from genesis (longer than main, but too deep to reorg)
        fork_chain = Blockchain()
        fork_chain._apply_block(genesis)
        for i in range(52):
            block = build_block_on_chain(fork_chain, [], pub, 2000001 + i)
            fork_chain._apply_block(block)
            chain.store_fork_block(block)

        # Manually test depth check by setting MAX_REORG_DEPTH to something small
        # Store old value
        import jiji.core.chain
        old_max = jiji.core.chain.MAX_REORG_DEPTH
        jiji.core.chain.MAX_REORG_DEPTH = 10

        try:
            # Attempt reorg should fail (fork is 52 blocks, main is 50, depth = 50)
            with pytest.raises(ValueError, match="exceeds maximum"):
                chain.reorganize(fork_chain.tip.block_hash())
        finally:
            # Restore
            jiji.core.chain.MAX_REORG_DEPTH = old_max


class TestReorgWithPersistence:
    """Test reorg with persistent storage."""

    def test_reorg_persists_to_store(self):
        store = BlockStore(":memory:")
        chain = Blockchain(store=store)
        _, pub = generate_keypair()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)

        # Main chain
        a1 = build_block_on_chain(chain, [], pub, 1000001)
        chain.add_block(a1, current_time=1000001)
        a2 = build_block_on_chain(chain, [], pub, 1000002)
        chain.add_block(a2, current_time=1000002)

        # Fork (different timestamps)
        fork_chain = Blockchain()
        fork_chain._apply_block(genesis)
        b1 = build_block_on_chain(fork_chain, [], pub, 2000001)
        fork_chain._apply_block(b1)
        b2 = build_block_on_chain(fork_chain, [], pub, 2000002)
        fork_chain._apply_block(b2)
        b3 = build_block_on_chain(fork_chain, [], pub, 2000003)

        chain.store_fork_block(b1)
        chain.store_fork_block(b2)
        chain.store_fork_block(b3)

        # Reorg
        chain.reorganize(b3.block_hash())

        # Verify store updated
        assert store.get_tip_hash() == b3.block_hash()
        hashes = store.get_main_chain_hashes()
        assert len(hashes) == 4
        assert hashes[3] == b3.block_hash()

        store.close()

    def test_load_from_store(self):
        store = BlockStore(":memory:")
        chain = Blockchain(store=store)
        _, pub = generate_keypair()

        genesis = chain.initialize_genesis(pub, timestamp=1000000)
        block1 = build_block_on_chain(chain, [], pub, 1000001)
        chain.add_block(block1, current_time=1000001)
        block2 = build_block_on_chain(chain, [], pub, 1000002)
        chain.add_block(block2, current_time=1000002)

        # Create new chain and load from store
        chain2 = Blockchain(store=store)
        chain2.load_from_store()

        assert chain2.height == 2
        assert chain2.tip.block_hash() == block2.block_hash()
        assert len(chain2.main_chain) == 3
        assert chain2.state.get_account(pub).balance == block_reward(0) + block_reward(1) + block_reward(2)

        store.close()


class TestValidateBlockStructure:
    """Test lightweight fork block validation."""

    def test_validates_correct_block(self):
        _, pub = generate_keypair()
        chain = Blockchain()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)

        block = build_block_on_chain(chain, [], pub, 1000001)
        validate_block_structure(block, genesis, 1000001)  # should not raise

    def test_rejects_wrong_prev_hash(self):
        _, pub = generate_keypair()
        chain = Blockchain()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)

        block = build_block_on_chain(chain, [], pub, 1000001)
        block.header.prev_hash = bytes(32)  # wrong

        with pytest.raises(ValidationError, match="prev_hash"):
            validate_block_structure(block, genesis, 1000001)

    def test_rejects_wrong_height(self):
        _, pub = generate_keypair()
        chain = Blockchain()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)

        block = build_block_on_chain(chain, [], pub, 1000001)
        block.header.height = 99  # wrong

        with pytest.raises(ValidationError, match="height"):
            validate_block_structure(block, genesis, 1000001)

    def test_rejects_future_timestamp(self):
        _, pub = generate_keypair()
        chain = Blockchain()
        genesis = chain.initialize_genesis(pub, timestamp=1000000)

        block = build_block_on_chain(chain, [], pub, 1000001)
        # Test future timestamp rejection
        with pytest.raises(ValidationError, match="future"):
            validate_block_structure(block, genesis, 1000)
