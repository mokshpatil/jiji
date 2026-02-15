from __future__ import annotations

import enum
import json
import struct
from dataclasses import dataclass

from jiji.core.config import MAX_MESSAGE_SIZE


class MessageType(enum.IntEnum):
    HANDSHAKE = 0
    PEERS_REQUEST = 1
    PEERS_RESPONSE = 2
    TX_ANNOUNCE = 3
    TX_REQUEST = 4
    TX_RESPONSE = 5
    BLOCK_ANNOUNCE = 6
    BLOCK_REQUEST = 7
    BLOCK_RESPONSE = 8
    SYNC_REQUEST = 9
    SYNC_RESPONSE = 10


@dataclass
class Message:
    """A P2P protocol message."""

    msg_type: MessageType
    payload: dict

    def to_dict(self) -> dict:
        return {"type": self.msg_type.value, "payload": self.payload}

    @classmethod
    def from_dict(cls, d: dict) -> Message:
        return cls(msg_type=MessageType(d["type"]), payload=d["payload"])


# Wire format: [4 bytes big-endian uint32 length][JSON bytes]


def encode_message(msg: Message) -> bytes:
    """Serialize a Message to length-prefixed JSON bytes."""
    data = json.dumps(msg.to_dict(), separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE_SIZE:
        raise ValueError(f"message too large: {len(data)} bytes")
    return struct.pack("!I", len(data)) + data


def decode_length_prefix(header_bytes: bytes) -> int:
    """Decode the 4-byte big-endian length prefix."""
    return struct.unpack("!I", header_bytes)[0]


def decode_message(data: bytes) -> Message:
    """Deserialize JSON bytes (without length prefix) into a Message."""
    d = json.loads(data.decode("utf-8"))
    return Message.from_dict(d)


# -- Factory functions --


def make_handshake(version: int, height: int, genesis_hash: str) -> Message:
    return Message(MessageType.HANDSHAKE, {
        "version": version, "height": height, "genesis_hash": genesis_hash,
    })


def make_peers_request() -> Message:
    return Message(MessageType.PEERS_REQUEST, {})


def make_peers_response(peers: list[tuple[str, int]]) -> Message:
    return Message(MessageType.PEERS_RESPONSE, {
        "peers": [{"host": h, "port": p} for h, p in peers],
    })


def make_tx_announce(tx_hash: str) -> Message:
    return Message(MessageType.TX_ANNOUNCE, {"tx_hash": tx_hash})


def make_tx_request(tx_hash: str) -> Message:
    return Message(MessageType.TX_REQUEST, {"tx_hash": tx_hash})


def make_tx_response(tx_dict: dict | None) -> Message:
    return Message(MessageType.TX_RESPONSE, {"transaction": tx_dict})


def make_block_announce(block_hash: str, height: int) -> Message:
    return Message(MessageType.BLOCK_ANNOUNCE, {
        "block_hash": block_hash, "height": height,
    })


def make_block_request(block_hash: str | None = None, height: int | None = None) -> Message:
    payload: dict = {}
    if block_hash is not None:
        payload["block_hash"] = block_hash
    if height is not None:
        payload["height"] = height
    return Message(MessageType.BLOCK_REQUEST, payload)


def make_block_response(block_dict: dict | None) -> Message:
    return Message(MessageType.BLOCK_RESPONSE, {"block": block_dict})


def make_sync_request(start_height: int, end_height: int) -> Message:
    return Message(MessageType.SYNC_REQUEST, {
        "start_height": start_height, "end_height": end_height,
    })


def make_sync_response(blocks: list[dict]) -> Message:
    return Message(MessageType.SYNC_RESPONSE, {"blocks": blocks})
