import asyncio
import json
import pytest
from jiji.core.crypto import generate_keypair
from jiji.core.chain import Blockchain
from jiji.core.config import block_reward
from jiji.core.merkle import verify_merkle_proof
from jiji.core.transaction import Post, Transfer, transaction_from_dict
from jiji.core.validation import ValidationError
from jiji.mining.mempool import Mempool
from jiji.mining.miner import Miner
from jiji.rpc.server import RPCServer
from tests.test_chain import build_block


def make_keys():
    return generate_keypair()


class FakeP2P:
    """Stub for P2PServer so RPCServer can call node.p2p.peers."""
    def __init__(self):
        self.peers = {}
        self.broadcast_calls = []

    async def broadcast_tx(self, tx_hash_hex, exclude=None):
        self.broadcast_calls.append(("tx", tx_hash_hex))

    async def broadcast_block(self, block_hash_hex, height, exclude=None):
        self.broadcast_calls.append(("block", block_hash_hex))


class FakeNode:
    """Minimal node-like object for RPC testing."""

    def __init__(self):
        self.priv, self.pub = make_keys()
        self.chain = Blockchain()
        self.chain.initialize_genesis(self.pub, timestamp=1000000)
        self.mempool = Mempool(self.chain)
        self.p2p = FakeP2P()

    async def handle_new_transaction(self, tx_dict, source_peer=None):
        tx = transaction_from_dict(tx_dict)
        tx_hash = self.mempool.add(tx)
        tx_hash_hex = tx_hash.hex()
        await self.p2p.broadcast_tx(tx_hash_hex, exclude=source_peer)
        return tx_hash_hex


def make_rpc():
    node = FakeNode()
    rpc = RPCServer(node, "127.0.0.1", 0)
    return rpc, node


def dispatch(rpc, method, params=None):
    """Helper to call RPC dispatch synchronously."""
    request = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    return asyncio.run(rpc._dispatch(request))


class TestRPCDirect:
    def test_submit_transaction(self):
        rpc, node = make_rpc()
        post = Post(author=node.pub, nonce=0, timestamp=1000010, body="hello", reply_to=None, gas_fee=1)
        post.sign_tx(node.priv)
        result = dispatch(rpc, "submit_transaction", {"transaction": post.to_dict()})
        assert "result" in result
        assert result["result"]["tx_hash"] == post.tx_hash().hex()

    def test_submit_invalid_transaction(self):
        rpc, node = make_rpc()
        post = Post(author=node.pub, nonce=0, timestamp=1000010, body="hi", reply_to=None, gas_fee=0)
        post.sign_tx(node.priv)
        result = dispatch(rpc, "submit_transaction", {"transaction": post.to_dict()})
        assert "error" in result

    def test_get_block_by_height(self):
        rpc, node = make_rpc()
        result = dispatch(rpc, "get_block", {"height": 0})
        assert "result" in result
        assert result["result"]["header"]["height"] == 0

    def test_get_block_by_hash(self):
        rpc, node = make_rpc()
        genesis_hash = node.chain.tip.block_hash().hex()
        result = dispatch(rpc, "get_block", {"hash": genesis_hash})
        assert "result" in result
        assert result["result"]["header"]["height"] == 0

    def test_get_block_not_found(self):
        rpc, node = make_rpc()
        result = dispatch(rpc, "get_block", {"height": 99})
        assert "error" in result

    def test_get_transaction_confirmed(self):
        rpc, node = make_rpc()
        post = Post(author=node.pub, nonce=0, timestamp=1000015, body="find", reply_to=None, gas_fee=1)
        post.sign_tx(node.priv)
        block = build_block(node.chain, [post], node.pub, 1000015)
        node.chain.add_block(block, current_time=1000020)
        result = dispatch(rpc, "get_transaction", {"tx_hash": post.tx_hash().hex()})
        assert "result" in result
        assert result["result"]["body"] == "find"

    def test_get_transaction_in_mempool(self):
        rpc, node = make_rpc()
        post = Post(author=node.pub, nonce=0, timestamp=1000010, body="pending", reply_to=None, gas_fee=1)
        post.sign_tx(node.priv)
        node.mempool.add(post)
        result = dispatch(rpc, "get_transaction", {"tx_hash": post.tx_hash().hex()})
        assert "result" in result
        assert result["result"]["body"] == "pending"

    def test_get_transaction_not_found(self):
        rpc, node = make_rpc()
        result = dispatch(rpc, "get_transaction", {"tx_hash": "ff" * 32})
        assert "error" in result

    def test_get_account_exists(self):
        rpc, node = make_rpc()
        result = dispatch(rpc, "get_account", {"pubkey": node.pub.hex()})
        assert "result" in result
        assert result["result"]["balance"] == block_reward(0)
        assert result["result"]["nonce"] == 0

    def test_get_account_not_exists(self):
        rpc, node = make_rpc()
        _, unknown = make_keys()
        result = dispatch(rpc, "get_account", {"pubkey": unknown.hex()})
        assert result["result"]["balance"] == 0
        assert result["result"]["nonce"] == 0

    def test_get_latest_block(self):
        rpc, node = make_rpc()
        result = dispatch(rpc, "get_latest_block")
        assert "result" in result
        assert result["result"]["header"]["height"] == 0

    def test_get_mempool_empty(self):
        rpc, node = make_rpc()
        result = dispatch(rpc, "get_mempool")
        assert result["result"]["transactions"] == []

    def test_get_mempool_with_txs(self):
        rpc, node = make_rpc()
        post = Post(author=node.pub, nonce=0, timestamp=1000010, body="pool", reply_to=None, gas_fee=1)
        post.sign_tx(node.priv)
        node.mempool.add(post)
        result = dispatch(rpc, "get_mempool")
        assert post.tx_hash().hex() in result["result"]["transactions"]

    def test_get_merkle_proof(self):
        rpc, node = make_rpc()
        post = Post(author=node.pub, nonce=0, timestamp=1000015, body="proof", reply_to=None, gas_fee=1)
        post.sign_tx(node.priv)
        block = build_block(node.chain, [post], node.pub, 1000015)
        node.chain.add_block(block, current_time=1000020)
        result = dispatch(rpc, "get_merkle_proof", {"tx_hash": post.tx_hash().hex()})
        assert "result" in result
        proof_data = result["result"]
        assert proof_data["tx_hash"] == post.tx_hash().hex()
        # verify the proof is valid
        proof = [(bytes.fromhex(p["hash"]), p["is_left"]) for p in proof_data["proof"]]
        root = bytes.fromhex(proof_data["root"])
        assert verify_merkle_proof(post.tx_hash(), proof, root)

    def test_get_node_info(self):
        rpc, node = make_rpc()
        result = dispatch(rpc, "get_node_info")
        assert result["result"]["height"] == 0
        assert result["result"]["peer_count"] == 0
        assert result["result"]["mempool_size"] == 0

    def test_unknown_method(self):
        rpc, node = make_rpc()
        result = dispatch(rpc, "nonexistent_method")
        assert result["error"]["code"] == -32601


