from __future__ import annotations

import asyncio
import logging

from jiji.core.config import MAX_MESSAGE_SIZE
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
    ):
        self.reader = reader
        self.writer = writer
        self.host = host
        self.port = port
        self.inbound = inbound
        self.version: int | None = None
        self.peer_height: int = -1
        self.genesis_hash: str | None = None
        self.handshake_done = False
        self._closed = False

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

    async def receive(self) -> Message | None:
        """Read one framed message. Returns None on EOF or error."""
        try:
            header = await self.reader.readexactly(4)
            length = decode_length_prefix(header)
            if length > MAX_MESSAGE_SIZE:
                logger.warning(f"message too large from {self.address}: {length}")
                await self.close()
                return None
            data = await self.reader.readexactly(length)
            return decode_message(data)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            logger.debug(f"receive error from {self.address}: {e}")
            await self.close()
            return None

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
