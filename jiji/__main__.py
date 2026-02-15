"""CLI entry point for running a jiji node."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from jiji.core.config import DEFAULT_P2P_PORT, DEFAULT_RPC_PORT
from jiji.core.crypto import generate_keypair, public_key_from_private
from jiji.node import Node


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="jiji blockchain node")
    parser.add_argument("--host", default="0.0.0.0", help="P2P listen host")
    parser.add_argument("--port", type=int, default=DEFAULT_P2P_PORT, help="P2P port")
    parser.add_argument("--rpc-host", default="127.0.0.1", help="RPC listen host")
    parser.add_argument("--rpc-port", type=int, default=DEFAULT_RPC_PORT, help="RPC port")
    parser.add_argument("--mine", action="store_true", help="Enable mining")
    parser.add_argument("--peers", default="", help="Bootstrap peers (host:port,...)")
    parser.add_argument("--keyfile", default=None, help="Private key file (hex)")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def load_or_generate_keypair(keyfile: str | None) -> tuple[bytes, bytes]:
    """Load a private key from file, or generate and save a new one."""
    if keyfile and os.path.exists(keyfile):
        with open(keyfile, "r") as f:
            private_key = bytes.fromhex(f.read().strip())
        public_key = public_key_from_private(private_key)
        return private_key, public_key

    private_key, public_key = generate_keypair()

    if keyfile:
        with open(keyfile, "w") as f:
            f.write(private_key.hex())
        print(f"Generated keypair, saved to {keyfile}")

    return private_key, public_key


def parse_peers(peers_str: str) -> list[tuple[str, int]]:
    """Parse 'host:port,host:port,...' into a list of tuples."""
    if not peers_str.strip():
        return []
    result = []
    for entry in peers_str.split(","):
        entry = entry.strip()
        if ":" in entry:
            host, port_str = entry.rsplit(":", 1)
            result.append((host, int(port_str)))
    return result


async def run(args: argparse.Namespace) -> None:
    private_key, public_key = load_or_generate_keypair(args.keyfile)
    bootstrap_peers = parse_peers(args.peers)

    print(f"Public key: {public_key.hex()}")
    print(f"P2P: {args.host}:{args.port}")
    print(f"RPC: {args.rpc_host}:{args.rpc_port}")
    print(f"Mining: {'enabled' if args.mine else 'disabled'}")
    if bootstrap_peers:
        print(f"Bootstrap peers: {bootstrap_peers}")

    node = Node(
        private_key=private_key,
        public_key=public_key,
        p2p_host=args.host,
        p2p_port=args.port,
        rpc_host=args.rpc_host,
        rpc_port=args.rpc_port,
        mine=args.mine,
        bootstrap_peers=bootstrap_peers,
    )
    await node.start()

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await node.stop()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
