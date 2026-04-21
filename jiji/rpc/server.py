from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import deque
from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING

from jiji.core.config import DEFAULT_RPC_PORT, RPC_REQ_PER_MIN
from jiji.core.merkle import merkle_proof
from jiji.core.transaction import transaction_from_dict

if TYPE_CHECKING:
    from jiji.node import Node

logger = logging.getLogger(__name__)


class RPCServer:
    """Minimal async HTTP server implementing JSON-RPC 2.0.

    When `auth_token` is set, requests must carry a matching
    `Authorization: Bearer <token>` header. When `allow_origin` is set,
    CORS headers are emitted and `OPTIONS` preflight requests return 204.
    """

    def __init__(
        self,
        node: Node,
        host: str = "127.0.0.1",
        port: int = DEFAULT_RPC_PORT,
        auth_token: str | None = None,
        allow_origin: str | None = None,
        rate_limit: bool = True,
        trusted_cidrs: tuple[str, ...] = (),
    ):
        self.node = node
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.allow_origin = allow_origin
        self.rate_limit = rate_limit
        self._trusted = [ip_network(c, strict=False) for c in trusted_cidrs]
        self._req_log: dict[str, deque[float]] = {}
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port,
        )
        logger.info(f"RPC server listening on {self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _is_trusted(self, ip: str) -> bool:
        if not self._trusted:
            return False
        try:
            addr = ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self._trusted)

    def _rate_limit_ok(self, ip: str) -> bool:
        """Per-IP sliding-window cap on RPC requests."""
        if not self.rate_limit or self._is_trusted(ip):
            return True
        now = time.monotonic()
        log = self._req_log.setdefault(ip, deque())
        cutoff = now - 60
        while log and log[0] < cutoff:
            log.popleft()
        if len(log) >= RPC_REQ_PER_MIN:
            return False
        log.append(now)
        return True

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            peer_addr = writer.get_extra_info("peername")
            peer_ip = peer_addr[0] if peer_addr else ""

            # read HTTP request headers
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
                if not chunk:
                    return
                raw += chunk

            header_part, _, body_start = raw.partition(b"\r\n\r\n")
            headers_text = header_part.decode("utf-8", errors="replace")
            header_lines = headers_text.split("\r\n")
            request_line = header_lines[0] if header_lines else ""
            method_verb = request_line.split(" ", 1)[0].upper() if request_line else ""

            # CORS preflight short-circuit — no auth or rate limit on OPTIONS.
            if method_verb == "OPTIONS":
                await self._send_preflight(writer)
                return

            if not self._rate_limit_ok(peer_ip):
                await self._send_http(
                    writer,
                    self._error_response(None, -32002, "Too Many Requests"),
                    status_code=429,
                )
                return

            auth_header = ""
            content_length = 0
            for line in header_lines:
                lower = line.lower()
                if lower.startswith("content-length:"):
                    try:
                        content_length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        content_length = 0
                elif lower.startswith("authorization:"):
                    auth_header = line.split(":", 1)[1].strip()

            if self.auth_token is not None:
                expected = f"Bearer {self.auth_token}"
                if not _constant_time_equals(auth_header, expected):
                    await self._send_http(
                        writer,
                        self._error_response(None, -32001, "Unauthorized"),
                        status_code=401,
                    )
                    return

            # read remaining body
            body = body_start
            while len(body) < content_length:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
                if not chunk:
                    break
                body += chunk

            # parse JSON-RPC request
            try:
                request = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = self._error_response(None, -32700, "Parse error")
                await self._send_http(writer, response)
                return

            result = await self._dispatch(request)
            await self._send_http(writer, result)

        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _send_http(
        self, writer: asyncio.StreamWriter, body: dict, status_code: int = 200,
    ) -> None:
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        status_line = f"HTTP/1.1 {status_code} {_http_reason(status_code)}\r\n".encode()
        headers = [
            status_line,
            b"Content-Type: application/json\r\n",
            b"Content-Length: " + str(len(body_bytes)).encode() + b"\r\n",
            b"Connection: close\r\n",
        ]
        headers.extend(self._cors_header_lines())
        headers.append(b"\r\n")
        writer.write(b"".join(headers) + body_bytes)
        await writer.drain()

    async def _send_preflight(self, writer: asyncio.StreamWriter) -> None:
        headers = [b"HTTP/1.1 204 No Content\r\n", b"Connection: close\r\n"]
        headers.extend(self._cors_header_lines())
        headers.append(b"\r\n")
        writer.write(b"".join(headers))
        await writer.drain()

    def _cors_header_lines(self) -> list[bytes]:
        if self.allow_origin is None:
            return []
        origin = self.allow_origin.encode("utf-8")
        return [
            b"Access-Control-Allow-Origin: " + origin + b"\r\n",
            b"Access-Control-Allow-Methods: POST, OPTIONS\r\n",
            b"Access-Control-Allow-Headers: Authorization, Content-Type\r\n",
            b"Access-Control-Max-Age: 600\r\n",
        ]

    async def _dispatch(self, request: dict) -> dict:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        methods = {
            "submit_transaction": self._submit_transaction,
            "get_block": self._get_block,
            "get_transaction": self._get_transaction,
            "get_account": self._get_account,
            "get_latest_block": self._get_latest_block,
            "get_mempool": self._get_mempool,
            "get_merkle_proof": self._get_merkle_proof,
            "get_node_info": self._get_node_info,
            "get_next_nonce": self._get_next_nonce,
        }

        handler = methods.get(method)
        if handler is None:
            return self._error_response(req_id, -32601, f"Method not found: {method}")

        try:
            result = await handler(params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as e:
            return self._error_response(req_id, -32000, str(e))

    def _error_response(self, req_id: int | None, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }

    # -- RPC method implementations --

    async def _submit_transaction(self, params: dict) -> dict:
        tx_dict = params.get("transaction")
        if tx_dict is None:
            raise ValueError("missing 'transaction' parameter")
        tx_hash_hex = await self.node.handle_new_transaction(tx_dict)
        return {"tx_hash": tx_hash_hex}

    async def _get_block(self, params: dict) -> dict:
        chain = self.node.chain
        if "height" in params:
            block = chain.get_block_by_height(params["height"])
        elif "hash" in params:
            block = chain.get_block_by_hash(bytes.fromhex(params["hash"]))
        else:
            raise ValueError("must specify 'height' or 'hash'")
        if block is None:
            raise ValueError("block not found")
        return block.to_dict()

    async def _get_transaction(self, params: dict) -> dict:
        tx_hash = bytes.fromhex(params["tx_hash"])
        tx = self.node.chain.get_transaction(tx_hash)
        if tx is None:
            tx = self.node.mempool.get_by_hash(tx_hash)
        if tx is None:
            raise ValueError("transaction not found")
        return tx.to_dict()

    async def _get_account(self, params: dict) -> dict:
        pubkey = bytes.fromhex(params["pubkey"])
        account = self.node.chain.state.get_account(pubkey)
        if account is None:
            return {"exists": False, "balance": 0, "nonce": 0}
        return {"exists": True, "balance": account.balance, "nonce": account.nonce}

    async def _get_latest_block(self, params: dict) -> dict:
        block = self.node.chain.tip
        if block is None:
            raise ValueError("chain not initialized")
        return block.to_dict()

    async def _get_mempool(self, params: dict) -> dict:
        pending = self.node.mempool.get_pending()
        return {"transactions": [tx.tx_hash().hex() for tx in pending]}

    async def _get_merkle_proof(self, params: dict) -> dict:
        tx_hash = bytes.fromhex(params["tx_hash"])
        block_hash_bytes = self.node.chain.tx_index.get(tx_hash)
        if block_hash_bytes is None:
            raise ValueError("transaction not in any confirmed block")
        block = self.node.chain.get_block_by_hash(block_hash_bytes)
        tx_hashes = [tx.tx_hash() for tx in block.transactions]
        index = tx_hashes.index(tx_hash)
        proof = merkle_proof(tx_hashes, index)
        return {
            "tx_hash": tx_hash.hex(),
            "block_hash": block_hash_bytes.hex(),
            "index": index,
            "proof": [{"hash": h.hex(), "is_left": left} for h, left in proof],
            "root": block.header.tx_merkle_root.hex(),
        }

    async def _get_node_info(self, params: dict) -> dict:
        return {
            "height": self.node.chain.height,
            "peer_count": len(self.node.p2p.peers) if self.node.p2p else 0,
            "mempool_size": self.node.mempool.size,
        }

    async def _get_next_nonce(self, params: dict) -> dict:
        pubkey = bytes.fromhex(params["pubkey"])
        return {"nonce": self.node.mempool.next_nonce(pubkey)}


_HTTP_REASONS = {
    200: "OK",
    204: "No Content",
    401: "Unauthorized",
    429: "Too Many Requests",
}


def _http_reason(status: int) -> str:
    return _HTTP_REASONS.get(status, "OK")


def _constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
