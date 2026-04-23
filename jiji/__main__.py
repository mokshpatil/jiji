"""CLI entry point for running a jiji node and wallet operations."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
import urllib.request
import urllib.error

from jiji.core.config import DEFAULT_P2P_PORT, DEFAULT_RPC_PORT
from jiji.core.crypto import generate_keypair, public_key_from_private
from jiji.core.transaction import Post, Transfer
from jiji.node import Node


# ---------------------------------------------------------------------------
# Keypair helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# RPC helper
# ---------------------------------------------------------------------------

def rpc_call(rpc_url: str, method: str, params: dict, token: str | None = None) -> dict:
    """Call a JSON-RPC method and return the result dict."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(rpc_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"RPC connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    if "error" in result:
        print(f"RPC error: {result['error']}", file=sys.stderr)
        sys.exit(1)
    return result.get("result", {})


def _cli_rpc_token(args: argparse.Namespace) -> str | None:
    """Resolve a bearer token for CLI RPC calls.

    Precedence:
      1. explicit ``--rpc-token``
      2. ``$JIJI_RPC_TOKEN``
      3. ``<--data-dir>/rpc_token`` (or ``~/.jiji/data/rpc_token`` as a fallback
         for the quickstart layout)
    """
    tok = getattr(args, "rpc_token", None)
    if tok:
        return tok.strip()
    env_tok = os.environ.get("JIJI_RPC_TOKEN")
    if env_tok:
        return env_tok.strip()
    data_dir = getattr(args, "data_dir", None) or os.path.expanduser("~/.jiji/data")
    path = os.path.join(data_dir, "rpc_token")
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    return None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_pubkey(args: argparse.Namespace) -> None:
    """Derive and print public key from a private key."""
    if args.keyfile:
        private_key = bytes.fromhex(open(args.keyfile).read().strip())
    elif args.privkey:
        private_key = bytes.fromhex(args.privkey)
    else:
        print("provide --keyfile or --privkey", file=sys.stderr)
        sys.exit(1)
    print(public_key_from_private(private_key).hex())


def cmd_keygen(args: argparse.Namespace) -> None:
    """Generate a new keypair and save to file."""
    if args.keyfile and os.path.exists(args.keyfile):
        print(f"Key file already exists: {args.keyfile}", file=sys.stderr)
        sys.exit(1)
    priv, pub = generate_keypair()
    if args.keyfile:
        with open(args.keyfile, "w") as f:
            f.write(priv.hex())
        print(f"Saved private key to {args.keyfile}")
    print(f"Public key: {pub.hex()}")


def cmd_account(args: argparse.Namespace) -> None:
    """Query an account's balance and nonce."""
    rpc_url = f"http://{args.rpc_host}:{args.rpc_port}"
    token = _cli_rpc_token(args)
    _, pub = load_or_generate_keypair(args.keyfile)
    pubkey_hex = pub.hex()
    result = rpc_call(rpc_url, "get_account", {"pubkey": pubkey_hex}, token=token)
    if result.get("exists"):
        print(f"pubkey:  {pubkey_hex}")
        print(f"balance: {result['balance']}")
        print(f"nonce:   {result['nonce']}")
    else:
        print(f"pubkey:  {pubkey_hex}")
        print("account not found (no transactions yet)")


def cmd_post(args: argparse.Namespace) -> None:
    """Submit a post transaction."""
    rpc_url = f"http://{args.rpc_host}:{args.rpc_port}"
    token = _cli_rpc_token(args)
    priv, pub = load_or_generate_keypair(args.keyfile)

    # Get next nonce (accounts for pending txs in mempool)
    result = rpc_call(rpc_url, "get_next_nonce", {"pubkey": pub.hex()}, token=token)
    nonce = result.get("nonce", 0)

    tx = Post(
        author=pub,
        nonce=nonce,
        timestamp=int(time.time()),
        body=args.body,
        reply_to=bytes.fromhex(args.reply_to) if args.reply_to else None,
        gas_fee=args.fee,
    )
    tx.sign_tx(priv)

    result = rpc_call(rpc_url, "submit_transaction", {"transaction": tx.to_dict()}, token=token)
    print(f"submitted: {result.get('tx_hash', '')}")


def cmd_transfer(args: argparse.Namespace) -> None:
    """Submit a transfer transaction."""
    rpc_url = f"http://{args.rpc_host}:{args.rpc_port}"
    token = _cli_rpc_token(args)
    priv, pub = load_or_generate_keypair(args.keyfile)

    # Get next nonce (accounts for pending txs in mempool)
    result = rpc_call(rpc_url, "get_next_nonce", {"pubkey": pub.hex()}, token=token)
    nonce = result.get("nonce", 0)

    tx = Transfer(
        sender=pub,
        recipient=bytes.fromhex(args.to),
        amount=args.amount,
        nonce=nonce,
        gas_fee=args.fee,
    )
    tx.sign_tx(priv)

    result = rpc_call(rpc_url, "submit_transaction", {"transaction": tx.to_dict()}, token=token)
    print(f"submitted: {result.get('tx_hash', '')}")


