from __future__ import annotations

import asyncio
import logging
import time

from jiji.core.config import MAX_MESSAGE_SIZE, PEER_MSG_BURST, PEER_MSG_PER_SEC
from jiji.net.protocol import Message, decode_length_prefix, decode_message, encode_message

logger = logging.getLogger(__name__)


class PeerConnection:
    """Wraps an asyncio StreamReader/StreamWriter pair for the P2P protocol."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
        inbound: bool = False,
        rate_limit: bool = True,
    ):
        self.reader = reader
        self.writer = writer
        self.host = host
        self.port = port
        self.inbound = inbound
        self.version: int | None = None
        self.peer_height: int = -1
        self.genesis_hash: str | None = None
        self.listen_port: int = port  # defaults to connection port, updated by handshake
        self.handshake_done = False
        self._closed = False
        # Token bucket: refills at PEER_MSG_PER_SEC, caps at PEER_MSG_BURST.
        self._rate_limit = rate_limit
        self._tokens: float = float(PEER_MSG_BURST)
        self._tokens_updated: float = time.monotonic()
        # Hashes we've already told this peer about (for mempool dedup).
        self.sent_mempool_hashes: set[str] = set()

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def send(self, msg: Message) -> None:
        """Send a framed message to this peer."""
        if self._closed:
            return
        try:
            data = encode_message(msg)
            self.writer.write(data)
            await self.writer.drain()
        except (ConnectionError, OSError) as e:
            logger.debug(f"send error to {self.address}: {e}")
            await self.close()

    def _consume_token(self) -> bool:
        """Refill the bucket and try to take one token. False means drop this msg."""
        if not self._rate_limit:
            return True
        now = time.monotonic()
        elapsed = now - self._tokens_updated
        if elapsed > 0:
            self._tokens = min(
                float(PEER_MSG_BURST),
                self._tokens + elapsed * PEER_MSG_PER_SEC,
            )
            self._tokens_updated = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    async def receive(self) -> Message | None:
        """Read one framed message. Returns None on EOF or error.

        Over-rate messages are read off the wire but silently dropped — we
        don't want honest clients during a big-block sync to be disconnected
        as a side effect of their own catch-up traffic. Returns the next
        in-budget message, skipping drops.
        """
        while True:
            try:
                header = await self.reader.readexactly(4)
                length = decode_length_prefix(header)
                if length > MAX_MESSAGE_SIZE:
                    logger.warning(f"message too large from {self.address}: {length}")
                    await self.close()
                    return None
                data = await self.reader.readexactly(length)
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
                logger.debug(f"receive error from {self.address}: {e}")
                await self.close()
                return None
            if not self._consume_token():
                logger.debug(f"rate-limited message from {self.address} (dropped)")
                continue
            return decode_message(data)

    async def close(self) -> None:
        """Close the connection."""
        if self._closed:
            return
        self._closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
