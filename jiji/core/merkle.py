from jiji.core.serialization import sha256

EMPTY_HASH = sha256(b"")


def merkle_root(hashes: list[bytes]) -> bytes:
    """Compute the Merkle root of a list of leaf hashes."""
    if not hashes:
        return EMPTY_HASH
    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        next_level = []
        for i in range(0, len(level), 2):
            next_level.append(sha256(level[i] + level[i + 1]))
        level = next_level
    return level[0]


def merkle_proof(hashes: list[bytes], index: int) -> list[tuple[bytes, bool]]:
    """Generate a Merkle proof for the leaf at the given index.

    Returns list of (sibling_hash, is_left) tuples where is_left indicates
    the sibling is on the left side of the concatenation.
    """
    if not hashes or index < 0 or index >= len(hashes):
        raise ValueError("invalid index for merkle proof")
    proof = []
    level = list(hashes)
    idx = index
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        if idx % 2 == 0:
            proof.append((level[idx + 1], False))
        else:
            proof.append((level[idx - 1], True))
        next_level = []
        for i in range(0, len(level), 2):
            next_level.append(sha256(level[i] + level[i + 1]))
        level = next_level
        idx //= 2
    return proof


def verify_merkle_proof(
    leaf_hash: bytes, proof: list[tuple[bytes, bool]], root: bytes
) -> bool:
    """Verify a Merkle proof against a known root."""
    current = leaf_hash
    for sibling, is_left in proof:
        if is_left:
            current = sha256(sibling + current)
        else:
            current = sha256(current + sibling)
    return current == root
