import struct
import pytest
from jiji.net.protocol import (
    MessageType,
    Message,
    encode_message,
    decode_length_prefix,
    decode_message,
    make_handshake,
    make_peers_request,
    make_peers_response,
    make_tx_announce,
    make_tx_request,
    make_tx_response,
    make_block_announce,
    make_block_request,
    make_block_response,
    make_sync_request,
    make_sync_response,
)


class TestMessageType:
    def test_all_values_unique(self):
        values = [m.value for m in MessageType]
        assert len(values) == len(set(values))

    def test_has_eleven_types(self):
        assert len(MessageType) == 11

    def test_roundtrip_all_types(self):
        for mt in MessageType:
            assert MessageType(mt.value) == mt


class TestMessageEncodeDecode:
    def test_roundtrip_handshake(self):
        msg = make_handshake(1, 42, "abc123")
        encoded = encode_message(msg)
        length = decode_length_prefix(encoded[:4])
        decoded = decode_message(encoded[4:])
        assert decoded.msg_type == MessageType.HANDSHAKE
        assert decoded.payload["version"] == 1
        assert decoded.payload["height"] == 42
        assert decoded.payload["genesis_hash"] == "abc123"

    def test_roundtrip_peers_response(self):
        peers = [("127.0.0.1", 9333), ("10.0.0.1", 9334)]
        msg = make_peers_response(peers)
        encoded = encode_message(msg)
        decoded = decode_message(encoded[4:])
        assert decoded.msg_type == MessageType.PEERS_RESPONSE
        assert len(decoded.payload["peers"]) == 2
        assert decoded.payload["peers"][0]["host"] == "127.0.0.1"
        assert decoded.payload["peers"][1]["port"] == 9334

    def test_roundtrip_tx_announce(self):
        msg = make_tx_announce("ff" * 32)
        encoded = encode_message(msg)
        decoded = decode_message(encoded[4:])
        assert decoded.msg_type == MessageType.TX_ANNOUNCE
        assert decoded.payload["tx_hash"] == "ff" * 32

    def test_roundtrip_block_announce(self):
        msg = make_block_announce("aa" * 32, 10)
        encoded = encode_message(msg)
        decoded = decode_message(encoded[4:])
        assert decoded.msg_type == MessageType.BLOCK_ANNOUNCE
        assert decoded.payload["block_hash"] == "aa" * 32
        assert decoded.payload["height"] == 10

    def test_roundtrip_sync_request(self):
        msg = make_sync_request(5, 15)
        encoded = encode_message(msg)
        decoded = decode_message(encoded[4:])
        assert decoded.msg_type == MessageType.SYNC_REQUEST
        assert decoded.payload["start_height"] == 5
        assert decoded.payload["end_height"] == 15

    def test_roundtrip_sync_response(self):
        blocks = [{"header": {"height": i}} for i in range(3)]
        msg = make_sync_response(blocks)
        encoded = encode_message(msg)
        decoded = decode_message(encoded[4:])
        assert decoded.msg_type == MessageType.SYNC_RESPONSE
        assert len(decoded.payload["blocks"]) == 3

    def test_length_prefix_matches_json(self):
        msg = make_handshake(1, 0, "00" * 32)
        encoded = encode_message(msg)
        length = struct.unpack("!I", encoded[:4])[0]
        assert length == len(encoded) - 4

    def test_empty_payload_roundtrips(self):
        msg = make_peers_request()
        encoded = encode_message(msg)
        decoded = decode_message(encoded[4:])
        assert decoded.msg_type == MessageType.PEERS_REQUEST
        assert decoded.payload == {}

    def test_message_to_dict_from_dict(self):
        msg = Message(MessageType.TX_REQUEST, {"tx_hash": "ab" * 32})
        d = msg.to_dict()
        restored = Message.from_dict(d)
        assert restored.msg_type == msg.msg_type
        assert restored.payload == msg.payload


class TestFactoryFunctions:
    def test_make_handshake(self):
        msg = make_handshake(1, 5, "deadbeef")
        assert msg.msg_type == MessageType.HANDSHAKE
        assert msg.payload["version"] == 1
        assert msg.payload["height"] == 5

    def test_make_peers_request(self):
        msg = make_peers_request()
        assert msg.msg_type == MessageType.PEERS_REQUEST
        assert msg.payload == {}

    def test_make_peers_response(self):
        msg = make_peers_response([("1.2.3.4", 80)])
        assert msg.payload["peers"][0]["host"] == "1.2.3.4"

    def test_make_tx_announce(self):
        msg = make_tx_announce("ab" * 32)
        assert msg.payload["tx_hash"] == "ab" * 32

    def test_make_tx_request(self):
        msg = make_tx_request("cd" * 32)
        assert msg.payload["tx_hash"] == "cd" * 32

    def test_make_tx_response_with_tx(self):
        msg = make_tx_response({"tx_type": "post", "body": "hi"})
        assert msg.payload["transaction"]["body"] == "hi"

    def test_make_tx_response_none(self):
        msg = make_tx_response(None)
        assert msg.payload["transaction"] is None

    def test_make_block_request_by_hash(self):
        msg = make_block_request(block_hash="ee" * 32)
        assert msg.payload["block_hash"] == "ee" * 32
        assert "height" not in msg.payload

    def test_make_block_request_by_height(self):
        msg = make_block_request(height=7)
        assert msg.payload["height"] == 7
        assert "block_hash" not in msg.payload

    def test_make_block_response(self):
        msg = make_block_response({"header": {"height": 0}})
        assert msg.payload["block"]["header"]["height"] == 0

    def test_make_sync_request(self):
        msg = make_sync_request(10, 20)
        assert msg.payload["start_height"] == 10
        assert msg.payload["end_height"] == 20

    def test_make_sync_response(self):
        msg = make_sync_response([{"h": 1}, {"h": 2}])
        assert len(msg.payload["blocks"]) == 2
