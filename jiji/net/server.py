from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from jiji.core.config import (
    DEFAULT_P2P_PORT,
    HANDSHAKE_TIMEOUT,
    MAX_PEERS,
    PEER_EXCHANGE_INTERVAL,
    PROTOCOL_VERSION,
    SYNC_BATCH_SIZE,
)
from jiji.net.peer import PeerConnection
from jiji.net.protocol import (
    MessageType,
    Message,
    make_block_announce,
    make_block_request,
    make_block_response,
    make_handshake,
    make_peers_request,
    make_peers_response,
    make_sync_request,
    make_sync_response,
    make_tx_announce,
    make_tx_request,
    make_tx_response,
)

if TYPE_CHECKING:
    from jiji.node import Node

logger = logging.getLogger(__name__)


class P2PServer:
    """Manages peer connections, gossip, and chain sync."""

    def __init__(self, node: Node, host: str = "0.0.0.0", port: int = DEFAULT_P2P_PORT):
        self.node = node
        self.host = host
        self.port = port
        self.peers: dict[tuple[str, int], PeerConnection] = {}
        self.known_addresses: set[tuple[str, int]] = set()
        self._server: asyncio.Server | None = None
        self._syncing = False
        self._seen_tx_hashes: set[str] = set()
        self._seen_block_hashes: set[str] = set()

    # -- Lifecycle --

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_inbound, self.host, self.port,
        )
        logger.info(f"P2P server listening on {self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for peer in list(self.peers.values()):
            await peer.close()
        self.peers.clear()

    # -- Connection management --

    async def connect_to_peer(self, host: str, port: int) -> bool:
        if (host, port) in self.peers:
            return True
        if len(self.peers) >= MAX_PEERS:
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=HANDSHAKE_TIMEOUT,
            )
            peer = PeerConnection(reader, writer, host, port, inbound=False)
            await self._perform_handshake(peer)
            if peer.handshake_done:
                self.peers[peer.address] = peer
                asyncio.create_task(self._peer_loop(peer))
                logger.info(f"connected to peer {host}:{port}")
                return True
            else:
                await peer.close()
                return False
        except (OSError, asyncio.TimeoutError) as e:
            logger.debug(f"failed to connect to {host}:{port}: {e}")
            return False

    async def _handle_inbound(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        addr = writer.get_extra_info("peername")
        host, port = addr[0], addr[1]
        if len(self.peers) >= MAX_PEERS:
            writer.close()
            return
        peer = PeerConnection(reader, writer, host, port, inbound=True)
        try:
            msg = await asyncio.wait_for(peer.receive(), timeout=HANDSHAKE_TIMEOUT)
            if msg is None or msg.msg_type != MessageType.HANDSHAKE:
                await peer.close()
                return
            self._process_handshake(peer, msg)
            await self._send_handshake(peer)
            peer.handshake_done = True
            self.peers[peer.address] = peer
            logger.info(f"inbound peer connected: {host}:{port}")
            asyncio.create_task(self._peer_loop(peer))
        except asyncio.TimeoutError:
            await peer.close()

    # -- Handshake --

    async def _perform_handshake(self, peer: PeerConnection) -> None:
        await self._send_handshake(peer)
        msg = await asyncio.wait_for(peer.receive(), timeout=HANDSHAKE_TIMEOUT)
        if msg is None or msg.msg_type != MessageType.HANDSHAKE:
            return
        self._process_handshake(peer, msg)
        # verify genesis match
        our_genesis = self.node.chain.get_block_by_height(0)
        if our_genesis and peer.genesis_hash != our_genesis.block_hash().hex():
            logger.warning(f"genesis mismatch with {peer.address}")
            return
        peer.handshake_done = True

    async def _send_handshake(self, peer: PeerConnection) -> None:
        genesis = self.node.chain.get_block_by_height(0)
        genesis_hash = genesis.block_hash().hex() if genesis else ""
        msg = make_handshake(PROTOCOL_VERSION, self.node.chain.height, genesis_hash)
        await peer.send(msg)

    def _process_handshake(self, peer: PeerConnection, msg: Message) -> None:
        peer.version = msg.payload.get("version")
        peer.peer_height = msg.payload.get("height", -1)
        peer.genesis_hash = msg.payload.get("genesis_hash")

    # -- Message loop --

    async def _peer_loop(self, peer: PeerConnection) -> None:
        try:
            # if peer is ahead, sync
            if peer.peer_height > self.node.chain.height:
                await self._start_sync(peer)
            while not peer.is_closed:
                msg = await peer.receive()
                if msg is None:
                    break
                await self._handle_message(peer, msg)
        finally:
            self.peers.pop(peer.address, None)
            await peer.close()

    async def _handle_message(self, peer: PeerConnection, msg: Message) -> None:
        handlers = {
            MessageType.PEERS_REQUEST: self._on_peers_request,
            MessageType.PEERS_RESPONSE: self._on_peers_response,
            MessageType.TX_ANNOUNCE: self._on_tx_announce,
            MessageType.TX_REQUEST: self._on_tx_request,
            MessageType.TX_RESPONSE: self._on_tx_response,
            MessageType.BLOCK_ANNOUNCE: self._on_block_announce,
            MessageType.BLOCK_REQUEST: self._on_block_request,
            MessageType.BLOCK_RESPONSE: self._on_block_response,
            MessageType.SYNC_REQUEST: self._on_sync_request,
            MessageType.SYNC_RESPONSE: self._on_sync_response,
        }
        handler = handlers.get(msg.msg_type)
        if handler:
            await handler(peer, msg)

    # -- Peers --

    async def _on_peers_request(self, peer: PeerConnection, msg: Message) -> None:
        addrs = [(h, p) for (h, p) in self.peers.keys() if (h, p) != peer.address]
        await peer.send(make_peers_response(addrs))

    async def _on_peers_response(self, peer: PeerConnection, msg: Message) -> None:
        for entry in msg.payload.get("peers", []):
            addr = (entry["host"], entry["port"])
            self.known_addresses.add(addr)

    # -- Transaction gossip --

    async def _on_tx_announce(self, peer: PeerConnection, msg: Message) -> None:
        tx_hash_hex = msg.payload["tx_hash"]
        if tx_hash_hex in self._seen_tx_hashes:
            return
        self._seen_tx_hashes.add(tx_hash_hex)
        tx_hash = bytes.fromhex(tx_hash_hex)
        if tx_hash in self.node.mempool or tx_hash in self.node.chain.tx_index:
            return
        await peer.send(make_tx_request(tx_hash_hex))

    async def _on_tx_request(self, peer: PeerConnection, msg: Message) -> None:
        tx_hash = bytes.fromhex(msg.payload["tx_hash"])
        tx = self.node.mempool.get_by_hash(tx_hash)
        if tx is None:
            tx = self.node.chain.get_transaction(tx_hash)
        tx_dict = tx.to_dict() if tx else None
        await peer.send(make_tx_response(tx_dict))

    async def _on_tx_response(self, peer: PeerConnection, msg: Message) -> None:
        tx_dict = msg.payload.get("transaction")
        if tx_dict is None:
            return
        try:
            await self.node.handle_new_transaction(tx_dict, source_peer=peer)
        except Exception as e:
            logger.debug(f"rejected tx from {peer.address}: {e}")

    # -- Block gossip --

    async def _on_block_announce(self, peer: PeerConnection, msg: Message) -> None:
        block_hash_hex = msg.payload["block_hash"]
        height = msg.payload["height"]
        if block_hash_hex in self._seen_block_hashes:
            return
        self._seen_block_hashes.add(block_hash_hex)
        block_hash = bytes.fromhex(block_hash_hex)
        if self.node.chain.get_block_by_hash(block_hash) is not None:
            return
        if height == self.node.chain.height + 1:
            await peer.send(make_block_request(block_hash=block_hash_hex))
        elif height > self.node.chain.height + 1:
            await self._start_sync(peer)

    async def _on_block_request(self, peer: PeerConnection, msg: Message) -> None:
        block = None
        if "block_hash" in msg.payload:
            bh = bytes.fromhex(msg.payload["block_hash"])
            block = self.node.chain.get_block_by_hash(bh)
        elif "height" in msg.payload:
            block = self.node.chain.get_block_by_height(msg.payload["height"])
        block_dict = block.to_dict() if block else None
        await peer.send(make_block_response(block_dict))

    async def _on_block_response(self, peer: PeerConnection, msg: Message) -> None:
        block_dict = msg.payload.get("block")
        if block_dict is None:
            return
        try:
            await self.node.handle_new_block(block_dict, source_peer=peer)
        except Exception as e:
            logger.debug(f"rejected block from {peer.address}: {e}")

    # -- Sync --

    async def _on_sync_request(self, peer: PeerConnection, msg: Message) -> None:
        start = msg.payload["start_height"]
        end = msg.payload["end_height"]
        end = min(end, start + SYNC_BATCH_SIZE - 1)
        blocks = []
        for h in range(start, end + 1):
            block = self.node.chain.get_block_by_height(h)
            if block is None:
                break
            blocks.append(block.to_dict())
        await peer.send(make_sync_response(blocks))

    async def _on_sync_response(self, peer: PeerConnection, msg: Message) -> None:
        blocks = msg.payload.get("blocks", [])
        for block_dict in blocks:
            try:
                await self.node.handle_new_block(block_dict, source_peer=peer)
            except Exception as e:
                logger.debug(f"sync block rejected: {e}")
                break
        # if we got a full batch and peer has more, continue
        if len(blocks) == SYNC_BATCH_SIZE:
            last_height = blocks[-1]["header"]["height"]
            if last_height < peer.peer_height:
                await peer.send(make_sync_request(
                    last_height + 1, last_height + SYNC_BATCH_SIZE,
                ))
        else:
            self._syncing = False

    async def _start_sync(self, peer: PeerConnection) -> None:
        if self._syncing:
            return
        self._syncing = True
        start = self.node.chain.height + 1
        end = start + SYNC_BATCH_SIZE - 1
        logger.info(f"syncing from {peer.address}, requesting blocks {start}-{end}")
        await peer.send(make_sync_request(start, end))

    def mark_sync_done(self) -> None:
        self._syncing = False

    # -- Broadcasting --

    async def broadcast_tx(self, tx_hash_hex: str, exclude: PeerConnection | None = None) -> None:
        self._seen_tx_hashes.add(tx_hash_hex)
        msg = make_tx_announce(tx_hash_hex)
        for peer in list(self.peers.values()):
            if peer is not exclude and not peer.is_closed:
                await peer.send(msg)

    async def broadcast_block(
        self, block_hash_hex: str, height: int, exclude: PeerConnection | None = None,
    ) -> None:
        self._seen_block_hashes.add(block_hash_hex)
        msg = make_block_announce(block_hash_hex, height)
        for peer in list(self.peers.values()):
            if peer is not exclude and not peer.is_closed:
                await peer.send(msg)

    # -- Peer exchange background task --

    async def peer_exchange_loop(self) -> None:
        while True:
            await asyncio.sleep(PEER_EXCHANGE_INTERVAL)
            for peer in list(self.peers.values()):
                if not peer.is_closed:
                    await peer.send(make_peers_request())
            for addr in list(self.known_addresses):
                if addr not in self.peers and len(self.peers) < MAX_PEERS:
                    asyncio.create_task(self.connect_to_peer(*addr))
