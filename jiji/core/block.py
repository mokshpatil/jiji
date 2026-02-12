from __future__ import annotations

from dataclasses import dataclass

from jiji.core.config import MAX_TARGET
from jiji.core.merkle import merkle_root
from jiji.core.serialization import canonicalize, sha256
from jiji.core.transaction import Transaction, transaction_from_dict


@dataclass
class BlockHeader:
    """Block header containing metadata and proof of work fields."""

    version: int
    height: int
    prev_hash: bytes
    timestamp: int
    miner: bytes
    difficulty: int
    nonce: int
    tx_merkle_root: bytes
    state_root: bytes
    tx_count: int

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "height": self.height,
            "prev_hash": self.prev_hash.hex(),
            "timestamp": self.timestamp,
            "miner": self.miner.hex(),
            "difficulty": self.difficulty,
            "nonce": self.nonce,
            "tx_merkle_root": self.tx_merkle_root.hex(),
            "state_root": self.state_root.hex(),
            "tx_count": self.tx_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> BlockHeader:
        return cls(
            version=d["version"],
            height=d["height"],
            prev_hash=bytes.fromhex(d["prev_hash"]),
            timestamp=d["timestamp"],
            miner=bytes.fromhex(d["miner"]),
            difficulty=d["difficulty"],
            nonce=d["nonce"],
            tx_merkle_root=bytes.fromhex(d["tx_merkle_root"]),
            state_root=bytes.fromhex(d["state_root"]),
            tx_count=d["tx_count"],
        )

    def block_hash(self) -> bytes:
        """SHA-256 of the canonical header serialization."""
        return sha256(canonicalize(self.to_dict()))


@dataclass
class Block:
    """A complete block with header and transaction body."""

    header: BlockHeader
    transactions: list[Transaction]

    def block_hash(self) -> bytes:
        return self.header.block_hash()

    def compute_tx_merkle_root(self) -> bytes:
        """Compute Merkle root from the block's transactions."""
        tx_hashes = [tx.tx_hash() for tx in self.transactions]
        return merkle_root(tx_hashes)

    def serialized_size(self) -> int:
        """Approximate serialized size in bytes."""
        return len(canonicalize(self.to_dict()))

    def meets_difficulty(self) -> bool:
        """Check if the block hash satisfies the difficulty target."""
        hash_int = int.from_bytes(self.block_hash(), "big")
        target = MAX_TARGET // self.header.difficulty
        return hash_int <= target

    def to_dict(self) -> dict:
        return {
            "header": self.header.to_dict(),
            "transactions": [tx.to_dict() for tx in self.transactions],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Block:
        header = BlockHeader.from_dict(d["header"])
        transactions = [transaction_from_dict(tx) for tx in d["transactions"]]
        return cls(header=header, transactions=transactions)
