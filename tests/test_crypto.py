from jiji.core.crypto import generate_keypair, sign, verify, public_key_from_private


class TestKeypairGeneration:
    def test_key_lengths(self):
        priv, pub = generate_keypair()
        assert len(priv) == 32
        assert len(pub) == 32

    def test_keys_are_unique(self):
        _, pub1 = generate_keypair()
        _, pub2 = generate_keypair()
        assert pub1 != pub2

    def test_public_key_derivation(self):
        priv, pub = generate_keypair()
        assert public_key_from_private(priv) == pub


class TestSignAndVerify:
    def test_valid_signature(self):
        priv, pub = generate_keypair()
        sig = sign(priv, b"hello")
        assert len(sig) == 64
        assert verify(pub, b"hello", sig)

    def test_wrong_message_fails(self):
        priv, pub = generate_keypair()
        sig = sign(priv, b"hello")
        assert not verify(pub, b"wrong", sig)

    def test_wrong_key_fails(self):
        priv1, _ = generate_keypair()
        _, pub2 = generate_keypair()
        sig = sign(priv1, b"hello")
        assert not verify(pub2, b"hello", sig)

    def test_tampered_signature_fails(self):
        priv, pub = generate_keypair()
        sig = sign(priv, b"hello")
        tampered = bytes([sig[0] ^ 0xFF]) + sig[1:]
        assert not verify(pub, b"hello", tampered)

    def test_empty_message(self):
        priv, pub = generate_keypair()
        sig = sign(priv, b"")
        assert verify(pub, b"", sig)
