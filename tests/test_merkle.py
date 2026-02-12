import pytest
from jiji.core.serialization import sha256
from jiji.core.merkle import merkle_root, merkle_proof, verify_merkle_proof, EMPTY_HASH


class TestMerkleRoot:
    def test_empty_list(self):
        assert merkle_root([]) == EMPTY_HASH

    def test_single_element(self):
        h = sha256(b"leaf")
        assert merkle_root([h]) == h

    def test_two_elements(self):
        h1, h2 = sha256(b"a"), sha256(b"b")
        assert merkle_root([h1, h2]) == sha256(h1 + h2)

    def test_four_elements(self):
        leaves = [sha256(bytes([i])) for i in range(4)]
        left = sha256(leaves[0] + leaves[1])
        right = sha256(leaves[2] + leaves[3])
        assert merkle_root(leaves) == sha256(left + right)

    def test_odd_count_duplicates_last(self):
        leaves = [sha256(bytes([i])) for i in range(3)]
        # 3 leaves: [0,1,2] -> [0,1,2,2] -> hash(0+1), hash(2+2)
        left = sha256(leaves[0] + leaves[1])
        right = sha256(leaves[2] + leaves[2])
        assert merkle_root(leaves) == sha256(left + right)

    def test_deterministic(self):
        leaves = [sha256(bytes([i])) for i in range(5)]
        assert merkle_root(leaves) == merkle_root(leaves)


class TestMerkleProof:
    def test_proof_roundtrip_two_elements(self):
        leaves = [sha256(b"a"), sha256(b"b")]
        root = merkle_root(leaves)
        for i in range(2):
            proof = merkle_proof(leaves, i)
            assert verify_merkle_proof(leaves[i], proof, root)

    def test_proof_roundtrip_seven_elements(self):
        leaves = [sha256(bytes([i])) for i in range(7)]
        root = merkle_root(leaves)
        for i in range(7):
            proof = merkle_proof(leaves, i)
            assert verify_merkle_proof(leaves[i], proof, root)

    def test_proof_roundtrip_large(self):
        leaves = [sha256(bytes([i])) for i in range(16)]
        root = merkle_root(leaves)
        for i in range(16):
            proof = merkle_proof(leaves, i)
            assert verify_merkle_proof(leaves[i], proof, root)

    def test_tampered_leaf_fails(self):
        leaves = [sha256(bytes([i])) for i in range(4)]
        root = merkle_root(leaves)
        proof = merkle_proof(leaves, 0)
        fake_leaf = sha256(b"fake")
        assert not verify_merkle_proof(fake_leaf, proof, root)

    def test_wrong_root_fails(self):
        leaves = [sha256(bytes([i])) for i in range(4)]
        proof = merkle_proof(leaves, 0)
        wrong_root = sha256(b"wrong")
        assert not verify_merkle_proof(leaves[0], proof, wrong_root)

    def test_invalid_index_raises(self):
        leaves = [sha256(b"a")]
        with pytest.raises(ValueError):
            merkle_proof(leaves, 1)
        with pytest.raises(ValueError):
            merkle_proof(leaves, -1)
        with pytest.raises(ValueError):
            merkle_proof([], 0)
