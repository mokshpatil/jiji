"""Tests for RPC rate limit and peer-message token bucket."""
import asyncio
import json

import pytest

from jiji.core.config import PEER_MSG_BURST, RPC_REQ_PER_MIN
from jiji.core.crypto import generate_keypair
from jiji.net.peer import PeerConnection
from jiji.node import Node


async def _make_node(**kwargs):
    priv, pub = generate_keypair()
    node = Node(
        private_key=priv,
        public_key=pub,
        p2p_host="127.0.0.1",
        p2p_port=0,
        rpc_host="127.0.0.1",
        rpc_port=0,
        **kwargs,
    )
    await node.start()
    return node


def _rpc_port(node):
    return node.rpc._server.sockets[0].getsockname()[1]


async def _send_rpc(port: int, method: str = "get_node_info") -> tuple[int, dict]:
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": {}, "id": 1}).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"POST / HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    )
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(65536), timeout=5)
    writer.close()
    await writer.wait_closed()
    status_line = raw.split(b"\r\n", 1)[0].decode()
    status = int(status_line.split(" ")[1])
    _, _, body_bytes = raw.partition(b"\r\n\r\n")
    try:
        parsed = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed


class TestRPCRateLimit:
    def test_over_limit_returns_429(self):
        async def _t():
            node = await _make_node()
            try:
                port = _rpc_port(node)
                # First RPC_REQ_PER_MIN requests should succeed.
                for _ in range(RPC_REQ_PER_MIN):
                    status, _ = await _send_rpc(port)
                    assert status == 200
                status, body = await _send_rpc(port)
                assert status == 429
                assert body.get("error", {}).get("code") == -32002
            finally:
                await node.stop()
        asyncio.run(_t())

    def test_no_rate_limit_flag(self):
        async def _t():
            node = await _make_node(rate_limit=False)
            try:
                port = _rpc_port(node)
                for _ in range(RPC_REQ_PER_MIN + 10):
                    status, _ = await _send_rpc(port)
                    assert status == 200
            finally:
                await node.stop()
        asyncio.run(_t())

    def test_trusted_cidr_exempt(self):
        async def _t():
            node = await _make_node(trusted_cidrs=("127.0.0.0/8",))
            try:
                port = _rpc_port(node)
                for _ in range(RPC_REQ_PER_MIN + 5):
                    status, _ = await _send_rpc(port)
                    assert status == 200
            finally:
                await node.stop()
        asyncio.run(_t())


class TestPeerTokenBucket:
    def test_bucket_refuses_after_burst(self):
        async def _t():
            reader, writer = await _make_fake_stream_pair()
            peer = PeerConnection(reader, writer, "127.0.0.1", 9999, rate_limit=True)
            for _ in range(PEER_MSG_BURST):
                assert peer._consume_token()
            assert not peer._consume_token()
        asyncio.run(_t())

    def test_disabled_bucket_never_limits(self):
        async def _t():
            reader, writer = await _make_fake_stream_pair()
            peer = PeerConnection(reader, writer, "127.0.0.1", 9999, rate_limit=False)
            for _ in range(PEER_MSG_BURST * 3):
                assert peer._consume_token()
        asyncio.run(_t())


async def _make_fake_stream_pair():
    """Cheap StreamReader/StreamWriter pair that isn't actually wired anywhere."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()

    class _NullTransport:
        def is_closing(self):
            return False

        def write(self, _):
            pass

        def close(self):
            pass

        def get_extra_info(self, _name, default=None):
            return default

    protocol = asyncio.StreamReaderProtocol(reader)
    writer = asyncio.StreamWriter(_NullTransport(), protocol, reader, loop)
    return reader, writer
