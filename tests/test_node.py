import asyncio
import json
import pytest
from jiji.core.crypto import generate_keypair
from jiji.core.transaction import Post, Transfer
from jiji.node import Node


def make_keys():
    return generate_keypair()


async def create_node(mine=False, bootstrap_peers=None, genesis_block=None):
    """Create and start a node on random ports."""
    priv, pub = make_keys()
    node = Node(
        private_key=priv,
        public_key=pub,
        p2p_host="127.0.0.1",
        p2p_port=0,  # OS-assigned
        rpc_host="127.0.0.1",
        rpc_port=0,  # OS-assigned
        mine=mine,
        bootstrap_peers=bootstrap_peers or [],
    )
    await node.start(genesis_block=genesis_block)
    return node


def get_p2p_port(node):
    return node.p2p._server.sockets[0].getsockname()[1]


def get_rpc_port(node):
    return node.rpc._server.sockets[0].getsockname()[1]


async def rpc_call(port, method, params=None):
    """Send a JSON-RPC request over HTTP and return the parsed response."""
    body = json.dumps({
        "jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1,
    }).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"POST / HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    )
    await writer.drain()
    resp = await asyncio.wait_for(reader.read(65536), timeout=5)
    writer.close()
    await writer.wait_closed()
    _, _, resp_body = resp.partition(b"\r\n\r\n")
    return json.loads(resp_body)


class TestSingleNode:
    def test_starts_and_stops(self):
        async def _test():
            node = await create_node()
            try:
                assert node.chain.height == 0
                assert node._running
            finally:
                await node.stop()
        asyncio.run(_test())

    def test_rpc_get_latest_block(self):
        async def _test():
            node = await create_node()
            try:
                port = get_rpc_port(node)
                result = await rpc_call(port, "get_latest_block")
                assert result["result"]["header"]["height"] == 0
            finally:
                await node.stop()
        asyncio.run(_test())

    def test_rpc_submit_transaction(self):
        async def _test():
            node = await create_node()
            try:
                port = get_rpc_port(node)
                post = Post(
                    author=node.public_key, nonce=0, timestamp=1000010,
                    body="rpc test", reply_to=None, gas_fee=1,
                )
                post.sign_tx(node.private_key)
                result = await rpc_call(port, "submit_transaction", {
                    "transaction": post.to_dict(),
                })
                assert "result" in result
                assert result["result"]["tx_hash"] == post.tx_hash().hex()
                # verify it's in mempool
                mempool_result = await rpc_call(port, "get_mempool")
                assert post.tx_hash().hex() in mempool_result["result"]["transactions"]
            finally:
                await node.stop()
        asyncio.run(_test())

    def test_mining_produces_blocks(self):
        async def _test():
            node = await create_node(mine=True)
            try:
                # wait for at least one block to be mined
                for _ in range(50):
                    if node.chain.height > 0:
                        break
                    await asyncio.sleep(0.1)
                assert node.chain.height > 0
            finally:
                await node.stop()
        asyncio.run(_test())


class TestTwoNodes:
    def test_peer_connection(self):
        async def _test():
            node_a = await create_node()
            p2p_port_a = get_p2p_port(node_a)
            genesis = node_a.chain.get_block_by_height(0)
            node_b = await create_node(genesis_block=genesis)
            try:
                connected = await node_b.p2p.connect_to_peer("127.0.0.1", p2p_port_a)
                assert connected
                assert len(node_b.p2p.peers) == 1
                # give inbound connection time to register
                await asyncio.sleep(0.1)
                assert len(node_a.p2p.peers) == 1
            finally:
                await node_b.stop()
                await node_a.stop()
        asyncio.run(_test())

    def test_tx_gossip(self):
        async def _test():
            node_a = await create_node()
            p2p_port_a = get_p2p_port(node_a)
            genesis = node_a.chain.get_block_by_height(0)
            node_b = await create_node(genesis_block=genesis)
            try:
                await node_b.p2p.connect_to_peer("127.0.0.1", p2p_port_a)
                await asyncio.sleep(0.1)
                # submit tx to node_a
                post = Post(
                    author=node_a.public_key, nonce=0, timestamp=1000010,
                    body="gossip", reply_to=None, gas_fee=1,
                )
                post.sign_tx(node_a.private_key)
                await node_a.handle_new_transaction(post.to_dict())
                # wait for gossip propagation
                for _ in range(30):
                    if post.tx_hash() in node_b.mempool:
                        break
                    await asyncio.sleep(0.1)
                assert post.tx_hash() in node_b.mempool
            finally:
                await node_b.stop()
                await node_a.stop()
        asyncio.run(_test())

    def test_block_gossip(self):
        async def _test():
            node_a = await create_node(mine=True)
            p2p_port_a = get_p2p_port(node_a)
            genesis = node_a.chain.get_block_by_height(0)
            node_b = await create_node(genesis_block=genesis)
            try:
                await node_b.p2p.connect_to_peer("127.0.0.1", p2p_port_a)
                # wait for node_a to mine and node_b to receive
                for _ in range(100):
                    if node_b.chain.height > 0:
                        break
                    await asyncio.sleep(0.1)
                assert node_b.chain.height > 0
            finally:
                await node_b.stop()
                await node_a.stop()
        asyncio.run(_test())

    def test_sync_on_connect(self):
        async def _test():
            node_a = await create_node(mine=True)
            # let node_a mine a few blocks
            for _ in range(100):
                if node_a.chain.height >= 3:
                    break
                await asyncio.sleep(0.1)
            assert node_a.chain.height >= 3

            p2p_port_a = get_p2p_port(node_a)
            genesis = node_a.chain.get_block_by_height(0)
            node_b = await create_node(genesis_block=genesis)
            try:
                await node_b.p2p.connect_to_peer("127.0.0.1", p2p_port_a)
                # wait for sync
                for _ in range(100):
                    if node_b.chain.height >= 3:
                        break
                    await asyncio.sleep(0.1)
                assert node_b.chain.height >= 3
            finally:
                await node_b.stop()
                await node_a.stop()
        asyncio.run(_test())
