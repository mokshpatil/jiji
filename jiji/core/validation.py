from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

from jiji.core.block import Block
from jiji.core.config import (
    BLOCK_TIME_TARGET,
    DIFFICULTY_ADJUSTMENT_WINDOW,
    ENDORSE_MESSAGE_LIMIT,
    GENESIS_DIFFICULTY,
    MAX_BLOCK_SIZE,
    MAX_DIFFICULTY_ADJUSTMENT,
    MAX_FUTURE_TIMESTAMP,
    MAX_TARGET,
    MEDIAN_TIME_BLOCK_COUNT,
    MINIMUM_GAS_FEE,
    POST_BODY_LIMIT,
    PROTOCOL_VERSION,
    block_reward,
)
from jiji.core.merkle import merkle_root
from jiji.core.state import WorldState
from jiji.core.transaction import Coinbase, Endorse, Post, Transfer, Transaction

if TYPE_CHECKING:
    from jiji.core.chain import Blockchain


class ValidationError(Exception):
    pass


# -- Transaction format validation --


def validate_post_format(tx: Post) -> None:
    """Validate post structure and signature."""
    if not isinstance(tx.body, str) or not tx.body:
        raise ValidationError("post body must be a non-empty string")
    if len(tx.body) > POST_BODY_LIMIT:
        raise ValidationError(f"post body exceeds {POST_BODY_LIMIT} chars")
    if len(tx.author) != 32:
        raise ValidationError("author key must be 32 bytes")
    if tx.nonce < 0:
        raise ValidationError("nonce must be non-negative")
    if tx.gas_fee < MINIMUM_GAS_FEE:
        raise ValidationError(f"gas fee below minimum ({MINIMUM_GAS_FEE})")
    if tx.reply_to is not None and len(tx.reply_to) != 32:
        raise ValidationError("reply_to must be 32 bytes or null")
    if not tx.verify_signature():
        raise ValidationError("invalid post signature")


def validate_endorse_format(tx: Endorse) -> None:
    """Validate endorsement structure and signature."""
    if len(tx.author) != 32:
        raise ValidationError("author key must be 32 bytes")
    if len(tx.target) != 32:
        raise ValidationError("target must be 32 bytes")
    if tx.nonce < 0:
        raise ValidationError("nonce must be non-negative")
    if tx.amount < 0:
        raise ValidationError("amount must be non-negative")
    if len(tx.message) > ENDORSE_MESSAGE_LIMIT:
        raise ValidationError(f"message exceeds {ENDORSE_MESSAGE_LIMIT} chars")
    if tx.gas_fee < MINIMUM_GAS_FEE:
        raise ValidationError(f"gas fee below minimum ({MINIMUM_GAS_FEE})")
    if not tx.verify_signature():
        raise ValidationError("invalid endorsement signature")


def validate_transfer_format(tx: Transfer) -> None:
    """Validate transfer structure and signature."""
    if len(tx.sender) != 32:
        raise ValidationError("sender key must be 32 bytes")
    if len(tx.recipient) != 32:
        raise ValidationError("recipient key must be 32 bytes")
    if tx.sender == tx.recipient:
        raise ValidationError("sender and recipient must differ")
    if tx.amount <= 0:
        raise ValidationError("transfer amount must be positive")
    if tx.nonce < 0:
        raise ValidationError("nonce must be non-negative")
    if tx.gas_fee < MINIMUM_GAS_FEE:
        raise ValidationError(f"gas fee below minimum ({MINIMUM_GAS_FEE})")
    if not tx.verify_signature():
        raise ValidationError("invalid transfer signature")


def validate_coinbase_format(tx: Coinbase, expected_height: int) -> None:
    """Validate coinbase structure and reward amount."""
    if len(tx.recipient) != 32:
        raise ValidationError("coinbase recipient must be 32 bytes")
    if tx.height != expected_height:
        raise ValidationError("coinbase height mismatch")
    expected_reward = block_reward(expected_height)
    if tx.amount != expected_reward:
        raise ValidationError(
            f"coinbase amount {tx.amount} != expected {expected_reward}"
        )