def cmd_viewblocks(args: argparse.Namespace) -> None:
    """Display N blocks starting from a given height (default: latest)."""
    rpc_url = f"http://{args.rpc_host}:{args.rpc_port}"
    token = _cli_rpc_token(args)
    tip = rpc_call(rpc_url, "get_latest_block", {}, token=token)
    tip_height = tip["header"]["height"]

    top = args.start if args.start is not None else tip_height
    top = min(top, tip_height)
    bottom = max(0, top - args.n + 1)
    for height in range(top, bottom - 1, -1):
        block = rpc_call(rpc_url, "get_block", {"height": height}, token=token)
        h = block["header"]
        print(f"Block #{h['height']}  miner={h['miner'][:16]}...  time={h['timestamp']}  txs={h['tx_count']}")
        print(f"Difficulty: {h['difficulty']}")
        for tx in block["transactions"]:
            tx_type = tx.get("tx_type", "unknown")
            if tx_type == "coinbase":
                print(f"  [coinbase] recipient={tx['recipient'][:16]}...  amount={tx['amount']}")
            elif tx_type == "post":
                print(f"  [post]     author={tx['author'][:16]}...  body={tx['body'][:60]!r}")
            elif tx_type == "transfer":
                print(f"  [transfer] sender={tx['sender'][:16]}...  recipient={tx['recipient'][:16]}...  amount={tx['amount']}")
            elif tx_type == "endorse":
                tip = f"  tip={tx['amount']}" if tx.get("amount") else ""
                msg = f"  msg={tx['message']!r}" if tx.get("message") else ""
                print(f"  [endorse]  author={tx['author'][:16]}...  target={tx['target'][:16]}...{tip}{msg}")
            else:
                print(f"  [{tx_type}] {tx}")
        print()


def cmd_status(args: argparse.Namespace) -> None:
    """Show node status."""
    rpc_url = f"http://{args.rpc_host}:{args.rpc_port}"
    token = _cli_rpc_token(args)
    info = rpc_call(rpc_url, "get_node_info", {}, token=token)
    tip = rpc_call(rpc_url, "get_latest_block", {}, token=token)
    print(f"height:       {info.get('height', 'n/a')}")
    print(f"peers:        {info.get('peer_count', 0)}")
    print(f"mempool:      {info.get('mempool_size', 0)} txs")
    if tip:
        h = tip.get("header", {})
        print(f"tip hash:     {tip.get('hash', '')[:16]}...")
        print(f"tip miner:    {h.get('miner', '')[:16]}...")
        print(f"tip time:     {h.get('timestamp', '')}")


# ---------------------------------------------------------------------------
# Node runner
# ---------------------------------------------------------------------------

