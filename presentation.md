# jiji — Demo Script

A guided walkthrough of every shipped feature. Each section is a copy-pasteable
block; comments above each command explain *what's new* and *why it matters*.

> Clean slate before you start:
> ```bash
rm -rf /tmp/alice.key /tmp/bob.key /tmp/miner.key
rm -rf /tmp/jiji-alice /tmp/jiji-bob /tmp/jiji-miner
> ```

---

## 1. Keygen — separate from the node

The CLI now ships a dedicated `keygen` subcommand (was implicit before). Keys
are 32-byte Ed25519 private keys, written hex-encoded.

```bash
jiji keygen --keyfile /tmp/alice.key
jiji keygen --keyfile /tmp/bob.key
jiji keygen --keyfile /tmp/miner.key

# Derive the public key (address) without touching a node:
jiji pubkey --keyfile /tmp/alice.key
```

---

## 2. Single-node bring-up with persistent storage

`--data-dir` persists the chain, world state, peer list, and the RPC bearer
token across restarts.

```bash
jiji node \
  --mine \
  --keyfile /tmp/miner.key \
  --data-dir /tmp/jiji-miner \
  --log-level INFO
```

In another terminal:

```bash
jiji status                       
jiji viewblocks 5                 
jiji viewblocks 3 --from 10         
```

---

## 3. `--lan` mode — one flag, four conveniences

Replaces a stack of manual flags. `--lan` simultaneously:

1. binds RPC to `0.0.0.0` (so other devices can reach it)
2. enables mDNS / DNS-SD peer discovery on `_jiji._tcp.local.`
3. auto-generates a 32-byte bearer token, persisted to `<data-dir>/rpc_token`
4. sets `Access-Control-Allow-Origin: *` so a browser client can call the RPC

```bash
jiji node --lan --mine \
  --keyfile /tmp/miner.key \
  --data-dir /tmp/jiji-miner
```

The startup banner prints the public key, the RPC address, and the token path.
Show it on screen — every subsequent CLI call auto-loads the token from
`<data-dir>/rpc_token` (no env var or flag needed for local use).

---

## 4. Multi-node mesh (manual peers + mDNS)

Spin up a second node on the same machine. With mDNS on, it discovers the
miner without `--peers`.

```bash
# Node B (no mining, mDNS only)
jiji node --lan \
  --keyfile /tmp/bob.key \
  --data-dir /tmp/jiji-bob \
  --port 9335 --rpc-port 9334
```

Or wire the peer explicitly (works without mDNS, e.g. across subnets):

```bash
jiji node \
  --keyfile /tmp/bob.key \
  --data-dir /tmp/jiji-bob \
  --port 9335 --rpc-port 9334 \
  --peers 127.0.0.1:9333
```

Confirm the mesh from either side:

```bash
jiji status --rpc-port 9334
```

---

## 5. Funding a fresh account

Brand-new keys have zero balance. Fund alice from the miner:

```bash
# Miner sends 100 to alice
jiji transfer \
  --keyfile /tmp/miner.key \
  --data-dir /tmp/jiji-miner \
  $(jiji pubkey --keyfile /tmp/alice.key) 100

# Wait one block, then check
sleep 16
jiji account --keyfile /tmp/alice.key --data-dir /tmp/jiji-miner
```

---

## 6. Posts, replies, and the 300-char limit

```bash
# Top-level post
jiji post "Hello from alice" \
  --keyfile /tmp/alice.key \
  --data-dir /tmp/jiji-miner

# Capture the tx hash from the output, then reply
PARENT=<tx_hash from previous output>
jiji post "Replying to my own post" \
  --reply-to $PARENT \
  --keyfile /tmp/alice.key \
  --data-dir /tmp/jiji-miner
```

Consensus rejects bodies > 300 chars. Show the failure:

```bash
jiji post "$(python3 -c 'print("x"*301)')" \
  --keyfile /tmp/alice.key \
  --data-dir /tmp/jiji-miner
```

---

## 7. Endorsements — the new transaction type

Endorse = public amplification + optional tip + optional 150-char note. The
tip moves tokens from endorser to the post's author at confirmation time.
Self-endorsement is rejected at the mempool and at block validation.

```bash
# Bob endorses alice's post with a 5-token tip
TARGET=<alice's post tx_hash>
jiji-rpc-call ... # see frontend, or use the web client

# (CLI endorse subcommand: see frontend client — endorsements are a first-class
# tx type in the protocol; the bundled CLI wallet ships post/transfer.)
```

Check the result by reading the block:

```bash
jiji viewblocks 1
# Look for a [endorse] line with target=<alice's hash> and tip=5
```

---

## 8. Web client — full wallet in the browser

Vanilla JS, no build step, signs with WebCrypto Ed25519.

```bash
cd frontend

# Same machine only:
python3 -m http.server 8080

# Multiple devices over LAN (HTTPS required by WebCrypto):
python3 serve.py
# Auto-generates a cert in .certs/ (mkcert if installed, else openssl).
# Prints reachable URLs for each LAN IP.
```