def validate_transaction_format(tx: Transaction, expected_height: int = 0) -> None:
    """Dispatch format validation to the appropriate type handler."""
    if isinstance(tx, Post):
        validate_post_format(tx)
    elif isinstance(tx, Endorse):
        validate_endorse_format(tx)
    elif isinstance(tx, Transfer):
        validate_transfer_format(tx)
    elif isinstance(tx, Coinbase):
        validate_coinbase_format(tx, expected_height)
    else:
        raise ValidationError(f"unknown transaction type: {type(tx)}")


# -- State validation (balance, nonce) --


def validate_transaction_state(
    tx: Transaction,
    state: WorldState,
    known_posts: set[bytes],
) -> None:
    """Validate a transaction against the current world state."""
    if isinstance(tx, Post):
        _validate_post_state(tx, state, known_posts)
    elif isinstance(tx, Endorse):
        _validate_endorse_state(tx, state, known_posts)
    elif isinstance(tx, Transfer):
        _validate_transfer_state(tx, state)
    elif isinstance(tx, Coinbase):
        pass  # coinbase has no state preconditions


def _validate_post_state(
    tx: Post, state: WorldState, known_posts: set[bytes]
) -> None:
    account = state.get_account(tx.author)
    if account is None:
        raise ValidationError("author account does not exist")
    if tx.nonce != account.nonce:
        raise ValidationError(
            f"nonce mismatch: tx={tx.nonce}, expected={account.nonce}"
        )
    if account.balance < tx.gas_fee:
        raise ValidationError("insufficient balance for gas fee")
    if tx.reply_to is not None and tx.reply_to not in known_posts:
        raise ValidationError("reply_to references unknown post")


def _validate_endorse_state(
    tx: Endorse, state: WorldState, known_posts: set[bytes]
) -> None:
    account = state.get_account(tx.author)
    if account is None:
        raise ValidationError("author account does not exist")
    if tx.nonce != account.nonce:
        raise ValidationError(
            f"nonce mismatch: tx={tx.nonce}, expected={account.nonce}"
        )
    total_cost = tx.gas_fee + tx.amount
    if account.balance < total_cost:
        raise ValidationError("insufficient balance for gas + tip")
    if tx.target not in known_posts:
        raise ValidationError("endorsement target is not a known post")


def _validate_transfer_state(tx: Transfer, state: WorldState) -> None:
    account = state.get_account(tx.sender)
    if account is None:
        raise ValidationError("sender account does not exist")
    if tx.nonce != account.nonce:
        raise ValidationError(
            f"nonce mismatch: tx={tx.nonce}, expected={account.nonce}"
        )
    total_cost = tx.amount + tx.gas_fee
    if account.balance < total_cost:
        raise ValidationError("insufficient balance for transfer + gas")


# -- Difficulty computation --


def compute_expected_difficulty(chain: Blockchain, height: int) -> int:
    """Compute the expected difficulty for a block at the given height."""
    if height == 0:
        return GENESIS_DIFFICULTY
    if height % DIFFICULTY_ADJUSTMENT_WINDOW != 0:
        return chain.get_block_by_height(height - 1).header.difficulty

    # Adjustment window boundary
    window_end = chain.get_block_by_height(height - 1)
    window_start_height = height - DIFFICULTY_ADJUSTMENT_WINDOW
    window_start = chain.get_block_by_height(window_start_height)
    if window_start is None or window_end is None:
        return GENESIS_DIFFICULTY

    actual_time = window_end.header.timestamp - window_start.header.timestamp
    if actual_time <= 0:
        actual_time = 1
    expected_time = DIFFICULTY_ADJUSTMENT_WINDOW * BLOCK_TIME_TARGET

    ratio = expected_time / actual_time
    ratio = max(1.0 / MAX_DIFFICULTY_ADJUSTMENT, min(MAX_DIFFICULTY_ADJUSTMENT, ratio))
    new_difficulty = max(1, int(window_end.header.difficulty * ratio))
    return new_difficulty


