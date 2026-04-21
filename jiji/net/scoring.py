"""Peer misbehavior tracking and persisted ban list.

A score accumulates per source IP for observed misbehavior (bad signature,
malformed JSON, handshake from wrong genesis, clearly-invalid block).
Crossing `BAN_SCORE_THRESHOLD` schedules a ban for `BAN_DURATION` seconds.

Bans are persisted to `{data_dir}/bans.json` so a restart does not wipe
state. A simple `{ip: {"score": int, "until": ts}}` map keeps the format
greppable without needing a migration path.
"""
from __future__ import annotations

import json
import logging
import os
import time
from ipaddress import ip_address, ip_network
from typing import Iterable

from jiji.core.config import (
    BAN_DURATION,
    BAN_SCORE_THRESHOLD,
    SCORE_BAD_HANDSHAKE,
    SCORE_BAD_JSON,
    SCORE_BAD_SIG,
    SCORE_INVALID_BLOCK,
)

logger = logging.getLogger(__name__)


class PeerScorer:
    """Tracks per-IP misbehavior scores and bans, with disk persistence."""

    EVENTS = {
        "bad_sig": SCORE_BAD_SIG,
        "bad_handshake": SCORE_BAD_HANDSHAKE,
        "bad_json": SCORE_BAD_JSON,
        "invalid_block": SCORE_INVALID_BLOCK,
    }

    def __init__(
        self,
        data_dir: str | None = None,
        trusted_cidrs: Iterable[str] = (),
        disabled: bool = False,
    ):
        self.data_dir = data_dir
        self.disabled = disabled
        self._records: dict[str, dict] = {}
        self._trusted = [ip_network(c, strict=False) for c in trusted_cidrs]
        self.load()

    # -- Public API --

    def is_trusted(self, ip: str) -> bool:
        if not self._trusted:
            return False
        try:
            addr = ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self._trusted)

    def is_banned(self, ip: str) -> bool:
        if self.disabled or self.is_trusted(ip):
            return False
        rec = self._records.get(ip)
        if rec is None:
            return False
        until = rec.get("until", 0)
        if until and time.time() < until:
            return True
        # Expired ban — clear it lazily.
        if until and time.time() >= until:
            self._records.pop(ip, None)
            self.save()
        return False

    def record(self, ip: str, event: str) -> bool:
        """Record a misbehavior event. Returns True if the peer is now banned."""
        if self.disabled or self.is_trusted(ip):
            return False
        delta = self.EVENTS.get(event)
        if delta is None:
            return False
        rec = self._records.setdefault(ip, {"score": 0, "until": 0})
        rec["score"] = int(rec.get("score", 0)) + delta
        banned_now = False
        if rec["score"] >= BAN_SCORE_THRESHOLD:
            rec["until"] = time.time() + BAN_DURATION
            rec["score"] = 0
            banned_now = True
            logger.warning(f"banning peer {ip} for {BAN_DURATION}s after '{event}'")
        self.save()
        return banned_now

    # -- Persistence --

    def _path(self) -> str | None:
        if self.data_dir is None:
            return None
        return os.path.join(self.data_dir, "bans.json")

    def load(self) -> None:
        path = self._path()
        if path is None or not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._records = {
                    ip: {
                        "score": int(v.get("score", 0)),
                        "until": float(v.get("until", 0)),
                    }
                    for ip, v in data.items()
                    if isinstance(v, dict)
                }
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(f"failed to load bans from {path}: {e}")

    def save(self) -> None:
        path = self._path()
        if path is None:
            return
        tmp = path + ".tmp"
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self._records, f, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"failed to save bans: {e}")