class TestRPCHTTP:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_valid_request_over_http(self):
        async def _test():
            rpc, node = make_rpc()
            await rpc.start()
            port = rpc._server.sockets[0].getsockname()[1]
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                body = json.dumps({
                    "jsonrpc": "2.0", "method": "get_latest_block", "params": {}, "id": 1,
                }).encode()
                request = (
                    f"POST / HTTP/1.1\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n"
                ).encode() + body
                writer.write(request)
                await writer.drain()
                response = await asyncio.wait_for(reader.read(8192), timeout=5)
                writer.close()
                await writer.wait_closed()
                # parse HTTP response body
                _, _, resp_body = response.partition(b"\r\n\r\n")
                data = json.loads(resp_body)
                assert data["id"] == 1
                assert data["result"]["header"]["height"] == 0
            finally:
                await rpc.stop()
        self._run(_test())

    def test_malformed_json_over_http(self):
        async def _test():
            rpc, node = make_rpc()
            await rpc.start()
            port = rpc._server.sockets[0].getsockname()[1]
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                body = b"not json at all"
                request = (
                    f"POST / HTTP/1.1\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n"
                ).encode() + body
                writer.write(request)
                await writer.drain()
                response = await asyncio.wait_for(reader.read(8192), timeout=5)
                writer.close()
                await writer.wait_closed()
                _, _, resp_body = response.partition(b"\r\n\r\n")
                data = json.loads(resp_body)
                assert data["error"]["code"] == -32700
            finally:
                await rpc.stop()
        self._run(_test())

    def test_submit_and_retrieve_over_http(self):
        async def _test():
            rpc, node = make_rpc()
            await rpc.start()
            port = rpc._server.sockets[0].getsockname()[1]
            try:
                # submit a transaction
                post = Post(author=node.pub, nonce=0, timestamp=1000010, body="http", reply_to=None, gas_fee=1)
                post.sign_tx(node.priv)
                submit_body = json.dumps({
                    "jsonrpc": "2.0", "method": "submit_transaction",
                    "params": {"transaction": post.to_dict()}, "id": 1,
                }).encode()
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(
                    f"POST / HTTP/1.1\r\nContent-Length: {len(submit_body)}\r\n\r\n".encode()
                    + submit_body
                )
                await writer.drain()
                resp = await asyncio.wait_for(reader.read(8192), timeout=5)
                writer.close()
                await writer.wait_closed()
                _, _, body = resp.partition(b"\r\n\r\n")
                submit_result = json.loads(body)
                assert "result" in submit_result

                # retrieve it
                get_body = json.dumps({
                    "jsonrpc": "2.0", "method": "get_transaction",
                    "params": {"tx_hash": post.tx_hash().hex()}, "id": 2,
                }).encode()
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(
                    f"POST / HTTP/1.1\r\nContent-Length: {len(get_body)}\r\n\r\n".encode()
                    + get_body
                )
                await writer.drain()
                resp = await asyncio.wait_for(reader.read(8192), timeout=5)
                writer.close()
                await writer.wait_closed()
                _, _, body = resp.partition(b"\r\n\r\n")
                get_result = json.loads(body)
                assert get_result["result"]["body"] == "http"
            finally:
                await rpc.stop()
        self._run(_test())
