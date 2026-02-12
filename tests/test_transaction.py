import time
import pytest
from jiji.core.crypto import generate_keypair
from jiji.core.transaction import (
    Post, Endorse, Transfer, Coinbase, transaction_from_dict,
)


def make_keys():
    return generate_keypair()


class TestPost:
    def test_create_sign_verify(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="hello", reply_to=None, gas_fee=2)
        post.sign_tx(priv)
        assert post.verify_signature()

    def test_tx_hash_is_32_bytes(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="test", reply_to=None, gas_fee=1)
        assert len(post.tx_hash()) == 32

    def test_tx_hash_deterministic(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="test", reply_to=None, gas_fee=1)
        assert post.tx_hash() == post.tx_hash()

    def test_tx_hash_independent_of_signature(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="test", reply_to=None, gas_fee=1)
        h1 = post.tx_hash()
        post.sign_tx(priv)
        h2 = post.tx_hash()
        assert h1 == h2

    def test_roundtrip(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="hello", reply_to=None, gas_fee=2)
        post.sign_tx(priv)
        restored = Post.from_dict(post.to_dict())
        assert restored.tx_hash() == post.tx_hash()
        assert restored.verify_signature()

    def test_with_reply_to(self):
        priv, pub = make_keys()
        parent_hash = b"\xab" * 32
        post = Post(author=pub, nonce=0, timestamp=1000, body="reply", reply_to=parent_hash, gas_fee=1)
        post.sign_tx(priv)
        restored = Post.from_dict(post.to_dict())
        assert restored.reply_to == parent_hash

    def test_unsigned_fails_verification(self):
        _, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="test", reply_to=None, gas_fee=1)
        assert not post.verify_signature()

    def test_wrong_key_fails(self):
        priv1, pub1 = make_keys()
        _, pub2 = make_keys()
        post = Post(author=pub2, nonce=0, timestamp=1000, body="test", reply_to=None, gas_fee=1)
        post.sign_tx(priv1)
        assert not post.verify_signature()


class TestEndorse:
    def test_create_sign_verify(self):
        priv, pub = make_keys()
        endorse = Endorse(author=pub, nonce=0, target=b"\x00" * 32, amount=5, message="nice", gas_fee=1)
        endorse.sign_tx(priv)
        assert endorse.verify_signature()

    def test_roundtrip(self):
        priv, pub = make_keys()
        endorse = Endorse(author=pub, nonce=0, target=b"\xff" * 32, amount=10, message="great", gas_fee=2)
        endorse.sign_tx(priv)
        restored = Endorse.from_dict(endorse.to_dict())
        assert restored.tx_hash() == endorse.tx_hash()
        assert restored.verify_signature()

    def test_empty_message(self):
        priv, pub = make_keys()
        endorse = Endorse(author=pub, nonce=0, target=b"\x00" * 32, amount=0, message="", gas_fee=1)
        endorse.sign_tx(priv)
        assert endorse.verify_signature()


class TestTransfer:
    def test_create_sign_verify(self):
        priv, pub = make_keys()
        _, recipient = make_keys()
        tx = Transfer(sender=pub, recipient=recipient, amount=10, nonce=0, gas_fee=1)
        tx.sign_tx(priv)
        assert tx.verify_signature()

    def test_roundtrip(self):
        priv, pub = make_keys()
        _, recipient = make_keys()
        tx = Transfer(sender=pub, recipient=recipient, amount=10, nonce=0, gas_fee=1)
        tx.sign_tx(priv)
        restored = Transfer.from_dict(tx.to_dict())
        assert restored.tx_hash() == tx.tx_hash()
        assert restored.verify_signature()


class TestCoinbase:
    def test_tx_hash(self):
        _, pub = make_keys()
        cb = Coinbase(recipient=pub, amount=50, height=0)
        assert len(cb.tx_hash()) == 32

    def test_roundtrip(self):
        _, pub = make_keys()
        cb = Coinbase(recipient=pub, amount=50, height=0)
        restored = Coinbase.from_dict(cb.to_dict())
        assert restored.tx_hash() == cb.tx_hash()

    def test_different_heights_different_hash(self):
        _, pub = make_keys()
        cb1 = Coinbase(recipient=pub, amount=50, height=0)
        cb2 = Coinbase(recipient=pub, amount=50, height=1)
        assert cb1.tx_hash() != cb2.tx_hash()


class TestTransactionFromDict:
    def test_dispatches_post(self):
        priv, pub = make_keys()
        post = Post(author=pub, nonce=0, timestamp=1000, body="hi", reply_to=None, gas_fee=1)
        post.sign_tx(priv)
        restored = transaction_from_dict(post.to_dict())
        assert isinstance(restored, Post)
        assert restored.tx_hash() == post.tx_hash()

    def test_dispatches_endorse(self):
        priv, pub = make_keys()
        e = Endorse(author=pub, nonce=0, target=b"\x00" * 32, amount=0, message="", gas_fee=1)
        e.sign_tx(priv)
        assert isinstance(transaction_from_dict(e.to_dict()), Endorse)

    def test_dispatches_transfer(self):
        priv, pub = make_keys()
        _, r = make_keys()
        t = Transfer(sender=pub, recipient=r, amount=5, nonce=0, gas_fee=1)
        t.sign_tx(priv)
        assert isinstance(transaction_from_dict(t.to_dict()), Transfer)

    def test_dispatches_coinbase(self):
        _, pub = make_keys()
        cb = Coinbase(recipient=pub, amount=50, height=0)
        assert isinstance(transaction_from_dict(cb.to_dict()), Coinbase)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError):
            transaction_from_dict({"tx_type": "unknown"})
