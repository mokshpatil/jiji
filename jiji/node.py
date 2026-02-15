from __future__ import annotations

import asyncio
import logging

from jiji.core.block import Block
from jiji.core.chain import Blockchain
from jiji.core.config import DEFAULT_P2P_PORT, DEFAULT_RPC_PORT
from jiji.core.transaction import transaction_from_dict
from jiji.core.validation import ValidationError
from jiji.mining.mempool import Mempool
from jiji.mining.miner import Miner
from jiji.net.peer import PeerConnection
from jiji.net.server import P2PServer
from jiji.rpc.server import RPCServer

logger = logging.getLogger(__name__)


class Node:
    """Orchestrates chain, mempool, miner, P2P server, and RPC server."""

    def __init__(
        self,
        private_key: bytes,
        public_key: bytes,
        p2p_host: str = "0.0.0.0",
        p2p_port: int = DEFAULT_P2P_PORT,
        rpc_host: str = "127.0.0.1",
        rpc_port: int = DEFAULT_RPC_PORT,
        mine: bool = False,
        bootstrap_peers: list[tuple[str, int]] | None = None,
    ):
        self.private_key = private_key
        self.public_key = public_key
        self.chain = Blockchain()
        self.mempool = Mempool(self.chain)
        self.miner = Miner(self.chain, self.mempool, self.public_key)
        self.p2p = P2PServer(self, p2p_host, p2p_port)
        self.rpc = RPCServer(self, rpc_host, rpc_port)
        self._mine = mine
        self._bootstrap_peers = bootstrap_peers or []
        self._mining_task: asyncio.Task | None = None
        self._running = False

    async def start(self, genesis_block: Block | None = None) -> None:
        """Initialize chain and start servers."""
        if genesis_block is not None:
            self.chain._apply_block(genesis_block)
        else:
            self.chain.initialize_genesis(self.public_key)
        logger.info(f"chain initialized, height={self.chain.height}")

        await self.p2p.start()
        await self.rpc.start()

        for host, port in self._bootstrap_peers:
            asyncio.create_task(self.p2p.connect_to_peer(host, port))

        asyncio.create_task(self.p2p.peer_exchange_loop())

        if self._mine:
            self._mining_task = asyncio.create_task(self._mining_loop())

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
        await self.p2p.stop()
        await self.rpc.stop()
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
        """Validate, add to chain, update mempool, gossip."""
        block = Block.from_dict(block_dict)
        block_hash = block.block_hash()
        block_hash_hex = block_hash.hex()

        # already have it
        if self.chain.get_block_by_hash(block_hash) is not None:
            return

        # must extend tip
        expected_height = self.chain.height + 1
        if block.header.height != expected_height:
            logger.debug(
                f"block height {block.header.height} != expected {expected_height}"
            )
            return

        try:
            self.chain.add_block(block)
        except ValidationError as e:
            logger.warning(f"rejected block {block_hash_hex[:16]}: {e}")
            return

        logger.info(f"accepted block {block_hash_hex[:16]}... height={block.header.height}")

        self.mempool.remove_confirmed(block)
        self.mempool.revalidate()
        self.p2p.mark_sync_done()

        await self.p2p.broadcast_block(
            block_hash_hex, block.header.height, exclude=source_peer,
        )

    # -- Mining --

    async def _mining_loop(self) -> None:
        logger.info("mining started")
        while self._running:
            try:
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
