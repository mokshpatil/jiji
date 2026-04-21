# Protocol parameters for the jiji blockchain

PROTOCOL_VERSION = 1

# Block timing
BLOCK_TIME_TARGET = 15
DIFFICULTY_ADJUSTMENT_WINDOW = 10
MAX_DIFFICULTY_ADJUSTMENT = 4.0

# Block limits
MAX_BLOCK_SIZE = 262144

# Transaction limits
POST_BODY_LIMIT = 300
ENDORSE_MESSAGE_LIMIT = 150
MINIMUM_GAS_FEE = 1

# Token economics
INITIAL_BLOCK_REWARD = 50
HALVING_INTERVAL = 210000

# Mempool
MAX_MEMPOOL_SIZE = 10000
# Replace-By-Fee minimum bump, in basis points (1000 bps = 10%).
RBF_MIN_BUMP_BPS = 1000

# Hard fork: dynamic min fee and self-endorsement ban activate at this height.
# For a fresh chain, leaving this at 0 activates from genesis. Raise it when
# coordinating a fork on a live chain.
HARDFORK_HEIGHT = 0

# Dynamic minimum fee
DYNAMIC_FEE_WINDOW = 100
DYNAMIC_FEE_TARGET = 0.5  # target block utilization (fraction of MAX_BLOCK_SIZE)
DYNAMIC_FEE_MAX_STEP = 0.125  # ±12.5% per retarget window

# Timestamps
MAX_FUTURE_TIMESTAMP = 120
MEDIAN_TIME_BLOCK_COUNT = 11

# Genesis
GENESIS_DIFFICULTY = 1

# PoW target ceiling (2^256 - 1)
MAX_TARGET = (1 << 256) - 1

# Chain reorganization
MAX_REORG_DEPTH = 100

# Networking
DEFAULT_P2P_PORT = 9333
DEFAULT_RPC_PORT = 9332
MAX_PEERS = 50
MAX_OUTBOUND = 40
MAX_INBOUND = 10

# LAN discovery (mDNS / DNS-SD service type)
MDNS_SERVICE_TYPE = "_jiji._tcp.local."
SYNC_BATCH_SIZE = 50
PEER_EXCHANGE_INTERVAL = 60
PEER_EXCHANGE_INITIAL_DELAY = 5
MAX_MESSAGE_SIZE = 4 * 1024 * 1024  # 4 MB
HANDSHAKE_TIMEOUT = 10
PEER_MAX_AGE = 7 * 24 * 3600  # 7 days
MAX_SAVED_PEERS = 200

# Rate limiting and peer scoring
INBOUND_CONN_PER_MIN = 5  # per /32 sliding-window cap
PEER_MSG_PER_SEC = 100
PEER_MSG_BURST = 200
RPC_REQ_PER_MIN = 120
SEEN_SET_MAX = 50000
SEEN_SET_FLUSH_INTERVAL = 60
BAN_DURATION = 86400  # 24h
BAN_SCORE_THRESHOLD = 100
SCORE_BAD_SIG = 10
SCORE_BAD_HANDSHAKE = 20
SCORE_BAD_JSON = 5
SCORE_INVALID_BLOCK = 50


def block_reward(height: int) -> int:
    """Compute block reward for a given height using halving schedule."""
    halvings = height // HALVING_INTERVAL
    if halvings >= 64:
        return 0
    return INITIAL_BLOCK_REWARD >> halvings
