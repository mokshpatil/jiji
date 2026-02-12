from __future__ import annotations

from jiji.core.block import Block
from jiji.core.config import MAX_MEMPOOL_SIZE
from jiji.core.transaction import Coinbase, Post, Endorse, Transfer, Transaction
from jiji.core.validation import (
    ValidationError,
    validate_transaction_format,
    validate_transaction_state,
)

if __import__("typing").TYPE_CHECKING:
    from jiji.core.chain import Blockchain


class Mempool:
    """Unconfirmed transaction pool with validation, priority ordering, and eviction."""

    def __init__(self, chain: Blockchain, max_size: int = MAX_MEMPOOL_SIZE):
        self._chain = chain
        self._max_size = max_size
        self._txs: dict[bytes, Transaction] = {}

    @property
    def size(self) -> int:
        return len(self._txs)

    def __contains__(self, tx_hash: bytes) -> bool:
        return tx_hash in self._txs

    def add(self, tx: Transaction) -> bytes:
        """Validate and add a transaction. Returns tx_hash. Raises ValidationError."""
        if isinstance(tx, Coinbase):
            raise ValidationError("coinbase transactions cannot be added to mempool")

        tx_hash = tx.tx_hash()

        # reject duplicates
        if tx_hash in self._txs:
            raise ValidationError("transaction already in mempool")
        if tx_hash in self._chain.tx_index:
            raise ValidationError("transaction already confirmed")

        # validate format (signature, limits, etc.)
        validate_transaction_format(tx)

        # validate against current chain state
        validate_transaction_state(tx, self._chain.state, self._chain.known_posts)

        # evict lowest-fee tx if pool is full
        if len(self._txs) >= self._max_size:
            tx_fee = _get_gas_fee(tx)
            lowest_hash, lowest_fee = self._find_lowest_fee()
            if lowest_fee is not None and tx_fee > lowest_fee:
                del self._txs[lowest_hash]
            else:
                raise ValidationError("mempool full and fee too low for eviction")

        self._txs[tx_hash] = tx
        return tx_hash

    def remove(self, tx_hash: bytes) -> None:
        """Remove a single transaction by hash."""
        self._txs.pop(tx_hash, None)

    def remove_confirmed(self, block: Block) -> None:
        """Remove all transactions that appear in a confirmed block."""
        for tx in block.transactions:
            self._txs.pop(tx.tx_hash(), None)

    def revalidate(self) -> list[bytes]:
        """Purge transactions no longer valid against current state. Returns removed hashes."""
        removed = []
        for tx_hash, tx in list(self._txs.items()):
            try:
                validate_transaction_state(tx, self._chain.state, self._chain.known_posts)
            except ValidationError:
                del self._txs[tx_hash]
                removed.append(tx_hash)
        return removed

    def get_by_hash(self, tx_hash: bytes) -> Transaction | None:
        """Look up a pending transaction by hash."""
        return self._txs.get(tx_hash)

    def get_pending(self, limit: int | None = None) -> list[Transaction]:
        """Return transactions sorted by gas_fee descending (miner priority)."""
        txs = sorted(self._txs.values(), key=_get_gas_fee, reverse=True)
        if limit is not None:
            txs = txs[:limit]
        return txs

    def _find_lowest_fee(self) -> tuple[bytes | None, int | None]:
        """Find the transaction with the lowest gas fee."""
        lowest_hash = None
        lowest_fee = None
        for tx_hash, tx in self._txs.items():
            fee = _get_gas_fee(tx)
            if lowest_fee is None or fee < lowest_fee:
                lowest_hash = tx_hash
                lowest_fee = fee
        return lowest_hash, lowest_fee


def _get_gas_fee(tx: Transaction) -> int:
    """Extract gas_fee from a transaction."""
    if isinstance(tx, (Post, Endorse, Transfer)):
        return tx.gas_fee
    return 0