Walk through:

1. **Create wallet** — Ed25519 keypair generated in-browser, private key
   encrypted with PBKDF2 → AES-GCM, stored in `localStorage`. Never sent to
   the node.
2. **Connect node** — paste RPC URL + bearer token from `<data-dir>/rpc_token`.
3. **Feed** — scans last 50 blocks, then incremental. Cached in IndexedDB.
   Reorgs trigger a rescan.
4. **Compose** — post or reply (≤ 300 chars), signed locally.
5. **Endorse** — tap a post, optionally add a tip + message.
6. **Wallet** — balance, nonce, send transfers.
7. **Settings** — copy public key, switch nodes, export private key
   (passphrase-gated), lock, or wipe.

Safety bounds visible in [frontend/cache.js](frontend/cache.js): max 100-block
walk per refresh, max 500 cached posts (LRU by height).

---

## 9. RPC auth — bearer token everywhere off-loopback

Any non-loopback bind requires `Authorization: Bearer <hex>`. Demo it:

```bash
# This works (token auto-loaded from data-dir):
jiji status --data-dir /tmp/jiji-miner --rpc-host <lan-ip>

# This fails with 401:
curl -X POST http://<lan-ip>:9332 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"get_node_info","params":{},"id":1}'

# This works:
TOKEN=$(cat /tmp/jiji-miner/rpc_token)
curl -X POST http://<lan-ip>:9332 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","method":"get_node_info","params":{},"id":1}'
```

Token resolution precedence for the CLI: `--rpc-token` → `$JIJI_RPC_TOKEN` →
`<data-dir>/rpc_token`.

---

## 10. Rate limits, peer scoring, bans

On by default. The node defends itself against abusive peers and clients.

| Limit                     | Where it kicks in                            |
| ------------------------- | -------------------------------------------- |
| `PEER_MSG_PER_SEC` (+burst) | per inbound P2P connection (token bucket)  |
| `INBOUND_CONN_PER_MIN`    | per `/32` IP (sliding window)                |
| `RPC_REQ_PER_MIN`         | per IP on RPC; returns HTTP 429 on overrun   |
| Peer score → ban          | bad sigs / bad blocks; bans persist to disk  |

Bans are written to `<data-dir>/bans.json` and survive restart. For trusted
networks, escape hatches:

```bash
jiji node --lan --mine \
  --keyfile /tmp/miner.key \
  --data-dir /tmp/jiji-miner \
  --no-rate-limit                      # disable everything, OR
  --trust-ip 192.168.1.0/24,10.0.0.0/8 # exempt CIDRs from limits + bans
```

---

## 11. Block explorer in the terminal

`viewblocks` decodes every transaction type, not just hashes:

```bash
jiji viewblocks 5
```

Output includes coinbase recipients, post bodies (truncated), transfer
amounts, and endorsement tips/messages — useful during a live demo to show
what just landed on chain.

---

## 12. Stress test — fill a block, watch difficulty react

Push throughput up to the 256 KB / ~500-tx block limit and watch difficulty
adjust over the next 100-block window.

```bash
# Spam alice's account with posts (requires balance >= 80 * gas_fee)
for i in $(seq 1 80); do
  jiji post "stress $i" \
    --keyfile /tmp/alice.key \
    --data-dir /tmp/jiji-miner
done

jiji status --data-dir /tmp/jiji-miner
jiji viewblocks 3 --data-dir /tmp/jiji-miner
```

Compare `Difficulty:` across blocks before vs after the burst.

---

## 13. Tear-down

```bash
# Stop nodes (Ctrl-C in each terminal), then:
rm -rf /tmp/jiji-alice /tmp/jiji-bob /tmp/jiji-miner
rm -f  /tmp/alice.key  /tmp/bob.key  /tmp/miner.key
```

---

## Feature checklist (what this demo covers)

- [x] `keygen` / `pubkey` subcommands
- [x] `--data-dir` persistent storage (chain, state, token, bans)
- [x] `--lan` convenience flag (RPC bind + mDNS + auto-token + CORS)
- [x] mDNS / DNS-SD peer discovery (`_jiji._tcp.local.`)
- [x] Bearer-token RPC auth + auto-load from `<data-dir>/rpc_token`
- [x] CORS for browser clients
- [x] Per-peer / per-IP / per-RPC rate limits, peer scoring, persistent bans
- [x] `--no-rate-limit` and `--trust-ip` escape hatches
- [x] Endorsement transactions (tip + message, no self-endorse)
- [x] Replies via `--reply-to`
- [x] Transfers
- [x] `status`, `account`, `viewblocks` introspection commands
- [x] Vanilla-JS web client: in-browser keypair, encrypted localStorage,
      IndexedDB feed cache, reorg-aware scanner
- [x] HTTPS dev server with mkcert / openssl fallback for LAN demos
