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
    """Tracks all account balances and nonces. Supports state transitions."""

    def __init__(self):
        self.accounts: dict[bytes, Account] = {}

    def get_account(self, pubkey: bytes) -> Account | None:
        """Get an account, or None if it doesn't exist."""
        return self.accounts.get(pubkey)

    def get_or_create(self, pubkey: bytes) -> Account:
        """Get an existing account or create one with zero balance."""
        if pubkey not in self.accounts:
            self.accounts[pubkey] = Account()
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

    def _apply_post(self, tx: Post, miner: bytes) -> None:
        author = self.get_or_create(tx.author)
        author.balance -= tx.gas_fee
        author.nonce += 1
        miner_acct = self.get_or_create(miner)
        miner_acct.balance += tx.gas_fee

    def _apply_endorse(
        self, tx: Endorse, miner: bytes, target_author: bytes | None
    ) -> None:
        author = self.get_or_create(tx.author)
        author.balance -= tx.gas_fee + tx.amount
        author.nonce += 1
        miner_acct = self.get_or_create(miner)
        miner_acct.balance += tx.gas_fee
        if tx.amount > 0 and target_author is not None:
            recipient = self.get_or_create(target_author)
            recipient.balance += tx.amount

    def _apply_transfer(self, tx: Transfer, miner: bytes) -> None:
        sender = self.get_or_create(tx.sender)
        sender.balance -= tx.amount + tx.gas_fee
        sender.nonce += 1
        recipient = self.get_or_create(tx.recipient)
        recipient.balance += tx.amount
        miner_acct = self.get_or_create(miner)
        miner_acct.balance += tx.gas_fee

    def state_root(self) -> bytes:
        """Compute Merkle root of the world state."""
        if not self.accounts:
            return sha256(b"")
        leaf_hashes = []
        for pubkey in sorted(self.accounts.keys()):
            account = self.accounts[pubkey]
            leaf_data = canonicalize({
                "pubkey": pubkey.hex(),
                "balance": account.balance,
                "nonce": account.nonce,
            })
            leaf_hashes.append(sha256(leaf_data))
        return merkle_root(leaf_hashes)

    def copy(self) -> WorldState:
        """Create a deep copy of this state."""
        new_state = WorldState()
        new_state.accounts = copy.deepcopy(self.accounts)
        return new_state
