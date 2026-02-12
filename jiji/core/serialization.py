import hashlib
import json


def _json_default(obj):
    """Handle non-standard types in JSON serialization."""
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"Not JSON serializable: {type(obj)}")


def canonicalize(data: dict, exclude_fields: set[str] | None = None) -> bytes:
    """Serialize a dict to canonical JSON bytes (sorted keys, no whitespace)."""
    if exclude_fields:
        data = {k: v for k, v in data.items() if k not in exclude_fields}
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def sha256(data: bytes) -> bytes:
    """Compute SHA-256 hash."""
    return hashlib.sha256(data).digest()


def compute_hash(data: dict, exclude_fields: set[str] | None = None) -> bytes:
    """Compute SHA-256 of canonical JSON representation."""
    return sha256(canonicalize(data, exclude_fields))
