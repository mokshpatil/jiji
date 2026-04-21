"""SQLite-backed block storage."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from jiji.core.block import Block


class BlockStore:
    """SQLite-backed block storage with support for forks."""

    SCHEMA_VERSION = "1"

    def __init__(self, db_path: str | Path):
        """Open or create the database. Use ':memory:' for tests."""
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self) -> None:
        """Create tables and indexes if they don't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                block_hash BLOB PRIMARY KEY,
                height INTEGER NOT NULL,
                prev_hash BLOB NOT NULL,
                data TEXT NOT NULL,
                on_main_chain INTEGER NOT NULL DEFAULT 0
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocks_height ON blocks(height)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocks_prev_hash ON blocks(prev_hash)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocks_main ON blocks(on_main_chain, height)"
        )
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self._conn.commit()

        # Set schema version if not present
        cur = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        if cur.fetchone() is None:
            self._set_meta("schema_version", self.SCHEMA_VERSION)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def put_block(self, block: Block, on_main_chain: bool = True) -> None:
        """Store a block. Updates if already exists."""
        block_hash = block.block_hash()
        data_json = json.dumps(block.to_dict(), separators=(",", ":"))
        self._conn.execute(
            "INSERT OR REPLACE INTO blocks (block_hash, height, prev_hash, data, on_main_chain) "
            "VALUES (?, ?, ?, ?, ?)",
            (block_hash, block.header.height, block.header.prev_hash, data_json, int(on_main_chain)),
        )
        if on_main_chain:
            self._set_meta("tip_hash", block_hash.hex())
        self._conn.commit()

    def get_block(self, block_hash: bytes) -> Block | None:
        """Retrieve a block by hash."""
        cur = self._conn.execute("SELECT data FROM blocks WHERE block_hash = ?", (block_hash,))
        row = cur.fetchone()
        if row is None:
            return None
        return Block.from_dict(json.loads(row[0]))

    def has_block(self, block_hash: bytes) -> bool:
        """Check if a block exists."""
        cur = self._conn.execute("SELECT 1 FROM blocks WHERE block_hash = ?", (block_hash,))
        return cur.fetchone() is not None

    def block_count(self) -> int:
        """Total number of blocks stored."""
        cur = self._conn.execute("SELECT COUNT(*) FROM blocks")
        return cur.fetchone()[0]

    def get_blocks_at_height(self, height: int) -> list[Block]:
        """Get all blocks at a given height (main + forks)."""
        cur = self._conn.execute("SELECT data FROM blocks WHERE height = ?", (height,))
        return [Block.from_dict(json.loads(row[0])) for row in cur.fetchall()]

    def get_main_chain_block_at_height(self, height: int) -> Block | None:
        """Get the main chain block at a given height."""
        cur = self._conn.execute(
            "SELECT data FROM blocks WHERE height = ? AND on_main_chain = 1", (height,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return Block.from_dict(json.loads(row[0]))

    def get_children(self, block_hash: bytes) -> list[Block]:
        """Get all blocks whose prev_hash equals the given hash."""
        cur = self._conn.execute("SELECT data FROM blocks WHERE prev_hash = ?", (block_hash,))
        return [Block.from_dict(json.loads(row[0])) for row in cur.fetchall()]

    def get_tip_hash(self) -> bytes | None:
        """Get the main chain tip block hash."""
        cur = self._conn.execute("SELECT value FROM meta WHERE key = 'tip_hash'")
        row = cur.fetchone()
        if row is None:
            return None
        return bytes.fromhex(row[0])

    def get_main_chain_hashes(self) -> list[bytes]:
        """Return ordered list of main chain block hashes (genesis to tip)."""
        tip_hash = self.get_tip_hash()
        if tip_hash is None:
            return []

        # Walk backwards from tip collecting hashes
        hashes = []
        current_hash = tip_hash
        while current_hash != bytes(32):  # genesis prev_hash is all zeros
            hashes.append(current_hash)
            cur = self._conn.execute(
                "SELECT prev_hash FROM blocks WHERE block_hash = ? AND on_main_chain = 1",
                (current_hash,)
            )
            row = cur.fetchone()
            if row is None:
                break
            current_hash = row[0]

        hashes.reverse()  # return in ascending order
        return hashes

    def set_main_chain(self, block_hashes: list[bytes]) -> None:
        """Atomically update main chain flags. Clears all flags, then sets for given hashes."""
        self._conn.execute("BEGIN TRANSACTION")
        try:
            # Clear all main chain flags
            self._conn.execute("UPDATE blocks SET on_main_chain = 0")

            # Set flags for new main chain
            for bh in block_hashes:
                self._conn.execute(
                    "UPDATE blocks SET on_main_chain = 1 WHERE block_hash = ?", (bh,)
                )

            # Update tip
            if block_hashes:
                self._set_meta("tip_hash", block_hashes[-1].hex())

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def get_chain_from_to(self, from_hash: bytes, to_hash: bytes) -> list[Block]:
        """Walk backwards from to_hash to from_hash (exclusive).
        Returns blocks in ascending height order.
        """
        blocks = []
        current_hash = to_hash

        while current_hash != from_hash:
            block = self.get_block(current_hash)
            if block is None:
                raise ValueError(f"missing block in chain: {current_hash.hex()}")
            blocks.append(block)
            current_hash = block.header.prev_hash

        blocks.reverse()  # ascending order
        return blocks

    def _set_meta(self, key: str, value: str) -> None:
        """Set a metadata key-value pair."""
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
