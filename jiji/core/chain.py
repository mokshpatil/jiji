from __future__ import annotations

import time

from jiji.core.block import Block, BlockHeader
from jiji.core.config import GENESIS_DIFFICULTY, PROTOCOL_VERSION, block_reward
from jiji.core.merkle import merkle_root
from jiji.core.state import WorldState
from jiji.core.transaction import Coinbase, Endorse, Post, Transaction
from jiji.core.validation import validate_block


class Blockchain:
    """Manages the chain of blocks, world state, and transaction index."""

    def __init__(self):
        self.blocks: dict[bytes, Block] = {}
        self.main_chain: list[bytes] = []
        self.state: WorldState = WorldState()
        self.tx_index: dict[bytes, bytes] = {}
        self.known_posts: set[bytes] = set()
        self.post_authors: dict[bytes, bytes] = {}

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
