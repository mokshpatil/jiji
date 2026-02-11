# Jiji — Blockchain-Based Immutable Social Protocol

Jiji is a decentralized, blockchain-based social protocol where users publish
signed posts to an immutable, append-only ledger. Identity is pseudonymous
(cryptographic keypairs with no link to real-world identity). The protocol
handles data integrity, ordering, and economic incentives. Presentation,
moderation, and recommendation are delegated entirely to independent client
applications that anyone can build.

## Core Principles

- **Immutability**: confirmed posts cannot be altered or deleted
- **Pseudonymity**: identity = public key, no registration authority
- **Separation of existence and visibility**: the chain stores everything;
  clients choose what to display
- **Permissionless participation**: anyone can mine, post, or build a client
- **Economic spam resistance**: gas fees create natural friction

---

## Table of Contents

1. [Identity Model](#1-identity-model)
2. [Transaction Types](#2-transaction-types)
3. [Block Structure](#3-block-structure)
4. [World State](#4-world-state)
5. [Consensus: Proof of Work](#5-consensus-proof-of-work)
6. [Networking and P2P Protocol](#6-networking-and-p2p-protocol)
7. [Node Types](#7-node-types)
8. [Client Architecture](#8-client-architecture)
9. [Security Considerations](#9-security-considerations)
10. [Serialization Format](#10-serialization-format)
11. [Data Flow](#11-data-flow)
12. [Trade-Off Analysis](#12-trade-off-analysis)
13. [Protocol Parameters](#13-protocol-parameters)
14. [Development Roadmap](#14-development-roadmap)

---

## 1. Identity Model

Users generate an **Ed25519** keypair:

- **Private key**: 32 bytes, kept secret, used to sign transactions
- **Public key**: 32 bytes, serves as the user's identity/address
- **Address format**: hex-encoded public key (64 characters)

There is no registration, no username system, and no central authority.
A user "exists" the moment they submit their first transaction. Clients may
implement vanity names, profile metadata, or naming overlays,
but none of this lives on-chain.

Ed25519 was chosen for: fast signing and verification, small signatures (64 bytes),
small keys (32 bytes), and resistance to timing attacks.

---

## 2. Transaction Types

Every action on the chain is a transaction. All transactions are
content-addressed: the transaction's canonical ID is `SHA256(serialize(tx))`,
computed over all fields except the signature.

### 2.1 Post Transaction

```
{
    "tx_type":   "post",
    "author":    <ed25519 public key, 32 bytes>,
    "nonce":     <uint64, sequential per account>,
    "timestamp": <uint64, unix timestamp, client-reported>,
    "body":      <utf-8 string, max 300 characters>,
    "reply_to":  <tx_hash (32 bytes) or null>,
    "gas_fee":   <uint64, tokens offered to miner>,
    "signature": <ed25519 signature, 64 bytes>
}
```

**Field rationale:**

| Field | Purpose |
|-------|---------|
| `tx_type` | Identifies transaction kind for validation and indexing |
| `author` | The public key IS the identity; anyone can verify the signature |
| `nonce` | Sequential counter per account; prevents replay attacks; validator rejects if nonce != account's current nonce + 1 |
| `timestamp` | Client-reported, NOT authoritative; exists for display hints; the block timestamp provides canonical ordering |
| `body` | Post content, plain text, max 300 characters enforced by consensus |
| `reply_to` | Null for top-level posts; tx_hash of parent for replies; creates a tree structure (threads) derivable entirely from chain data |
| `gas_fee` | User-set, must be >= protocol minimum floor; miners prioritize higher fees when blocks are full |
| `signature` | Ed25519 signature over serialized tx (all fields except signature); proves authorship |

Estimated size: ~450-700 bytes per post transaction.

### 2.2 Endorsement Transaction

```
{
    "tx_type":   "endorse",
    "author":    <ed25519 public key, 32 bytes>,
    "nonce":     <uint64>,
    "target":    <tx_hash of post being endorsed, 32 bytes>,
    "amount":    <uint64, optional tip, 0 if no donation>,
    "message":   <utf-8 string, max 150 characters, may be empty>,
    "gas_fee":   <uint64>,
    "signature": <ed25519 signature, 64 bytes>
}
```

| Field | Purpose |
|-------|---------|
| `target` | Must reference a valid, confirmed post tx_hash; validators check existence; this is the "retweet" semantic (public amplification) |
| `amount` | Optional token donation transferred from endorser to target post's author upon block confirmation; 0 if no tip |
| `message` | Short context note from the endorser; capped at 150 characters; distinguishes endorsements from replies (which are full posts) |

**Validation rules:**
- `target` must reference an existing post (not an endorsement or transfer)
- `amount` must be <= sender's balance minus gas_fee
- One account may endorse the same post multiple times (each is a separate
  transaction with its own cost)

Estimated size: ~300-500 bytes per endorsement transaction.

### 2.3 Transfer Transaction

```
{
    "tx_type":   "transfer",
    "sender":    <ed25519 public key, 32 bytes>,
    "recipient": <ed25519 public key, 32 bytes>,
    "amount":    <uint64>,
    "nonce":     <uint64>,
    "gas_fee":   <uint64>,
    "signature": <ed25519 signature, 64 bytes>
}
```

**Validation rules:**
- `amount + gas_fee` must be <= sender's balance
- `recipient` need not be a known account (transfers can create new accounts)
- Sender and recipient must differ

Estimated size: ~180-220 bytes per transfer transaction.

### 2.4 Coinbase Transaction (Block Reward)

```
{
    "tx_type":   "coinbase",
    "recipient": <miner's public key>,
    "amount":    <block reward amount, protocol-determined>,
    "height":    <block height, must match the block it appears in>
}
```

This is the sole mechanism of new token creation. Exactly one coinbase
transaction appears as the first transaction in every block. It has no sender,
no nonce, no gas fee, and no signature (its validity is derived from the block's
proof of work). The `height` field prevents the same coinbase tx from being
valid in two different blocks.

---

## 3. Block Structure

```
Block {
    header: {
        "version":        <uint8>,
        "height":         <uint64>,
        "prev_hash":      <SHA256 hash of previous block header, 32 bytes>,
        "timestamp":      <uint64, miner-set, unix timestamp>,
        "miner":          <ed25519 public key of block producer>,
        "difficulty":     <uint64, current difficulty target>,
        "nonce":          <uint64, PoW solution>,
        "tx_merkle_root": <SHA256 merkle root of transaction hashes>,
        "state_root":     <SHA256 merkle root of world state>,
        "tx_count":       <uint16, number of transactions in body>
    },
    body: {
        "transactions": [coinbase_tx, tx1, tx2, ...]
    }
}

block_hash = SHA256(serialize(header))
```

### Header Fields

| Field | Purpose |
|-------|---------|
| `version` | Protocol versioning for future upgrades; nodes reject blocks with unsupported versions |
| `height` | Sequential block number; combined with prev_hash, gives unambiguous chain position |
| `prev_hash` | Cryptographic link to previous block; changing any historical block changes its hash, which invalidates all subsequent prev_hash references |
| `timestamp` | Miner-reported; consensus rules enforce it must be greater than the median of the last 11 blocks and not more than 2 minutes in the future |
| `miner` | Identifies who mined the block; the coinbase tx must pay this key |
| `difficulty` | The target difficulty for this block's PoW; every node independently computes expected difficulty and rejects blocks that don't match |
| `nonce` | The value the miner iterated to satisfy the PoW condition |
| `tx_merkle_root` | Merkle tree root over ordered transaction hashes; enables compact proofs that a transaction is included in a block (O(log n) proof size) |
| `state_root` | Merkle tree root over the world state after applying all transactions in this block; enables light clients to verify any account's balance without replaying the chain |
| `tx_count` | Metadata for quick block assessment; must match actual transaction count in body |

### PoW Validity Condition

```
SHA256(serialize(header)) < 2^(256 - difficulty)
```

The miner increments `nonce` (and optionally varies `timestamp`) until the
block hash, interpreted as a 256-bit integer, falls below the difficulty-derived
target. Verification is a single hash computation.

### Block Size Limit

**Maximum block size: 256 KB** (262,144 bytes), measured as the serialized size
of the full block (header + body).

- At ~500 bytes average per transaction, this allows ~500 transactions per block
- At 15-second block time, this yields ~33 TPS sustained throughput
- 256 KB propagates quickly even on modest connections
- Keeps storage growth manageable: ~1.5 MB/min, ~2.1 GB/day at full capacity
- Can be increased via protocol upgrade if demand warrants it

### Genesis Block

Block at height 0 with `prev_hash = 0x00...00`. Contains a single coinbase
transaction. Mining difficulty starts extremely low so that early participants
can mine on consumer hardware. This is the token bootstrapping mechanism:
early miners accumulate tokens and distribute them through the economy via
transfers, client faucets, or direct spending.

---

## 4. World State

Account-based model. The world state is a mapping from public keys to account
records:

```
state = {
    <pubkey>: {
        "balance": <uint64>,
        "nonce":   <uint64>
    }
}
```

### State Transitions

When a block is applied to the state:

1. **Coinbase**: `state[miner].balance += block_reward`
2. **Post**: `state[author].balance -= gas_fee`,
   `state[miner].balance += gas_fee`,
   `state[author].nonce += 1`
3. **Endorse**: `state[author].balance -= (gas_fee + amount)`,
   `state[miner].balance += gas_fee`,
   `state[target_author].balance += amount`,
   `state[author].nonce += 1`
4. **Transfer**: `state[sender].balance -= (amount + gas_fee)`,
   `state[recipient].balance += amount`,
   `state[miner].balance += gas_fee`,
   `state[sender].nonce += 1`

If any transaction would result in a negative balance, the entire block is
invalid.

### State Storage

The state is stored as a **Merkle Patricia Trie** (or a simpler sorted Merkle
tree for the prototype). This data structure:

- Allows O(log n) lookups and updates
- Produces a single root hash (the `state_root` in the block header)
- Enables compact proofs: "account X has balance Y" can be proven with a
  Merkle proof of O(log n) hashes, verifiable against the state_root

---

## 5. Consensus: Proof of Work

### Mining Process

1. Miner collects pending transactions from the mempool
2. Miner orders transactions (typically by gas fee, descending) and validates
   each one against current state
3. Miner constructs a candidate block with a coinbase transaction paying
   themselves
4. Miner iterates `nonce` values until `SHA256(header) < difficulty_target`
5. Miner broadcasts the solved block to all peers

### Difficulty Adjustment

**Window: every 100 blocks (~25 minutes at target pace)**

```
expected_time = 100 * 15 seconds = 1500 seconds
actual_time   = timestamp(block N) - timestamp(block N-100)

adjustment_ratio = expected_time / actual_time

# Clamp to prevent extreme swings
adjustment_ratio = clamp(adjustment_ratio, 0.25, 4.0)

new_difficulty = old_difficulty * adjustment_ratio
```

If blocks came too fast (more miners joined), difficulty increases. If blocks
came too slow (miners left), difficulty decreases. The clamp prevents more than
a 4x change per window, ensuring stability.

### Fork Resolution

When two miners solve a block at the same height simultaneously, the network
temporarily forks. Resolution rule: **longest chain wins** (the chain with the
most cumulative proof of work). Nodes always switch to the longest valid chain
they are aware of.

Transactions in orphaned blocks return to the mempool and can be included in
future blocks.

### Block Reward Schedule

A halving schedule controls token supply growth:

```
reward(height) = INITIAL_REWARD / (2 ^ (height / HALVING_INTERVAL))
```

Starting parameters (tunable):
- `INITIAL_REWARD`: 50 tokens
- `HALVING_INTERVAL`: 210,000 blocks (~36.5 days at 15s/block)
- `MINIMUM_GAS_FEE`: 1 token (protocol floor)

As block rewards diminish over time, gas fees become the primary miner
incentive. This creates a natural transition from inflationary bootstrapping
to a fee-driven economy.

---

## 6. Networking and P2P Protocol

### Peer Discovery

Nodes maintain a peer list. Discovery mechanisms:

- **Bootstrap nodes**: hardcoded addresses of well-known nodes that new nodes
  connect to first
- **Peer exchange**: nodes share their peer lists with each other periodically
- **DNS seeds** (optional, for production): DNS records that resolve to active
  node addresses

### Gossip Protocol

All propagation uses a gossip model:

**Transaction gossip:**
1. User submits signed transaction to any connected node
2. Node validates the transaction (signature, nonce, balance, format)
3. If valid, node adds it to the local mempool and forwards to all peers
4. Peers that already have the transaction (by tx_hash) ignore it
5. Peers that don't have it validate and propagate further

**Block gossip:**
1. Miner solves a block and broadcasts it to all peers
2. Receiving node validates the block (PoW, all transactions, state transitions)
3. If valid and extends the longest chain, node appends it and forwards to peers
4. If valid but does not extend longest chain, node stores it as an orphan
5. If invalid, node drops it and may penalize the sender

### Message Types

| Message | Description |
|---------|-------------|
| `HANDSHAKE` | Exchange version, height, genesis hash |
| `PEERS_REQUEST` | Request peer list |
| `PEERS_RESPONSE` | Return known peers |
| `TX_ANNOUNCE` | Announce a new transaction (send tx_hash) |
| `TX_REQUEST` | Request full transaction by hash |
| `TX_RESPONSE` | Return full transaction |
| `BLOCK_ANNOUNCE` | Announce a new block (send block_hash + height) |
| `BLOCK_REQUEST` | Request full block by hash or height |
| `BLOCK_RESPONSE` | Return full block |
| `SYNC_REQUEST` | Request blocks from height N to M |
| `SYNC_RESPONSE` | Return batch of blocks |

### Mempool

Each node maintains a local mempool of unconfirmed transactions:

- Maximum size: configurable (default 10,000 transactions)
- Eviction policy: lowest gas_fee transactions evicted first when full
- Transactions are removed when included in a confirmed block
- Transactions are returned to the mempool if their block gets orphaned
- Stale transactions (nonce too old) are periodically purged

---

## 7. Node Types

### Full Archival Node
- Stores the entire blockchain from genesis
- Stores the complete current world state
- Validates every block and transaction
- Serves historical data to peers and clients
- Can mine (optional)

### Pruned Full Node
- Validates everything like an archival node
- Discards block bodies older than a threshold (e.g., 10,000 blocks)
- Retains all block headers (for chain verification)
- Retains the current world state
- Cannot serve old transaction data, but can verify current state

### Light Client
- Downloads only block headers
- Trusts the longest chain's PoW without validating every transaction
- Requests specific data (account balance, transaction inclusion) with Merkle
  proofs from full nodes
- Cannot mine or validate independently
- Suitable for end-user devices

---

## 8. Client Architecture

Clients are independent applications that interface with the protocol layer.
The protocol provides the data; clients provide the experience. Anyone can
build a client.

```
+------------------------------------------------------+
|                   PROTOCOL LAYER                      |
|   Blockchain nodes expose a JSON-RPC API:             |
|                                                       |
|   submit_transaction(signed_tx) -> tx_hash            |
|   get_block(height | hash)      -> block              |
|   get_transaction(tx_hash)      -> transaction        |
|   get_account(pubkey)           -> {balance, nonce}   |
|   get_latest_block()            -> block              |
|   get_mempool()                 -> [tx_hash, ...]     |
|   get_merkle_proof(tx_hash)     -> proof              |
|   get_state_proof(pubkey)       -> proof              |
+---------------------------+--------------------------+
                            |
                            | JSON-RPC over TCP
                            |
+---------------------------v--------------------------+
|                    CLIENT LAYER                       |
|                                                       |
|   +----------+   +------------+   +--------------+    |
|   | Indexer   |   | Moderation |   | Recommender  |    |
|   | chain->DB |   |   Engine   |   |  Algorithm   |    |
|   +-----+----+   +-----+------+   +------+-------+    |
|         |              |                  |            |
|         +---------+----+------------------+            |
|              +----v-----+                              |
|              |  UI / UX  |                              |
|              +----------+                              |
+------------------------------------------------------+
```

### Indexer

Reads blocks from the node API as they are confirmed, extracts transactions,
and populates a local queryable database (SQLite for the prototype):

```sql
CREATE TABLE posts (
    tx_hash        TEXT PRIMARY KEY,
    author         TEXT NOT NULL,
    body           TEXT NOT NULL,
    block_height   INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL,
    reply_to       TEXT,
    gas_fee        INTEGER NOT NULL
);

CREATE TABLE endorsements (
    tx_hash        TEXT PRIMARY KEY,
    author         TEXT NOT NULL,
    target         TEXT NOT NULL,
    amount         INTEGER NOT NULL DEFAULT 0,
    message        TEXT,
    block_height   INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL
);

CREATE TABLE accounts (
    pubkey         TEXT PRIMARY KEY,
    balance        INTEGER NOT NULL DEFAULT 0,
    nonce          INTEGER NOT NULL DEFAULT 0,
    first_seen     INTEGER
);

CREATE INDEX idx_posts_author ON posts(author);
CREATE INDEX idx_posts_timestamp ON posts(block_timestamp DESC);
CREATE INDEX idx_posts_reply ON posts(reply_to);
CREATE INDEX idx_endorsements_target ON endorsements(target);
CREATE INDEX idx_endorsements_author ON endorsements(author);
```

### Moderation Engine

Client-side content filtering. The blockchain includes everything; the client
decides what to show. Approaches:

- **Blocklists**: maintained lists of addresses posting illegal or harmful content;
  can be community-curated or client-specific
- **Keyword filtering**: regex or ML-based content scanning
- **External moderation APIs**: client subscribes to a moderation service
- **User-controlled muting**: individual users block specific addresses

Different clients can have different moderation policies. Users choose the
client whose approach aligns with their preferences.

### Recommendation and Feed Algorithms

Entirely client-side. Possible approaches:

- **Chronological**: all posts in time order
- **Endorsement-weighted**: posts with more endorsements rank higher
- **Implicit follow graph**: user endorses authors they like; client shows
  posts from endorsed authors
- **Topic clustering**: NLP on post bodies to group content
- **Collaborative filtering**: users with similar endorsement patterns get
  recommended each other's endorsed content
- **Trending**: posts with the fastest-growing endorsement count in a time
  window

### Client Monetization

Clients are free to monetize independently of the protocol:
- Display ads alongside content
- Offer premium features (advanced algorithms, analytics)
- Distribute free tokens to new users (funded by ad revenue or other means)
- Charge subscription fees for enhanced features

None of this affects the protocol.

---

## 9. Security Considerations

### Spam Prevention
- Minimum gas fee enforced by consensus (economic floor)
- Market-driven fees rise during high activity
- Block size limit caps throughput, making sustained spam expensive

### Sybil Attacks
- Creating many accounts is free, but each account needs tokens to act
- Tokens cost real resources (mining or purchasing)
- No protocol-level benefit to having many accounts vs one account

### 51% Attack
- An attacker with >50% mining power can rewrite recent history
- Mitigation: for high-value operations (large transfers), wait for multiple
  confirmations (e.g., 6 blocks = 90 seconds)
- Social content is lower stakes than financial double-spending

### Eclipse Attack
- An attacker surrounds a node with malicious peers, feeding it a fake chain
- Mitigation: connect to diverse peers, use bootstrap nodes, verify PoW

### Transaction Replay
- Nonce field prevents replay: each transaction from an account has a unique
  sequential nonce; re-broadcasting an old transaction fails nonce validation

### Timestamp Manipulation
- Miners set block timestamps, creating potential for manipulation
- Mitigation: consensus rule requires timestamp > median of last 11 blocks
  and < current time + 120 seconds

---

## 10. Serialization Format

All data structures are serialized using a **canonical deterministic format**
for hashing and signing. The same data must always produce the same bytes,
and therefore the same hash.

For the prototype: **sorted-key JSON with no whitespace**. Simple to implement
and debug. A production system might use a binary format (CBOR, Protocol
Buffers, or a custom format) for efficiency.

Canonical JSON rules:
- Keys sorted alphabetically
- No whitespace
- Numbers as integers (no floating point)
- Strings as UTF-8
- Null represented as `null`
- Signature field excluded when computing tx_hash

Example:
```json
{"author":"ab12...","body":"hello","gas_fee":5,"nonce":1,"reply_to":null,"timestamp":1707600000,"tx_type":"post"}
```

SHA256 of this byte string = the tx_hash (content address).

---

## 11. Data Flow

### Posting Flow

```
 1. User writes post in client UI
 2. Client constructs Post transaction with user's keypair
 3. Client signs transaction with private key
 4. Client submits signed tx to a node via JSON-RPC
 5. Node validates tx (signature, nonce, balance >= gas_fee, body <= 300 chars)
 6. If valid, node adds tx to mempool and gossips to peers
 7. Miner selects tx from mempool (prioritizing high gas fees)
 8. Miner includes tx in candidate block
 9. Miner solves PoW and broadcasts block
10. All nodes validate and append block
11. Client's indexer detects new block, extracts post, inserts into local DB
12. Client UI displays the post
```

### Endorsement Flow

```
 1. User clicks "endorse" on a post in client UI
 2. Client constructs Endorse transaction referencing the post's tx_hash
 3. Client optionally includes tip amount and/or short message
 4. Submission, mining, and propagation follow the same steps as posting
 5. Target post author's balance increases by tip amount (if any)
 6. Client's indexer records the endorsement for the target post
```

### Sync Flow (new node joining)

```
 1. New node connects to bootstrap nodes
 2. Node requests peer lists, builds diverse peer set
 3. Node requests block headers from genesis to current height
 4. Node validates header chain (prev_hash linkage, PoW validity)
 5. Node downloads full blocks in batches
 6. Node validates every transaction in every block, building world state
 7. Node reaches current height, switches to real-time gossip mode
 8. Node is now a fully validated participant
```

---

## 12. Trade-Off Analysis

### Scalability
- **Throughput**: ~33 TPS at 256KB blocks / 15s. Sufficient for early growth.
  Not sufficient for global scale. Future solutions: increased block size,
  layer-2 channels, or sharding.
- **Storage**: ~2.1 GB/day at full capacity. Archival nodes need significant
  storage long-term. Pruned nodes mitigate this.
- **Latency**: 15-second block time means ~15-30s to first confirmation.
  Acceptable for a forum, not for real-time chat.

### Censorship Resistance
- **Strong**: any valid, fee-paying transaction will be included by rational
  miners (they want the fee). A single miner can censor, but others will
  include the transaction.
- **Weakness**: if >50% of mining power colludes to censor, targeted
  transactions can be excluded. Unlikely at scale but possible in small
  networks.

### Immutability
- **Strong**: rewriting history requires re-mining all subsequent blocks.
  Cost grows exponentially with depth.
- **Trade-off**: truly illegal content cannot be removed from the chain. This
  is a real legal and ethical concern. Mitigation is at the client layer
  (filtering), not the protocol layer.

### Usability
- **Barrier to entry**: users need tokens before they can post. Genesis
  bootstrapping via easy early mining helps, and client-operated faucets
  can further reduce friction.
- **Key management**: losing a private key means losing identity and all tokens
  permanently. No recovery mechanism exists by design.

---

## 13. Protocol Parameters

| Parameter              | Value          | Rationale                              |
|------------------------|----------------|----------------------------------------|
| Block time target      | 15 seconds     | Responsive for social, stable for PoW  |
| Max block size         | 256 KB         | ~500 txs, fast propagation             |
| Difficulty adjustment  | Every 100 blks | ~25 min, responsive to miner changes   |
| Max adjustment factor  | 4x per window  | Prevents instability                   |
| Post body limit        | 300 chars      | Fits in single network packet          |
| Endorse message limit  | 150 chars      | Short context, not a full post         |
| Initial block reward   | 50 tokens      | Bootstrap supply                       |
| Halving interval       | 210,000 blocks | ~36.5 days                             |
| Minimum gas fee        | 1 token        | Protocol spam floor                    |
| Signature algorithm    | Ed25519        | Fast, compact, secure                  |
| Hash algorithm         | SHA-256        | Widely supported, PoW compatible       |
| Serialization          | Canonical JSON | Simple for prototype                   |
| Max mempool size       | 10,000 txs     | Configurable per node                  |
| Timestamp tolerance    | +120 seconds   | Prevents future-dating blocks          |

---

## 14. Development Roadmap

### Phase 1 — Core Library

Build the foundational data structures and cryptographic primitives in Python.

- [ ] Key generation, signing, and verification (Ed25519)
- [ ] Transaction classes: Post, Endorse, Transfer, Coinbase
- [ ] Canonical serialization and content addressing (SHA-256 hashing)
- [ ] Block class with header construction and PoW validation
- [ ] Merkle tree implementation for tx_merkle_root
- [ ] World state: account-based balance and nonce tracking
- [ ] State transition logic: apply a block's transactions to state
- [ ] Block validation: verify PoW, all transactions, state roots
- [ ] Chain class: append blocks, track height, fork detection

### Phase 2 — Mining and Mempool

Implement the PoW mining loop and transaction queuing.

- [ ] Mempool: accept, validate, store, evict, and retrieve transactions
- [ ] Mining loop: construct candidate blocks, iterate nonce, solve PoW
- [ ] Difficulty adjustment algorithm (every 100 blocks)
- [ ] Coinbase transaction generation
- [ ] Block reward halving schedule
- [ ] Fee-based transaction ordering in candidate blocks

### Phase 3 — Networking and P2P

Build the node-to-node communication layer.

- [ ] TCP-based peer connections with message framing
- [ ] Handshake protocol (version, height, genesis hash exchange)
- [ ] Peer discovery: bootstrap nodes + peer exchange
- [ ] Transaction gossip: announce, request, respond
- [ ] Block gossip: announce, request, respond
- [ ] Chain sync protocol: bulk block download for new nodes
- [ ] Fork resolution: longest chain selection and reorganization

### Phase 4 — Full Node

Combine all components into a running node.

- [ ] Node startup: load chain from disk or sync from peers
- [ ] Concurrent operation: mining + gossip + RPC server
- [ ] JSON-RPC API: submit_transaction, get_block, get_account, etc.
- [ ] Persistent storage: chain and state on disk (LevelDB or flat files)
- [ ] Pruned node mode: discard old block bodies, keep headers and state
- [ ] Light client mode: header-only sync with Merkle proof requests

### Phase 5 — Client Application

Build a minimal client that demonstrates the full user experience.

- [ ] Indexer: read blocks from node API, populate SQLite database
- [ ] Query layer: latest posts, posts by author, threads, trending
- [ ] Post creation: sign and submit post transactions
- [ ] Endorsement: sign and submit endorsement transactions
- [ ] Balance and account management
- [ ] Simple feed UI (CLI or basic web interface)

### Phase 6 — Simulation and Analysis

Run a multi-node local network and test adversarial scenarios.

- [ ] Multi-node orchestration (multiprocessing or Docker)
- [ ] Normal operation: multiple miners, concurrent posting
- [ ] Network partition simulation: split and rejoin
- [ ] Selfish mining: test longest-chain fork resolution
- [ ] Spam attack: flood the mempool, observe fee market behavior
- [ ] Throughput measurement: transactions per second under load
- [ ] Storage growth analysis over simulated time
- [ ] Document findings and trade-offs
