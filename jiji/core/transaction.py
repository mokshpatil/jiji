from __future__ import annotations

from dataclasses import dataclass

from jiji.core.crypto import sign, verify
from jiji.core.serialization import canonicalize, compute_hash

_SIGNATURE_FIELD = {"signature"}


def _to_hex(b: bytes | None) -> str | None:
    """Convert bytes to hex string, or None."""
    if b is None:
        return None
    return b.hex()


def _from_hex(s: str | None) -> bytes | None:
    """Convert hex string to bytes, or None."""
    if s is None:
        return None
    if s == "":
        return b""
    return bytes.fromhex(s)


class Signable:
    """Mixin providing signing, verification, and hashing for transactions."""

    signature: bytes

    def to_dict(self) -> dict:
        raise NotImplementedError

    @property
    def signer_key(self) -> bytes:
        raise NotImplementedError

    def tx_hash(self) -> bytes:
        """Content address: SHA-256 of canonical serialization excluding signature."""
        return compute_hash(self.to_dict(), exclude_fields=_SIGNATURE_FIELD)

    def sign_tx(self, private_key: bytes) -> None:
        """Sign this transaction with the given private key."""
        payload = canonicalize(self.to_dict(), exclude_fields=_SIGNATURE_FIELD)
        self.signature = sign(private_key, payload)

    def verify_signature(self) -> bool:
        """Verify the transaction signature against the signer's public key."""
        if not self.signature:
            return False
        payload = canonicalize(self.to_dict(), exclude_fields=_SIGNATURE_FIELD)
        return verify(self.signer_key, payload, self.signature)


@dataclass
class Post(Signable):
    """A text post on the network."""

    author: bytes
    nonce: int
    timestamp: int
    body: str
    reply_to: bytes | None
    gas_fee: int
    signature: bytes = b""

    @property
    def signer_key(self) -> bytes:
        return self.author

    def to_dict(self) -> dict:
        return {
            "tx_type": "post",
            "author": self.author.hex(),
            "nonce": self.nonce,
            "timestamp": self.timestamp,
            "body": self.body,
            "reply_to": _to_hex(self.reply_to),
            "gas_fee": self.gas_fee,
            "signature": _to_hex(self.signature),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Post:
        return cls(
            author=_from_hex(d["author"]),
            nonce=d["nonce"],
            timestamp=d["timestamp"],
            body=d["body"],
            reply_to=_from_hex(d.get("reply_to")),
            gas_fee=d["gas_fee"],
            signature=_from_hex(d.get("signature", "")),
        )


@dataclass
class Endorse(Signable):
    """An endorsement of an existing post, optionally with a tip and message."""

    author: bytes
    nonce: int
    target: bytes
    amount: int
    message: str
    gas_fee: int
    signature: bytes = b""

    @property
    def signer_key(self) -> bytes:
        return self.author

    def to_dict(self) -> dict:
        return {
            "tx_type": "endorse",
            "author": self.author.hex(),
            "nonce": self.nonce,
            "target": self.target.hex(),
            "amount": self.amount,
            "message": self.message,
            "gas_fee": self.gas_fee,
            "signature": _to_hex(self.signature),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Endorse:
        return cls(
            author=_from_hex(d["author"]),
            nonce=d["nonce"],
            target=_from_hex(d["target"]),
            amount=d["amount"],
            message=d.get("message", ""),
            gas_fee=d["gas_fee"],
            signature=_from_hex(d.get("signature", "")),
        )


@dataclass
class Transfer(Signable):
    """A token transfer between accounts."""

    sender: bytes
    recipient: bytes
    amount: int
    nonce: int
    gas_fee: int
    signature: bytes = b""

    @property
    def signer_key(self) -> bytes:
        return self.sender

    def to_dict(self) -> dict:
        return {
            "tx_type": "transfer",
            "sender": self.sender.hex(),
            "recipient": self.recipient.hex(),
            "amount": self.amount,
            "nonce": self.nonce,
            "gas_fee": self.gas_fee,
            "signature": _to_hex(self.signature),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Transfer:
        return cls(
            sender=_from_hex(d["sender"]),
            recipient=_from_hex(d["recipient"]),
            amount=d["amount"],
            nonce=d["nonce"],
            gas_fee=d["gas_fee"],
            signature=_from_hex(d.get("signature", "")),
        )


@dataclass
class Coinbase:
    """Block reward transaction. Validity comes from the block, not a signature."""

    recipient: bytes
    amount: int
    height: int

    def to_dict(self) -> dict:
        return {
            "tx_type": "coinbase",
            "recipient": self.recipient.hex(),
            "amount": self.amount,
            "height": self.height,
        }

    def tx_hash(self) -> bytes:
        """Content address: SHA-256 of canonical serialization."""
        return compute_hash(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict) -> Coinbase:
        return cls(
            recipient=_from_hex(d["recipient"]),
            amount=d["amount"],
            height=d["height"],
        )


# Union type for all transaction kinds
Transaction = Post | Endorse | Transfer | Coinbase


def transaction_from_dict(data: dict) -> Transaction:
    """Deserialize a transaction dict into the appropriate type."""
    tx_type = data.get("tx_type")
    factories = {
        "post": Post.from_dict,
        "endorse": Endorse.from_dict,
        "transfer": Transfer.from_dict,
        "coinbase": Coinbase.from_dict,
    }
    factory = factories.get(tx_type)
    if factory is None:
        raise ValueError(f"Unknown transaction type: {tx_type}")
    return factory(data)
