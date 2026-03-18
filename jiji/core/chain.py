from __future__ import annotations

import time
from typing import TYPE_CHECKING

from jiji.core.block import Block, BlockHeader
from jiji.core.config import GENESIS_DIFFICULTY, MAX_REORG_DEPTH, PROTOCOL_VERSION, block_reward
from jiji.core.merkle import merkle_root
from jiji.core.state import WorldState
from jiji.core.transaction import Coinbase, Endorse, Post, Transaction
from jiji.core.validation import validate_block

if TYPE_CHECKING:
    from jiji.storage.store import BlockStore


class Blockchain:
    """Manages the chain of blocks, world state, and transaction index."""

    def __init__(self, store: BlockStore | None = None):
        self.blocks: dict[bytes, Block] = {}
        self.main_chain: list[bytes] = []
        self.state: WorldState = WorldState()
        self.tx_index: dict[bytes, bytes] = {}
        self.known_posts: set[bytes] = set()
        self.post_authors: dict[bytes, bytes] = {}
        self._store: BlockStore | None = store

    @property
    def height(self) -> int:
        """Current chain height (-1 if empty)."""
        return len(self.main_chain) - 1

    @property
    def tip(self) -> Block | None:
        """The latest block on the main chain."""
        if not self.main_chain:
            return None
        return self.blocks[self.main_chain[-1]]

    def get_block_by_height(self, height: int) -> Block | None:
        """Retrieve a main-chain block by height."""
        if 0 <= height < len(self.main_chain):
            return self.blocks[self.main_chain[height]]
        return None

    def get_block_by_hash(self, block_hash: bytes) -> Block | None:
        """Retrieve a block by its hash."""
        return self.blocks.get(block_hash)

    def get_transaction(self, tx_hash: bytes) -> Transaction | None:
        """Look up a confirmed transaction by hash."""
        block_hash = self.tx_index.get(tx_hash)
        if block_hash is None:
            return None
        block = self.blocks[block_hash]
        for tx in block.transactions:
            if tx.tx_hash() == tx_hash:
                return tx
        return None

    def get_recent_timestamps(self, count: int) -> list[int]:
        """Get timestamps of the last N blocks."""
        start = max(0, len(self.main_chain) - count)
        timestamps = []
        for i in range(start, len(self.main_chain)):
            block = self.blocks[self.main_chain[i]]
            timestamps.append(block.header.timestamp)
        return timestamps

    def add_block(self, block: Block, current_time: int | None = None) -> None:
        """Validate and append a block. Raises ValidationError if invalid."""
        if current_time is None:
            current_time = int(time.time())
        validate_block(block, self, current_time)
        self._apply_block(block)

    def _apply_block(self, block: Block) -> None:
        """Apply a validated block to the chain and state."""
        block_hash = block.block_hash()
        self.blocks[block_hash] = block
        self.main_chain.append(block_hash)
        self._replay_block_state(block)

        # Persist to disk if store is configured
        if self._store is not None:
            self._store.put_block(block, on_main_chain=True)

    def _replay_block_state(self, block: Block) -> None:
        """Replay a block's effects on state and indexes without persistence."""
        block_hash = block.block_hash()
        miner = block.header.miner

        for tx in block.transactions:
            tx_h = tx.tx_hash()
            self.tx_index[tx_h] = block_hash

            if isinstance(tx, Post):
                self.known_posts.add(tx_h)
                self.post_authors[tx_h] = tx.author

            target_author = None
            if isinstance(tx, Endorse) and tx.amount > 0:
                target_author = self.post_authors.get(tx.target)

            self.state.apply_transaction(tx, miner, target_author)

    def initialize_genesis(
        self, miner_pubkey: bytes, timestamp: int | None = None
    ) -> Block:
        """Create and apply the genesis block. Returns the genesis block."""
        if self.main_chain:
            raise RuntimeError("chain already initialized")
        if timestamp is None:
            timestamp = int(time.time())

        reward = block_reward(0)
        coinbase = Coinbase(recipient=miner_pubkey, amount=reward, height=0)

        # compute roots
        tx_root = merkle_root([coinbase.tx_hash()])
        temp_state = WorldState()
        temp_state.apply_transaction(coinbase, miner_pubkey)
        s_root = temp_state.state_root()

        header = BlockHeader(
            version=PROTOCOL_VERSION,
            height=0,
            prev_hash=bytes(32),
            timestamp=timestamp,
            miner=miner_pubkey,
            difficulty=GENESIS_DIFFICULTY,
            nonce=0,
            tx_merkle_root=tx_root,
            state_root=s_root,
            tx_count=1,
        )

        block = Block(header=header, transactions=[coinbase])

        # mine (at GENESIS_DIFFICULTY=1, any hash works)
        while not block.meets_difficulty():
            block.header.nonce += 1

        self._apply_block(block)
        return block

    def load_from_store(self) -> None:
        """Rebuild in-memory chain state from the block store."""
        if self._store is None:
            raise RuntimeError("no store configured")

        tip_hash = self._store.get_tip_hash()
        if tip_hash is None:
            return  # empty store

        # Get ordered main chain hashes
        main_hashes = self._store.get_main_chain_hashes()

        # Clear in-memory state
        self.blocks.clear()
        self.main_chain.clear()
        self.state = WorldState()
        self.tx_index.clear()
        self.known_posts.clear()
        self.post_authors.clear()

        # Replay all main chain blocks
        for bh in main_hashes:
            block = self._store.get_block(bh)
            if block is None:
                raise RuntimeError(f"store missing block {bh.hex()}")
            self.blocks[bh] = block
            self.main_chain.append(bh)
            self._replay_block_state(block)

    def store_fork_block(self, block: Block) -> None:
        """Store a valid block that doesn't extend the current tip."""
        block_hash = block.block_hash()
        self.blocks[block_hash] = block
        if self._store is not None:
            self._store.put_block(block, on_main_chain=False)

    def get_chain_length_from(self, block_hash: bytes) -> int:
        """Count the length of the chain ending at block_hash."""
        count = 0
        current_hash = block_hash
        while current_hash != bytes(32):  # genesis prev_hash is all zeros
            block = self.blocks.get(current_hash)
            if block is None:
                break
            count += 1
            current_hash = block.header.prev_hash
        return count

    def reorganize(self, new_tip_hash: bytes) -> list[Block]:
        """Switch to a longer fork chain. Returns orphaned blocks."""
        # Step 1: Find fork point by walking back from new tip
        fork_blocks = []
        current = self.blocks.get(new_tip_hash)
        if current is None:
            raise ValueError("new tip block not found")

        main_chain_set = set(self.main_chain)
        while current.block_hash() not in main_chain_set:
            fork_blocks.append(current)
            parent_hash = current.header.prev_hash
            current = self.blocks.get(parent_hash)
            if current is None:
                raise ValueError("fork chain has missing blocks")

        fork_point_hash = current.block_hash()
        fork_blocks.reverse()  # ascending height order

        # Step 2: Find fork point index in main chain
        fork_point_idx = self.main_chain.index(fork_point_hash)

        # Step 3: Validate reorg constraints
        orphaned_hashes = self.main_chain[fork_point_idx + 1:]
        reorg_depth = len(orphaned_hashes)
        if reorg_depth > MAX_REORG_DEPTH:
            raise ValueError(f"reorg depth {reorg_depth} exceeds maximum {MAX_REORG_DEPTH}")

        new_chain_length = fork_point_idx + 1 + len(fork_blocks)
        if new_chain_length <= len(self.main_chain):
            raise ValueError("fork chain is not longer than main chain")

        # Step 4: Collect orphaned blocks
        orphaned_blocks = [self.blocks[h] for h in orphaned_hashes]

        # Step 5: Rebuild state from genesis through fork point, then fork blocks
        self.state = WorldState()
        self.tx_index.clear()
        self.known_posts.clear()
        self.post_authors.clear()

        new_main_chain = list(self.main_chain[:fork_point_idx + 1])

        # Replay blocks from genesis to fork point
        for bh in new_main_chain:
            block = self.blocks[bh]
            self._replay_block_state(block)

        # Apply fork blocks
        for block in fork_blocks:
            block_hash = block.block_hash()
            new_main_chain.append(block_hash)
            self._replay_block_state(block)

        self.main_chain = new_main_chain

        # Step 6: Persist updated main chain
        if self._store is not None:
            self._store.set_main_chain(self.main_chain)

        return orphaned_blocks
