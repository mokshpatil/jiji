from jiji.core.crypto import generate_keypair
from jiji.core.block import Block, BlockHeader
from jiji.core.config import GENESIS_DIFFICULTY, PROTOCOL_VERSION
from jiji.core.merkle import merkle_root
from jiji.core.state import WorldState
from jiji.core.transaction import Coinbase


def make_genesis_block():
    _, pub = generate_keypair()
    cb = Coinbase(recipient=pub, amount=50, height=0)
    tx_root = merkle_root([cb.tx_hash()])
    state = WorldState()
    state.apply_transaction(cb, pub)
    header = BlockHeader(
        version=PROTOCOL_VERSION, height=0, prev_hash=bytes(32),
        timestamp=1000000, miner=pub, difficulty=GENESIS_DIFFICULTY,
        nonce=0, tx_merkle_root=tx_root, state_root=state.state_root(),
        tx_count=1,
    )
    return Block(header=header, transactions=[cb]), pub


class TestBlockHash:
    def test_is_32_bytes(self):
        block, _ = make_genesis_block()
        assert len(block.block_hash()) == 32

    def test_deterministic(self):
        block, _ = make_genesis_block()
        assert block.block_hash() == block.block_hash()

    def test_different_nonce_different_hash(self):
        block, _ = make_genesis_block()
        h1 = block.block_hash()
        block.header.nonce = 999
        h2 = block.block_hash()
        assert h1 != h2


class TestMeetsDifficulty:
    def test_genesis_difficulty(self):
        block, _ = make_genesis_block()
        assert block.meets_difficulty()

    def test_impossible_difficulty_fails(self):
        block, _ = make_genesis_block()
        block.header.difficulty = 256  # requires hash of all zeros
        assert not block.meets_difficulty()


class TestMerkleRoot:
    def test_matches_manual_computation(self):
        block, _ = make_genesis_block()
        expected = merkle_root([block.transactions[0].tx_hash()])
        assert block.compute_tx_merkle_root() == expected


class TestSerializedSize:
    def test_positive(self):
        block, _ = make_genesis_block()
        assert block.serialized_size() > 0


class TestBlockRoundtrip:
    def test_to_dict_from_dict(self):
        block, _ = make_genesis_block()
        restored = Block.from_dict(block.to_dict())
        assert restored.block_hash() == block.block_hash()
        assert restored.header.height == block.header.height
        assert restored.header.miner == block.header.miner
        assert len(restored.transactions) == len(block.transactions)

    def test_transactions_preserved(self):
        block, _ = make_genesis_block()
        restored = Block.from_dict(block.to_dict())
        for orig, rest in zip(block.transactions, restored.transactions):
            assert orig.tx_hash() == rest.tx_hash()
