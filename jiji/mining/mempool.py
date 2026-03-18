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
    """Unconfirmed transaction pool with validation, priority ordering, and eviction.

    Supports out-of-order nonce arrival: transactions with future nonces are
    held in a queue and promoted automatically when earlier nonces fill in.
    """

    def __init__(self, chain: Blockchain, max_size: int = MAX_MEMPOOL_SIZE):
        self._chain = chain
        self._max_size = max_size
        self._txs: dict[bytes, Transaction] = {}
        self._pending_nonces: dict[bytes, int] = {}  # pubkey -> next expected nonce
        # queued txs waiting for earlier nonces: pubkey -> {nonce: tx}
        self._queued: dict[bytes, dict[int, Transaction]] = {}

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
        # also check queued
        pubkey = _get_sender(tx)
        tx_nonce = _get_nonce(tx)
        if pubkey is not None and pubkey in self._queued:
            if tx_nonce in self._queued[pubkey]:
                raise ValidationError("transaction already queued")

        # validate format (signature, limits, etc.)
        validate_transaction_format(tx)

        # determine expected nonce for this sender
        expected = self._expected_nonce(pubkey)

        if pubkey is not None and tx_nonce > expected:
            # future nonce — queue it for later promotion
            if pubkey not in self._queued:
                self._queued[pubkey] = {}
            self._queued[pubkey][tx_nonce] = tx
            return tx_hash

        if pubkey is not None and tx_nonce < expected:
            raise ValidationError(
                f"nonce too low: tx={tx_nonce}, expected={expected}"
            )

        # nonce matches — validate against chain state or accept as sequential pending
        if pubkey is not None and pubkey in self._pending_nonces:
            # skip full state validation (on-chain nonce would mismatch)
            pass
        else:
            validate_transaction_state(tx, self._chain.state, self._chain.known_posts)

        self._insert(tx, pubkey)

        # promote any queued txs that now have sequential nonces
        if pubkey is not None:
            self._promote_queued(pubkey)

        return tx_hash

    def _insert(self, tx: Transaction, pubkey: bytes | None) -> None:
        """Insert a tx into the active pool and update nonce tracking."""
        # evict lowest-fee tx if pool is full
        if len(self._txs) >= self._max_size:
            tx_fee = _get_gas_fee(tx)
            lowest_hash, lowest_fee = self._find_lowest_fee()
            if lowest_fee is not None and tx_fee > lowest_fee:
                del self._txs[lowest_hash]
            else:
                raise ValidationError("mempool full and fee too low for eviction")

        self._txs[tx.tx_hash()] = tx
        if pubkey is not None:
            self._pending_nonces[pubkey] = _get_nonce(tx) + 1

    def _promote_queued(self, pubkey: bytes) -> None:
        """Move queued txs into the active pool if their nonce is now expected."""
        if pubkey not in self._queued:
            return
        queue = self._queued[pubkey]
        while True:
            expected = self._expected_nonce(pubkey)
            if expected not in queue:
                break
            tx = queue.pop(expected)
            self._insert(tx, pubkey)
        if not queue:
            del self._queued[pubkey]

    def _expected_nonce(self, pubkey: bytes | None) -> int:
        """Return the next expected nonce for a sender."""
        if pubkey is None:
            return 0
        if pubkey in self._pending_nonces:
            return self._pending_nonces[pubkey]
        account = self._chain.state.get_account(pubkey)
        return account.nonce if account else 0

    def remove(self, tx_hash: bytes) -> None:
        """Remove a single transaction by hash."""
        self._txs.pop(tx_hash, None)

    def remove_confirmed(self, block: Block) -> None:
        """Remove all transactions that appear in a confirmed block."""
        for tx in block.transactions:
            self._txs.pop(tx.tx_hash(), None)
        self._rebuild_pending_nonces()

    def revalidate(self) -> list[bytes]:
        """Purge transactions no longer valid against current state. Returns removed hashes."""
        removed = []
        for tx_hash, tx in list(self._txs.items()):
            try:
                validate_transaction_state(tx, self._chain.state, self._chain.known_posts)
            except ValidationError:
                del self._txs[tx_hash]
                removed.append(tx_hash)
        self._rebuild_pending_nonces()
        # try to promote queued txs (chain state may have advanced)
        for pubkey in list(self._queued.keys()):
            self._promote_queued(pubkey)
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

    def next_nonce(self, pubkey: bytes) -> int:
        """Return the next nonce to use for a given account (accounting for pending + queued txs)."""
        nonce = self._expected_nonce(pubkey)
        # also account for queued future nonces
        if pubkey in self._queued:
            for queued_nonce in self._queued[pubkey]:
                if queued_nonce >= nonce:
                    nonce = queued_nonce + 1
        return nonce

    def _rebuild_pending_nonces(self) -> None:
        """Rebuild pending nonce tracker from remaining pool transactions."""
        self._pending_nonces.clear()
        for tx in self._txs.values():
            pubkey = _get_sender(tx)
            if pubkey is None:
                continue
            nonce = _get_nonce(tx)
            next_nonce = nonce + 1
            if pubkey not in self._pending_nonces or next_nonce > self._pending_nonces[pubkey]:
                self._pending_nonces[pubkey] = next_nonce

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


def _get_sender(tx: Transaction) -> bytes | None:
    """Extract the sender/author public key from a transaction."""
    if isinstance(tx, (Post, Endorse)):
        return tx.author
    if isinstance(tx, Transfer):
        return tx.sender
    return None


def _get_nonce(tx: Transaction) -> int:
    """Extract nonce from a transaction."""
    if isinstance(tx, (Post, Endorse, Transfer)):
        return tx.nonce
    return 0
