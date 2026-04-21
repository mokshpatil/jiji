from __future__ import annotations

import copy
from dataclasses import dataclass

from jiji.core.merkle import merkle_root
from jiji.core.serialization import canonicalize, sha256
from jiji.core.transaction import Coinbase, Endorse, Post, Transfer, Transaction


@dataclass
class Account:
    """An account in the world state."""

    balance: int = 0
    nonce: int = 0


class WorldState:
    """Tracks all account balances and nonces. Supports state transitions.

    Maintains a per-pubkey leaf hash cache so `state_root()` only re-hashes
    accounts that have actually changed since the last call. The tree itself
    is re-assembled from the cached leaves on each call (sorted-pubkey
    ordering means we can't reuse tree internals when the key set shifts).
    """

    def __init__(self):
        self.accounts: dict[bytes, Account] = {}
        # pubkey -> sha256(canonicalized leaf)
        self._leaf_cache: dict[bytes, bytes] = {}
        self._dirty: set[bytes] = set()

    def _invalidate(self, pubkey: bytes) -> None:
        self._dirty.add(pubkey)
        self._leaf_cache.pop(pubkey, None)

    def get_account(self, pubkey: bytes) -> Account | None:
        """Get an account, or None if it doesn't exist."""
        return self.accounts.get(pubkey)

    def get_or_create(self, pubkey: bytes) -> Account:
        """Get an existing account or create one with zero balance."""
        if pubkey not in self.accounts:
            self.accounts[pubkey] = Account()
            self._invalidate(pubkey)
        return self.accounts[pubkey]

    def apply_transaction(
        self, tx: Transaction, miner: bytes, target_author: bytes | None = None
    ) -> None:
        """Apply a single transaction to the state. Assumes already validated."""
        if isinstance(tx, Coinbase):
            self._apply_coinbase(tx)
        elif isinstance(tx, Post):
            self._apply_post(tx, miner)
        elif isinstance(tx, Endorse):
            self._apply_endorse(tx, miner, target_author)
        elif isinstance(tx, Transfer):
            self._apply_transfer(tx, miner)
        else:
            raise ValueError(f"unknown transaction type: {type(tx)}")

    def _apply_coinbase(self, tx: Coinbase) -> None:
        account = self.get_or_create(tx.recipient)
        account.balance += tx.amount
        self._invalidate(tx.recipient)

    def _apply_post(self, tx: Post, miner: bytes) -> None:
        author = self.get_or_create(tx.author)
        author.balance -= tx.gas_fee
        author.nonce += 1
        miner_acct = self.get_or_create(miner)
        miner_acct.balance += tx.gas_fee
        self._invalidate(tx.author)
        self._invalidate(miner)

    def _apply_endorse(
        self, tx: Endorse, miner: bytes, target_author: bytes | None
    ) -> None:
        author = self.get_or_create(tx.author)
        author.balance -= tx.gas_fee + tx.amount
        author.nonce += 1
        miner_acct = self.get_or_create(miner)
        miner_acct.balance += tx.gas_fee
        self._invalidate(tx.author)
        self._invalidate(miner)
        if tx.amount > 0 and target_author is not None:
            recipient = self.get_or_create(target_author)
            recipient.balance += tx.amount
            self._invalidate(target_author)

    def _apply_transfer(self, tx: Transfer, miner: bytes) -> None:
        sender = self.get_or_create(tx.sender)
        sender.balance -= tx.amount + tx.gas_fee
        sender.nonce += 1
        recipient = self.get_or_create(tx.recipient)
        recipient.balance += tx.amount
        miner_acct = self.get_or_create(miner)
        miner_acct.balance += tx.gas_fee
        self._invalidate(tx.sender)
        self._invalidate(tx.recipient)
        self._invalidate(miner)

    def _leaf_hash(self, pubkey: bytes) -> bytes:
        cached = self._leaf_cache.get(pubkey)
        if cached is not None:
            return cached
        account = self.accounts[pubkey]
        leaf_data = canonicalize({
            "pubkey": pubkey.hex(),
            "balance": account.balance,
            "nonce": account.nonce,
        })
        h = sha256(leaf_data)
        self._leaf_cache[pubkey] = h
        return h

    def state_root(self) -> bytes:
        """Compute Merkle root of the world state (using cached leaf hashes)."""
        if not self.accounts:
            self._dirty.clear()
            return sha256(b"")
        leaf_hashes = [self._leaf_hash(pk) for pk in sorted(self.accounts.keys())]
        self._dirty.clear()
        return merkle_root(leaf_hashes)

    def copy(self) -> WorldState:
        """Create a deep copy of this state."""
        new_state = WorldState()
        new_state.accounts = copy.deepcopy(self.accounts)
        new_state._leaf_cache = dict(self._leaf_cache)
        new_state._dirty = set(self._dirty)
        return new_state