# -- Block validation --


def validate_block(block: Block, chain: Blockchain, current_time: int) -> None:
    """Full block validation against the chain. Raises ValidationError."""
    header = block.header

    # Version
    if header.version != PROTOCOL_VERSION:
        raise ValidationError(f"unsupported version: {header.version}")

    # Height continuity
    expected_height = chain.height + 1
    if header.height != expected_height:
        raise ValidationError(
            f"height mismatch: got {header.height}, expected {expected_height}"
        )

    # Previous hash linkage
    if chain.tip is not None:
        expected_prev = chain.tip.block_hash()
    else:
        expected_prev = bytes(32)
    if header.prev_hash != expected_prev:
        raise ValidationError("prev_hash does not match tip")

    # Timestamp: must exceed median of recent blocks
    if chain.height >= 0:
        recent = chain.get_recent_timestamps(MEDIAN_TIME_BLOCK_COUNT)
        if recent and header.timestamp <= statistics.median(recent):
            raise ValidationError("timestamp not above median of recent blocks")

    # Timestamp: not too far in the future
    if header.timestamp > current_time + MAX_FUTURE_TIMESTAMP:
        raise ValidationError("timestamp too far in the future")

    # Difficulty
    expected_diff = compute_expected_difficulty(chain, header.height)
    if header.difficulty != expected_diff:
        raise ValidationError(
            f"difficulty mismatch: got {header.difficulty}, expected {expected_diff}"
        )

    # Proof of work
    if not block.meets_difficulty():
        raise ValidationError("block does not meet difficulty target")

    # Transaction count
    if header.tx_count != len(block.transactions):
        raise ValidationError("tx_count does not match transaction list")

    # Must have at least the coinbase
    if not block.transactions:
        raise ValidationError("block has no transactions")

    # First transaction must be coinbase
    coinbase = block.transactions[0]
    if not isinstance(coinbase, Coinbase):
        raise ValidationError("first transaction must be coinbase")
    validate_coinbase_format(coinbase, header.height)
    if coinbase.recipient != header.miner:
        raise ValidationError("coinbase recipient must match block miner")

    # No other coinbase transactions allowed
    for tx in block.transactions[1:]:
        if isinstance(tx, Coinbase):
            raise ValidationError("only one coinbase per block")

    # Validate and apply each transaction on a working state copy
    working_state = chain.state.copy()
    working_posts = set(chain.known_posts)
    working_authors = dict(chain.post_authors)
    seen_hashes: set[bytes] = set()

    for i, tx in enumerate(block.transactions):
        tx_h = tx.tx_hash()

        # No duplicate transactions
        if tx_h in seen_hashes or tx_h in chain.tx_index:
            raise ValidationError(f"duplicate transaction at index {i}")
        seen_hashes.add(tx_h)

        # Format validation
        validate_transaction_format(tx, header.height)

        # State validation (skip coinbase, already checked)
        if not isinstance(tx, Coinbase):
            validate_transaction_state(tx, working_state, working_posts)

        # Resolve target author for endorsement tips
        target_author = None
        if isinstance(tx, Endorse) and tx.amount > 0:
            target_author = working_authors.get(tx.target)

        # Apply to working state
        working_state.apply_transaction(tx, header.miner, target_author)

        # Track new posts for intra-block reply/endorse references
        if isinstance(tx, Post):
            working_posts.add(tx_h)
            working_authors[tx_h] = tx.author

    # Merkle root verification
    expected_merkle = block.compute_tx_merkle_root()
    if header.tx_merkle_root != expected_merkle:
        raise ValidationError("tx_merkle_root mismatch")

    # State root verification
    expected_state_root = working_state.state_root()
    if header.state_root != expected_state_root:
        raise ValidationError("state_root mismatch")

    # Block size limit
    if block.serialized_size() > MAX_BLOCK_SIZE:
        raise ValidationError("block exceeds maximum size")