def _resolve_rpc_token(args: argparse.Namespace) -> str | None:
    """Resolve --rpc-token into a concrete token (or None if auth disabled).

    "auto" generates a fresh 32-byte hex token and persists it to
    {data_dir}/rpc_token when data_dir is set, so the token survives restarts.
    Any other string is used verbatim.
    """
    token_arg = getattr(args, "rpc_token", None)
    if not token_arg:
        return None
    if token_arg != "auto":
        return token_arg
    token_path: str | None = None
    if args.data_dir:
        token_path = os.path.join(args.data_dir, "rpc_token")
        if os.path.exists(token_path):
            with open(token_path, "r") as f:
                existing = f.read().strip()
                if existing:
                    return existing
    token = secrets.token_hex(32)
    if token_path:
        os.makedirs(args.data_dir, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(token)
        os.chmod(token_path, 0o600)
        print(f"RPC token written to {token_path}")
    else:
        print(f"RPC token (ephemeral): {token}")
    return token


def _apply_lan_defaults(args: argparse.Namespace) -> None:
    """--lan sets non-loopback RPC bind, mDNS, bearer token, and CORS."""
    if not getattr(args, "lan", False):
        return
    # Only override user-specified values when they match the default loopback.
    if args.rpc_host == "127.0.0.1":
        args.rpc_host = "0.0.0.0"
    if getattr(args, "mdns", None) is None:
        args.mdns = True
    if not getattr(args, "rpc_token", None):
        args.rpc_token = "auto"
    if getattr(args, "rpc_allow_origin", None) is None:
        args.rpc_allow_origin = "*"


async def run_node(args: argparse.Namespace) -> None:
    private_key, public_key = load_or_generate_keypair(args.keyfile)
    bootstrap_peers = parse_peers(args.peers)

    _apply_lan_defaults(args)
    rpc_token = _resolve_rpc_token(args)
    mdns_enabled = bool(getattr(args, "mdns", False))
    rate_limit = bool(getattr(args, "rate_limit", True))
    trust_raw = getattr(args, "trust_ip", "") or ""
    trusted_cidrs = tuple(c.strip() for c in trust_raw.split(",") if c.strip())

    print(f"Public key: {public_key.hex()}")
    print(f"P2P: {args.host}:{args.port}")
    print(f"RPC: {args.rpc_host}:{args.rpc_port}")
    print(f"Mining: {'enabled' if args.mine else 'disabled'}")
    print(f"Storage: {args.data_dir or 'in-memory'}")
    print(f"mDNS discovery: {'on' if mdns_enabled else 'off'}")
    print(f"RPC auth: {'bearer token required' if rpc_token else 'none'}")
    print(f"Rate limits: {'on' if rate_limit else 'off'}")
    if trusted_cidrs:
        print(f"Trusted CIDRs: {', '.join(trusted_cidrs)}")
    if getattr(args, "rpc_allow_origin", None):
        print(f"RPC CORS allow-origin: {args.rpc_allow_origin}")
    if bootstrap_peers:
        print(f"Bootstrap peers: {bootstrap_peers}")

    node = Node(
        private_key=private_key,
        public_key=public_key,
        data_dir=args.data_dir,
        p2p_host=args.host,
        p2p_port=args.port,
        rpc_host=args.rpc_host,
        rpc_port=args.rpc_port,
        mine=args.mine,
        bootstrap_peers=bootstrap_peers,
        rpc_auth_token=rpc_token,
        rpc_allow_origin=getattr(args, "rpc_allow_origin", None),
        mdns=mdns_enabled,
        rate_limit=rate_limit,
        trusted_cidrs=trusted_cidrs,
    )
    await node.start()

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await node.stop()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiji",
        description="jiji blockchain node and wallet",
    )
    # Shared RPC options used by wallet subcommands
    rpc_flags = argparse.ArgumentParser(add_help=False)
    rpc_flags.add_argument("--rpc-host", default="127.0.0.1", metavar="HOST")
    rpc_flags.add_argument("--rpc-port", type=int, default=DEFAULT_RPC_PORT, metavar="PORT")
    rpc_flags.add_argument("--keyfile", default=None, metavar="FILE",
                           help="Private key file (hex)")
    rpc_flags.add_argument("--rpc-token", default=None, metavar="TOKEN",
                           help="Bearer token for RPC auth. Overrides "
                                "$JIJI_RPC_TOKEN and <data-dir>/rpc_token.")
    rpc_flags.add_argument("--data-dir", default=None, metavar="DIR",
                           help="Node data dir; used to auto-load the bearer "
                                "token from <data-dir>/rpc_token.")

    subparsers = parser.add_subparsers(dest="command")

    # --- node (default when no subcommand given) ---
    node_p = subparsers.add_parser("node", help="Run a full node")
    node_p.add_argument("--host", default="0.0.0.0")
    node_p.add_argument("--port", type=int, default=DEFAULT_P2P_PORT)
    node_p.add_argument("--rpc-host", default="127.0.0.1")
    node_p.add_argument("--rpc-port", type=int, default=DEFAULT_RPC_PORT)
    node_p.add_argument("--mine", action="store_true", help="Enable mining")
    node_p.add_argument("--peers", default="", help="Bootstrap peers (host:port,...)")
    node_p.add_argument("--keyfile", default=None, help="Private key file (hex)")
    node_p.add_argument("--data-dir", default=None,
                        help="Data directory for persistent storage")
    node_p.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    node_p.add_argument("--lan", action="store_true",
                        help="Convenience: bind RPC to 0.0.0.0, enable mDNS discovery, "
                             "auto-generate bearer token, and set CORS allow-origin=*")
    node_p.add_argument("--mdns", dest="mdns", action="store_true", default=None,
                        help="Enable mDNS/DNS-SD LAN peer discovery")
    node_p.add_argument("--no-mdns", dest="mdns", action="store_false",
                        help="Disable mDNS (overrides --lan)")
    node_p.add_argument("--rpc-token", default=None, metavar="TOKEN",
                        help="Bearer token required for RPC requests "
                             "(use 'auto' to generate and persist one)")
    node_p.add_argument("--rpc-allow-origin", default=None, metavar="ORIGIN",
                        help="CORS Access-Control-Allow-Origin value (e.g. '*')")
    node_p.add_argument("--no-rate-limit", dest="rate_limit",
                        action="store_false", default=True,
                        help="Disable all per-peer and per-IP rate limits (trusted network)")
    node_p.add_argument("--trust-ip", default="", metavar="CIDR,CIDR,...",
                        help="Comma-separated CIDR ranges exempt from rate limits and bans")

    # --- viewblocks ---
    vb_p = subparsers.add_parser("viewblocks", parents=[rpc_flags],
                                  help="Display N blocks from a given height")
    vb_p.add_argument("n", type=int, nargs="?", default=5, metavar="N",
                      help="Number of blocks to show (default: 5)")
    vb_p.add_argument("--from", dest="start", type=int, default=None, metavar="HEIGHT",
                      help="Start from this block height (default: latest)")

    # --- pubkey ---
    pk_p = subparsers.add_parser("pubkey", help="Derive public key from private key")
    pk_p.add_argument("--keyfile", default=None, metavar="FILE",
                      help="Private key file (hex)")
    pk_p.add_argument("--privkey", default=None, metavar="HEX",
                      help="Private key as hex string")

    # --- keygen ---
    kg_p = subparsers.add_parser("keygen", help="Generate a new keypair")
    kg_p.add_argument("--keyfile", default=None, metavar="FILE",
                      help="Save private key to this file")

    # --- status ---
    st_p = subparsers.add_parser("status", parents=[rpc_flags],
                                  help="Show node status")

    # --- account ---
    ac_p = subparsers.add_parser("account", parents=[rpc_flags],
                                  help="Show account balance and nonce")

    # --- post ---
    po_p = subparsers.add_parser("post", parents=[rpc_flags],
                                  help="Publish a post")
    po_p.add_argument("body", help="Post body text (max 300 chars)")
    po_p.add_argument("--reply-to", default=None, metavar="TX_HASH",
                      help="Hash of post being replied to")
    po_p.add_argument("--fee", type=int, default=1, help="Gas fee (default: 1)")

    # --- transfer ---
    tr_p = subparsers.add_parser("transfer", parents=[rpc_flags],
                                  help="Transfer tokens to another account")
    tr_p.add_argument("to", metavar="PUBKEY", help="Recipient public key (hex)")
    tr_p.add_argument("amount", type=int, help="Amount to transfer")
    tr_p.add_argument("--fee", type=int, default=1, help="Gas fee (default: 1)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "viewblocks":
        cmd_viewblocks(args)
        return

    if args.command == "pubkey":
        cmd_pubkey(args)
        return

    if args.command == "keygen":
        cmd_keygen(args)
        return

    if args.command == "status":
        cmd_status(args)
        return

    if args.command == "account":
        cmd_account(args)
        return

    if args.command == "post":
        cmd_post(args)
        return

    if args.command == "transfer":
        cmd_transfer(args)
        return

    # Default: run the node (either `jiji node ...` or bare `jiji ...`)
    if args.command not in (None, "node"):
        parser.print_help()
        sys.exit(1)

    # If called as `jiji node`, args has log_level; otherwise use INFO
    log_level = getattr(args, "log_level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # For backward compat: if no subcommand, treat all args as node args
    if args.command is None:
        # re-parse as node args
        node_parser = argparse.ArgumentParser()
        node_parser.add_argument("--host", default="0.0.0.0")
        node_parser.add_argument("--port", type=int, default=DEFAULT_P2P_PORT)
        node_parser.add_argument("--rpc-host", default="127.0.0.1")
        node_parser.add_argument("--rpc-port", type=int, default=DEFAULT_RPC_PORT)
        node_parser.add_argument("--mine", action="store_true")
        node_parser.add_argument("--peers", default="")
        node_parser.add_argument("--keyfile", default=None)
        node_parser.add_argument("--data-dir", default=None)
        node_parser.add_argument("--log-level", default="INFO",
                                  choices=["DEBUG", "INFO", "WARNING", "ERROR"])
        node_parser.add_argument("--lan", action="store_true")
        node_parser.add_argument("--mdns", dest="mdns", action="store_true", default=None)
        node_parser.add_argument("--no-mdns", dest="mdns", action="store_false")
        node_parser.add_argument("--rpc-token", default=None)
        node_parser.add_argument("--rpc-allow-origin", default=None)
        node_parser.add_argument("--no-rate-limit", dest="rate_limit",
                                  action="store_false", default=True)
        node_parser.add_argument("--trust-ip", default="")
        args = node_parser.parse_args()
        logging.basicConfig(
            level=getattr(logging, args.log_level),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    asyncio.run(run_node(args))


if __name__ == "__main__":
    main()
