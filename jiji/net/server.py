from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING

from jiji.core.config import (
    DEFAULT_P2P_PORT,
    HANDSHAKE_TIMEOUT,
    INBOUND_CONN_PER_MIN,
    MAX_INBOUND,
    MAX_OUTBOUND,
    MAX_PEERS,
    MAX_REORG_DEPTH,
    MAX_SAVED_PEERS,
    PEER_EXCHANGE_INITIAL_DELAY,
    PEER_EXCHANGE_INTERVAL,
    PEER_MAX_AGE,
    PROTOCOL_VERSION,
    SEEN_SET_FLUSH_INTERVAL,
    SEEN_SET_MAX,
    SYNC_BATCH_SIZE,
)
from jiji.net.peer import PeerConnection
from jiji.net.scoring import PeerScorer
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
    make_mempool_request,
    make_mempool_response,
    make_tx_announce,
    make_tx_request,
    make_tx_response,
)

if TYPE_CHECKING:
    from jiji.node import Node

logger = logging.getLogger(__name__)


class P2PServer:
    """Manages peer connections, gossip, and chain sync."""

    def __init__(
        self,
        node: Node,
        host: str = "0.0.0.0",
        port: int = DEFAULT_P2P_PORT,
        data_dir: str | None = None,
        rate_limit: bool = True,
        trusted_cidrs: tuple[str, ...] = (),
    ):
        self.node = node
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self.rate_limit = rate_limit
        self.peers: dict[tuple[str, int], PeerConnection] = {}
        # addr -> last_seen timestamp
        self.known_addresses: dict[tuple[str, int], float] = {}
        self._server: asyncio.Server | None = None
        self._syncing = False
        # Use OrderedDicts as LRUs so oldest entries are evicted first.
        self._seen_tx_hashes: OrderedDict[str, None] = OrderedDict()
        self._seen_block_hashes: OrderedDict[str, None] = OrderedDict()
        # Inbound connection timestamps per /32 for sliding-window rate limit.
        self._inbound_attempts: dict[str, deque[float]] = {}
        self.scorer = PeerScorer(
            data_dir=data_dir,
            trusted_cidrs=trusted_cidrs,
            disabled=not rate_limit,
        )
        self._seen_flush_task: asyncio.Task | None = None

    # -- Lifecycle --

    async def start(self) -> None:
        self.load_seen()
        self._server = await asyncio.start_server(
            self._handle_inbound, self.host, self.port,
        )
        if self.data_dir is not None:
            self._seen_flush_task = asyncio.create_task(self._seen_flush_loop())
        logger.info(f"P2P server listening on {self.host}:{self.port}")

    async def stop(self) -> None:
        if self._seen_flush_task is not None:
            self._seen_flush_task.cancel()
            try:
                await self._seen_flush_task
            except asyncio.CancelledError:
                pass
        self.save_seen()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for peer in list(self.peers.values()):
            await peer.close()
        self.peers.clear()

    # -- Seen-set helpers (bounded LRU) --

    def _seen_add(self, store: OrderedDict[str, None], key: str) -> None:
        if key in store:
            store.move_to_end(key)
            return
        store[key] = None
        while len(store) > SEEN_SET_MAX:
            store.popitem(last=False)

    @property
    def _inbound_count(self) -> int:
        return sum(1 for p in self.peers.values() if p.inbound)

    @property
    def _outbound_count(self) -> int:
        return sum(1 for p in self.peers.values() if not p.inbound)

    def _inbound_rate_ok(self, ip: str) -> bool:
        """Per-/32 sliding-window cap on inbound connection opens."""
        if not self.rate_limit or self.scorer.is_trusted(ip):
            return True
        now = time.monotonic()
        window = self._inbound_attempts.setdefault(ip, deque())
        cutoff = now - 60
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= INBOUND_CONN_PER_MIN:
            return False
        window.append(now)
        return True

    # -- Connection management --

    async def connect_to_peer(self, host: str, port: int) -> bool:
        # Check if already connected to this listen address
        for p in self.peers.values():
            if p.host == host and p.listen_port == port:
                return True
        if self.scorer.is_banned(host):
            logger.debug(f"refusing outbound to banned {host}:{port}")
            return False
        if self._outbound_count >= MAX_OUTBOUND:
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=HANDSHAKE_TIMEOUT,
            )
            peer = PeerConnection(
                reader, writer, host, port,
                inbound=False, rate_limit=self.rate_limit,
            )
            await self._perform_handshake(peer)
            if peer.handshake_done:
                self.peers[peer.address] = peer
                self.known_addresses[(host, peer.listen_port)] = time.time()
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
        if self.scorer.is_banned(host):
            logger.debug(f"rejecting inbound from banned {host}")
            writer.close()
            return
        if not self._inbound_rate_ok(host):
            logger.debug(f"inbound rate limit hit for {host}")
            writer.close()
            return
        if self._inbound_count >= MAX_INBOUND:
            writer.close()
            return
        peer = PeerConnection(
            reader, writer, host, port,
            inbound=True, rate_limit=self.rate_limit,
        )
        try:
            msg = await asyncio.wait_for(peer.receive(), timeout=HANDSHAKE_TIMEOUT)
            if msg is None or msg.msg_type != MessageType.HANDSHAKE:
                self.scorer.record(host, "bad_handshake")
                await peer.close()
                return
            self._process_handshake(peer, msg)
            # Reject wrong-genesis inbounds before we accept them.
            our_genesis = self.node.chain.get_block_by_height(0)
            if (our_genesis is not None and peer.genesis_hash and
                    peer.genesis_hash != our_genesis.block_hash().hex()):
                self.scorer.record(host, "bad_handshake")
                await peer.close()
                return
            await self._send_handshake(peer)
            peer.handshake_done = True
            self.peers[peer.address] = peer
            if peer.listen_port > 0:
                self.known_addresses[(host, peer.listen_port)] = time.time()
            logger.info(f"inbound peer connected: {host}:{port}")
            asyncio.create_task(self._peer_loop(peer))
        except asyncio.TimeoutError:
            await peer.close()

    # -- Handshake --

    async def _perform_handshake(self, peer: PeerConnection) -> None:
        await self._send_handshake(peer)
        msg = await asyncio.wait_for(peer.receive(), timeout=HANDSHAKE_TIMEOUT)
        if msg is None or msg.msg_type != MessageType.HANDSHAKE:
            self.scorer.record(peer.host, "bad_handshake")
            return
        self._process_handshake(peer, msg)
        # verify genesis match
        our_genesis = self.node.chain.get_block_by_height(0)
        if our_genesis and peer.genesis_hash != our_genesis.block_hash().hex():
            logger.warning(f"genesis mismatch with {peer.address}")
            self.scorer.record(peer.host, "bad_handshake")
            return
        peer.handshake_done = True

    async def _send_handshake(self, peer: PeerConnection) -> None:
        genesis = self.node.chain.get_block_by_height(0)
        genesis_hash = genesis.block_hash().hex() if genesis else ""
        msg = make_handshake(PROTOCOL_VERSION, self.node.chain.height, genesis_hash, self.port)
        await peer.send(msg)

    def _process_handshake(self, peer: PeerConnection, msg: Message) -> None:
        peer.version = msg.payload.get("version")
        peer.peer_height = msg.payload.get("height", -1)
        peer.genesis_hash = msg.payload.get("genesis_hash")
        lp = msg.payload.get("listen_port", 0)
        if lp > 0:
            peer.listen_port = lp

    # -- Message loop --

    async def _peer_loop(self, peer: PeerConnection) -> None:
        try:
            # if peer is ahead, sync blocks
            if peer.peer_height > self.node.chain.height:
                await self._start_sync(peer)
            # sync mempool from peer
            await peer.send(make_mempool_request())
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
            MessageType.MEMPOOL_REQUEST: self._on_mempool_request,
            MessageType.MEMPOOL_RESPONSE: self._on_mempool_response,
        }
        handler = handlers.get(msg.msg_type)
        if handler:
            await handler(peer, msg)

    # -- Peers --

    async def _on_peers_request(self, peer: PeerConnection, msg: Message) -> None:
        # Share listen addresses (not ephemeral connection ports)
        addrs = []
        for p in self.peers.values():
            if p is not peer and not p.is_closed:
                addrs.append((p.host, p.listen_port))
        await peer.send(make_peers_response(addrs))

    async def _on_peers_response(self, peer: PeerConnection, msg: Message) -> None:
        now = time.time()
        for entry in msg.payload.get("peers", []):
            addr = (entry["host"], entry["port"])
            if addr not in self.known_addresses:
                self.known_addresses[addr] = now

    # -- Transaction gossip --

    async def _on_tx_announce(self, peer: PeerConnection, msg: Message) -> None:
        tx_hash_hex = msg.payload["tx_hash"]
        if tx_hash_hex in self._seen_tx_hashes:
            self._seen_tx_hashes.move_to_end(tx_hash_hex)
            return
        self._seen_add(self._seen_tx_hashes, tx_hash_hex)
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

    # -- Mempool sync --

    async def _on_mempool_request(self, peer: PeerConnection, msg: Message) -> None:
        pending = self.node.mempool.get_pending()
        # Skip hashes we've already gossiped to this peer.
        tx_hashes: list[str] = []
        for tx in pending:
            h = tx.tx_hash().hex()
            if h in peer.sent_mempool_hashes:
                continue
            tx_hashes.append(h)
            peer.sent_mempool_hashes.add(h)
        await peer.send(make_mempool_response(tx_hashes))

    async def _on_mempool_response(self, peer: PeerConnection, msg: Message) -> None:
        tx_hashes = msg.payload.get("tx_hashes", [])
        for tx_hash_hex in tx_hashes:
            if tx_hash_hex in self._seen_tx_hashes:
                self._seen_tx_hashes.move_to_end(tx_hash_hex)
                continue
            tx_hash = bytes.fromhex(tx_hash_hex)
            if tx_hash in self.node.mempool or tx_hash in self.node.chain.tx_index:
                continue
            # request the full transaction
            await peer.send(make_tx_request(tx_hash_hex))

    # -- Block gossip --

    async def _on_block_announce(self, peer: PeerConnection, msg: Message) -> None:
        block_hash_hex = msg.payload["block_hash"]
        height = msg.payload["height"]
        if block_hash_hex in self._seen_block_hashes:
            self._seen_block_hashes.move_to_end(block_hash_hex)
            return
        self._seen_add(self._seen_block_hashes, block_hash_hex)
        block_hash = bytes.fromhex(block_hash_hex)
        if self.node.chain.get_block_by_hash(block_hash) is not None:
            return
        our_height = self.node.chain.height
        if height == our_height + 1:
            # Directly request this block (extends tip)
            await peer.send(make_block_request(block_hash=block_hash_hex))
        elif height > our_height + 1:
            # Peer is significantly ahead — sync
            await self._start_sync(peer)
        elif height >= our_height - MAX_REORG_DEPTH:
            # Potential fork block within reorg window — fetch it
            await peer.send(make_block_request(block_hash=block_hash_hex))

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
        self._seen_add(self._seen_tx_hashes, tx_hash_hex)
        msg = make_tx_announce(tx_hash_hex)
        for peer in list(self.peers.values()):
            if peer is not exclude and not peer.is_closed:
                await peer.send(msg)

    async def broadcast_block(
        self, block_hash_hex: str, height: int, exclude: PeerConnection | None = None,
    ) -> None:
        self._seen_add(self._seen_block_hashes, block_hash_hex)
        msg = make_block_announce(block_hash_hex, height)
        for peer in list(self.peers.values()):
            if peer is not exclude and not peer.is_closed:
                await peer.send(msg)

    # -- Peer exchange background task --

    def _is_connected_to(self, host: str, port: int) -> bool:
        """Check if we're already connected to a peer at this listen address."""
        if host == self.host and port == self.port:
            return True  # that's us
        if host in ("0.0.0.0", "127.0.0.1", "localhost") and port == self.port:
            return True  # that's us
        for p in self.peers.values():
            if p.host == host and p.listen_port == port:
                return True
        return False

    async def peer_exchange_loop(self) -> None:
        # First exchange fires quickly so peers discover each other fast
        await asyncio.sleep(PEER_EXCHANGE_INITIAL_DELAY)
        while True:
            for peer in list(self.peers.values()):
                if not peer.is_closed:
                    await peer.send(make_peers_request())
            # Short delay to let responses arrive before connecting
            await asyncio.sleep(2)
            for addr in list(self.known_addresses):
                if not self._is_connected_to(*addr) and self._outbound_count < MAX_OUTBOUND:
                    asyncio.create_task(self.connect_to_peer(*addr))
            self.save_peers()
            await asyncio.sleep(PEER_EXCHANGE_INTERVAL)

    # -- Peer persistence --

    def _peers_path(self) -> str | None:
        if self.data_dir is None:
            return None
        return os.path.join(self.data_dir, "peers.json")

    def load_peers(self) -> None:
        """Load saved peers from disk into known_addresses."""
        path = self._peers_path()
        if path is None or not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                entries = json.load(f)
            now = time.time()
            loaded = 0
            for entry in entries:
                last_seen = entry.get("last_seen", 0)
                if now - last_seen > PEER_MAX_AGE:
                    continue
                addr = (entry["host"], entry["port"])
                if addr not in self.known_addresses:
                    self.known_addresses[addr] = last_seen
                    loaded += 1
            if loaded:
                logger.info(f"loaded {loaded} saved peers from {path}")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"failed to load peers from {path}: {e}")

    # -- Seen-set persistence --

    def _seen_path(self) -> str | None:
        if self.data_dir is None:
            return None
        return os.path.join(self.data_dir, "seen.json")

    def load_seen(self) -> None:
        path = self._seen_path()
        if path is None or not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            for h in (data.get("tx") or [])[-SEEN_SET_MAX:]:
                if isinstance(h, str):
                    self._seen_tx_hashes[h] = None
            for h in (data.get("block") or [])[-SEEN_SET_MAX:]:
                if isinstance(h, str):
                    self._seen_block_hashes[h] = None
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning(f"failed to load seen-set from {path}: {e}")

    def save_seen(self) -> None:
        path = self._seen_path()
        if path is None:
            return
        data = {
            "tx": list(self._seen_tx_hashes.keys()),
            "block": list(self._seen_block_hashes.keys()),
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError as e:
            logger.debug(f"failed to save seen-set: {e}")

    async def _seen_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(SEEN_SET_FLUSH_INTERVAL)
            self.save_seen()

    def save_peers(self) -> None:
        """Save known_addresses to disk atomically."""
        path = self._peers_path()
        if path is None:
            return
        # Also update last_seen for currently connected peers
        now = time.time()
        for peer in self.peers.values():
            if not peer.is_closed and peer.listen_port > 0:
                self.known_addresses[(peer.host, peer.listen_port)] = now
        # Sort by last_seen descending, cap at MAX_SAVED_PEERS
        entries = sorted(
            self.known_addresses.items(), key=lambda x: x[1], reverse=True,
        )[:MAX_SAVED_PEERS]
        data = [
            {"host": addr[0], "port": addr[1], "last_seen": ts}
            for addr, ts in entries
        ]
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
        except OSError as e:
            logger.warning(f"failed to save peers: {e}")
