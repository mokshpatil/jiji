from __future__ import annotations

import statistics
import time

from jiji.core.block import Block, BlockHeader
from jiji.core.config import MAX_BLOCK_SIZE, MEDIAN_TIME_BLOCK_COUNT, PROTOCOL_VERSION, block_reward
from jiji.core.merkle import merkle_root
from jiji.core.state import WorldState
from jiji.core.transaction import Coinbase, Endorse, Post, Transaction
from jiji.core.validation import ValidationError, compute_expected_difficulty
from jiji.mining.mempool import Mempool

if __import__("typing").TYPE_CHECKING:
    from jiji.core.chain import Blockchain


class Miner:
    """Assembles candidate blocks from the mempool and mines them via PoW."""

    def __init__(self, chain: Blockchain, mempool: Mempool, miner_pubkey: bytes):
        self._chain = chain
        self._mempool = mempool
        self._pubkey = miner_pubkey

    def create_block_template(self) -> Block:
        """Build a candidate block from mempool transactions, ready for PoW."""
        height = self._chain.height + 1
        prev_hash = self._chain.tip.block_hash() if self._chain.tip else bytes(32)
        difficulty = compute_expected_difficulty(self._chain, height)
        # ensure timestamp exceeds median of recent blocks
        timestamp = int(time.time())
        recent = self._chain.get_recent_timestamps(MEDIAN_TIME_BLOCK_COUNT)
        if recent:
            median = int(statistics.median(recent))
            timestamp = max(timestamp, median + 1)

        # coinbase
        reward = block_reward(height)
        coinbase = Coinbase(recipient=self._pubkey, amount=reward, height=height)
        selected: list[Transaction] = [coinbase]

        # simulate state to select valid transactions
        working_state = self._chain.state.copy()
        working_state.apply_transaction(coinbase, self._pubkey)
        working_posts = set(self._chain.known_posts)
        working_authors = dict(self._chain.post_authors)

        for tx in self._mempool.get_pending():
            try:
                # validate against working state (nonce and balance may have shifted)
                from jiji.core.validation import (
                    validate_transaction_format,
                    validate_transaction_state,
                )
                validate_transaction_format(tx)
                validate_transaction_state(tx, working_state, working_posts)
            except ValidationError:
                continue

            # check block size won't be exceeded
            test_txs = selected + [tx]
            if _estimate_block_size(test_txs) > MAX_BLOCK_SIZE:
                break

            # resolve target author for endorsement tips
            target_author = None
            if isinstance(tx, Endorse) and tx.amount > 0:
                target_author = working_authors.get(tx.target)

            working_state.apply_transaction(tx, self._pubkey, target_author)
            selected.append(tx)

            if isinstance(tx, Post):
                tx_h = tx.tx_hash()
                working_posts.add(tx_h)
                working_authors[tx_h] = tx.author

        # compute roots
        tx_hashes = [tx.tx_hash() for tx in selected]
        tx_root = merkle_root(tx_hashes)
        state_root = working_state.state_root()

        header = BlockHeader(
            version=PROTOCOL_VERSION,
            height=height,
            prev_hash=prev_hash,
            timestamp=timestamp,
            miner=self._pubkey,
            difficulty=difficulty,
            nonce=0,
            tx_merkle_root=tx_root,
            state_root=state_root,
            tx_count=len(selected),
        )
        return Block(header=header, transactions=selected)

    def mine_block(self, block: Block, max_iterations: int = 0) -> Block | None:
        """Grind nonce until PoW is satisfied. Returns solved block or None."""
        iterations = 0
        while not block.meets_difficulty():
            block.header.nonce += 1
            iterations += 1
            if max_iterations > 0 and iterations >= max_iterations:
                return None
        return block

    def mine_next(self, current_time: int | None = None) -> Block | None:
        """Create template, mine, add to chain, purge mempool. Returns the block."""
        template = self.create_block_template()
        block = self.mine_block(template)
        if block is None:
            return None
        self._chain.add_block(block, current_time=current_time or template.header.timestamp + 1)
        self._mempool.remove_confirmed(block)
        self._mempool.revalidate()
        return block


def _estimate_block_size(txs: list[Transaction]) -> int:
    """Rough byte estimate for a block containing these transactions."""
    # each tx serializes to roughly its dict JSON size
    total = 200  # header overhead estimate
    for tx in txs:
        total += len(str(tx.to_dict()))
    return total
