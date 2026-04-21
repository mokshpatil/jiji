from __future__ import annotations

import asyncio
import logging
import os
import time

from jiji.core.block import Block
from jiji.core.chain import Blockchain
from jiji.core.config import DEFAULT_P2P_PORT, DEFAULT_RPC_PORT, MAX_REORG_DEPTH
from jiji.core.transaction import Coinbase, transaction_from_dict
from jiji.core.validation import ValidationError, validate_block_structure
from jiji.mining.mempool import Mempool
from jiji.mining.miner import Miner
from jiji.net.peer import PeerConnection
from jiji.net.server import P2PServer
from jiji.rpc.server import RPCServer
from jiji.storage.store import BlockStore

try:
    from jiji.net.discovery import LANDiscovery
    _MDNS_AVAILABLE = True
except ImportError:  # zeroconf is an optional runtime dep
    LANDiscovery = None  # type: ignore[assignment]
    _MDNS_AVAILABLE = False

logger = logging.getLogger(__name__)


class Node:
    """Orchestrates chain, mempool, miner, P2P server, and RPC server."""

    def __init__(
        self,
        private_key: bytes,
        public_key: bytes,
        data_dir: str | None = None,
        p2p_host: str = "0.0.0.0",
        p2p_port: int = DEFAULT_P2P_PORT,
        rpc_host: str = "127.0.0.1",
        rpc_port: int = DEFAULT_RPC_PORT,
        mine: bool = False,
        bootstrap_peers: list[tuple[str, int]] | None = None,
        rpc_auth_token: str | None = None,
        rpc_allow_origin: str | None = None,
        mdns: bool = False,
        rate_limit: bool = True,
        trusted_cidrs: tuple[str, ...] = (),
    ):
        self.private_key = private_key
        self.public_key = public_key

        # Set up persistent store if data_dir is given
        self._store: BlockStore | None = None
        if data_dir is not None:
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(data_dir, "blocks.db")
            self._store = BlockStore(db_path)

        self.chain = Blockchain(store=self._store)
        self.mempool = Mempool(self.chain)
        self.miner = Miner(self.chain, self.mempool, self.public_key)
        self.p2p = P2PServer(
            self, p2p_host, p2p_port, data_dir=data_dir,
            rate_limit=rate_limit, trusted_cidrs=trusted_cidrs,
        )
        self.rpc = RPCServer(
            self, rpc_host, rpc_port,
            auth_token=rpc_auth_token,
            allow_origin=rpc_allow_origin,
            rate_limit=rate_limit,
            trusted_cidrs=trusted_cidrs,
        )
        self._mine = mine
        self._bootstrap_peers = bootstrap_peers or []
        self._mining_task: asyncio.Task | None = None
        self._running = False
        self._mdns_enabled = mdns and _MDNS_AVAILABLE
        if mdns and not _MDNS_AVAILABLE:
            logger.warning("mDNS requested but `zeroconf` is not installed — skipping")
        self._discovery: LANDiscovery | None = None

    async def start(self, genesis_block: Block | None = None) -> None:
        """Initialize chain and start servers."""
        # Load from persistent store if it already has data
        if self._store is not None and self._store.get_tip_hash() is not None:
            self.chain.load_from_store()
            logger.info(f"loaded chain from disk, height={self.chain.height}")
        elif genesis_block is not None:
            self.chain._apply_block(genesis_block)
        elif self._bootstrap_peers:
            # Will sync genesis from peers — don't create our own
            logger.info("waiting to sync genesis from peers")
        else:
            self.chain.initialize_genesis(self.public_key)
        logger.info(f"chain initialized, height={self.chain.height}")

        await self.p2p.start()
        await self.rpc.start()

        # Load saved peers from disk
        self.p2p.load_peers()

        for host, port in self._bootstrap_peers:
            asyncio.create_task(self.p2p.connect_to_peer(host, port))

        # Also try connecting to previously known peers (sorted by freshness)
        saved = sorted(self.p2p.known_addresses.items(), key=lambda x: x[1], reverse=True)
        for addr, _ in saved:
            if addr not in [(h, p) for h, p in self._bootstrap_peers]:
                asyncio.create_task(self.p2p.connect_to_peer(*addr))

        asyncio.create_task(self.p2p.peer_exchange_loop())

        if self._mine:
            self._mining_task = asyncio.create_task(self._mining_loop())

        if self._mdns_enabled and self.chain.tip is not None:
            genesis_hash = self.chain.get_block_by_height(0).block_hash()
            self._discovery = LANDiscovery(self.p2p, genesis_hash)
            try:
                await self._discovery.start()
            except Exception as e:
                logger.warning(f"mDNS discovery failed to start: {e}")
                self._discovery = None

        self._running = True
        logger.info("node started")

    async def stop(self) -> None:
        """Stop all services."""
        self._running = False
        if self._mining_task:
            self._mining_task.cancel()
            try:
                await self._mining_task
            except asyncio.CancelledError:
                pass
        if self._discovery is not None:
            try:
                await self._discovery.stop()
            except Exception as e:
                logger.debug(f"mDNS shutdown error: {e}")
        self.p2p.save_peers()
        await self.p2p.stop()
        await self.rpc.stop()
        if self._store is not None:
            self._store.close()
        logger.info("node stopped")

    # -- Event handlers --

    async def handle_new_transaction(
        self, tx_dict: dict, source_peer: PeerConnection | None = None,
    ) -> str:
        """Validate, add to mempool, gossip. Returns tx_hash hex."""
        tx = transaction_from_dict(tx_dict)
        tx_hash = self.mempool.add(tx)
        tx_hash_hex = tx_hash.hex()
        logger.info(f"new tx {tx_hash_hex[:16]}...")
        await self.p2p.broadcast_tx(tx_hash_hex, exclude=source_peer)
        return tx_hash_hex

    async def handle_new_block(
        self, block_dict: dict, source_peer: PeerConnection | None = None,
    ) -> None:
        """Validate, add to chain, handle forks, update mempool, gossip."""
        block = Block.from_dict(block_dict)
        block_hash = block.block_hash()
        block_hash_hex = block_hash.hex()

        # Already have this block
        if self.chain.get_block_by_hash(block_hash) is not None:
            return

        tip = self.chain.tip

        # Case 1: Extends current tip
        if (tip is not None and
                block.header.height == tip.header.height + 1 and
                block.header.prev_hash == tip.block_hash()):
            try:
                self.chain.add_block(block)
            except ValidationError as e:
                logger.warning(f"rejected block {block_hash_hex[:16]}: {e}")
                return

            logger.info(f"accepted block {block_hash_hex[:16]}... height={block.header.height}")
            self.mempool.remove_confirmed(block)
            self.mempool.revalidate()
            self.p2p.mark_sync_done()
            await self.p2p.broadcast_block(block_hash_hex, block.header.height, exclude=source_peer)
            return

        # Case 1b: Chain is empty (genesis)
        if tip is None:
            try:
                self.chain.add_block(block)
            except ValidationError as e:
                logger.warning(f"rejected genesis {block_hash_hex[:16]}: {e}")
                return
            logger.info(f"accepted genesis {block_hash_hex[:16]}")
            await self.p2p.broadcast_block(block_hash_hex, block.header.height, exclude=source_peer)
            return

        # Case 2: Known parent but not extending tip (fork candidate)
        parent = self.chain.get_block_by_hash(block.header.prev_hash)
        if parent is not None:
            try:
                validate_block_structure(block, parent, int(time.time()))
            except ValidationError as e:
                logger.debug(f"rejected fork block {block_hash_hex[:16]}: {e}")
                return

            self.chain.store_fork_block(block)
            logger.info(f"stored fork block {block_hash_hex[:16]}... height={block.header.height}")

            # Check if this fork is now longer than main chain
            fork_length = self.chain.get_chain_length_from(block_hash)
            main_length = len(self.chain.main_chain)

            if fork_length > main_length:
                logger.info(
                    f"fork (length={fork_length}) > main (length={main_length}), attempting reorg"
                )
                try:
                    orphaned = self.chain.reorganize(block_hash)
                    self._recycle_orphaned_transactions(orphaned)
                    self.mempool.revalidate()
                    logger.info(
                        f"reorg complete, height={self.chain.height}, "
                        f"orphaned {len(orphaned)} blocks"
                    )
                    await self.p2p.broadcast_block(block_hash_hex, block.header.height, exclude=source_peer)
                except (ValidationError, ValueError) as e:
                    logger.warning(f"reorg failed: {e}")
            return

        # Case 3: Unknown parent — request sync
        logger.debug(
            f"block {block_hash_hex[:16]} has unknown parent, height={block.header.height}"
        )
        if source_peer is not None and not self.p2p._syncing:
            asyncio.create_task(self.p2p._start_sync(source_peer))

    def _recycle_orphaned_transactions(self, orphaned_blocks: list[Block]) -> None:
        """Return non-coinbase transactions from orphaned blocks to the mempool."""
        for block in orphaned_blocks:
            for tx in block.transactions:
                if isinstance(tx, Coinbase):
                    continue
                tx_hash = tx.tx_hash()
                if tx_hash in self.chain.tx_index:
                    continue  # already confirmed on new chain
                if tx_hash in self.mempool:
                    continue  # already pending
                try:
                    self.mempool.add(tx)
                    logger.debug(f"recycled orphaned tx {tx_hash.hex()[:16]}")
                except ValidationError:
                    pass  # no longer valid against new state

    # -- Mining --

    async def _mining_loop(self) -> None:
        logger.info("mining started")
        while self._running:
            try:
                # wait for chain to be initialized
                while self._running and self.chain.tip is None:
                    await asyncio.sleep(1)
                if not self._running:
                    break

                has_txs = self.mempool.size > 0
                template = self.miner.create_block_template()
                block = await self._async_mine(template)
                if block is None:
                    continue
                # chain may have advanced while mining
                if block.header.height != self.chain.height + 1:
                    continue
                self.chain.add_block(block)
                self.mempool.remove_confirmed(block)
                self.mempool.revalidate()
                bh = block.block_hash().hex()
                logger.info(f"mined block {bh[:16]}... height={block.header.height}")
                await self.p2p.broadcast_block(bh, block.header.height)
                # After mining an empty block, wait before mining the next one
                # to avoid flooding the chain at low difficulty
                if not has_txs:
                    await asyncio.sleep(5)
            except ValidationError as e:
                logger.warning(f"mined block rejected: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"mining error: {e}")
                await asyncio.sleep(1)

    async def _async_mine(self, template: Block, chunk_size: int = 1000) -> Block | None:
        """Mine in chunks, yielding to event loop between iterations."""
        while self._running:
            result = self.miner.mine_block(template, max_iterations=chunk_size)
            if result is not None:
                return result
            await asyncio.sleep(0)
        return None
