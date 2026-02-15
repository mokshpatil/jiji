# Protocol parameters for the jiji blockchain

PROTOCOL_VERSION = 1

# Block timing
BLOCK_TIME_TARGET = 15
DIFFICULTY_ADJUSTMENT_WINDOW = 100
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

# Timestamps
MAX_FUTURE_TIMESTAMP = 120
MEDIAN_TIME_BLOCK_COUNT = 11

# Genesis
GENESIS_DIFFICULTY = 1

# PoW target ceiling (2^256 - 1)
MAX_TARGET = (1 << 256) - 1

# Networking
DEFAULT_P2P_PORT = 9333
DEFAULT_RPC_PORT = 9332
MAX_PEERS = 50
SYNC_BATCH_SIZE = 50
PEER_EXCHANGE_INTERVAL = 60
MAX_MESSAGE_SIZE = 4 * 1024 * 1024  # 4 MB
HANDSHAKE_TIMEOUT = 10


def block_reward(height: int) -> int:
    """Compute block reward for a given height using halving schedule."""
    halvings = height // HALVING_INTERVAL
    if halvings >= 64:
        return 0
    return INITIAL_BLOCK_REWARD >> halvings
