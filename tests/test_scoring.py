"""Tests for PeerScorer: misbehavior events, bans, and disk persistence."""
import json
import os
import time

from jiji.core.config import BAN_SCORE_THRESHOLD
from jiji.net.scoring import PeerScorer


def test_score_accumulates_and_bans(tmp_path):
    s = PeerScorer(data_dir=str(tmp_path))
    # A single BAD_HANDSHAKE is 20 points; under the threshold.
    s.record("1.2.3.4", "bad_handshake")
    assert not s.is_banned("1.2.3.4")
    # Two INVALID_BLOCK events (50 each) should cross the 100-point threshold.
    assert not s.record("1.2.3.4", "invalid_block")
    banned_now = s.record("1.2.3.4", "invalid_block")
    assert banned_now
    assert s.is_banned("1.2.3.4")


def test_ban_persists_across_restart(tmp_path):
    s = PeerScorer(data_dir=str(tmp_path))
    for _ in range(3):
        s.record("9.9.9.9", "invalid_block")
    assert s.is_banned("9.9.9.9")

    # New scorer instance loads from disk.
    s2 = PeerScorer(data_dir=str(tmp_path))
    assert s2.is_banned("9.9.9.9")
    assert os.path.exists(tmp_path / "bans.json")


def test_expired_ban_is_cleared(tmp_path):
    s = PeerScorer(data_dir=str(tmp_path))
    s._records["5.5.5.5"] = {"score": 0, "until": time.time() - 1}
    s.save()
    assert not s.is_banned("5.5.5.5")
    # And the record should now be gone.
    assert "5.5.5.5" not in s._records


def test_trusted_cidr_exempt(tmp_path):
    s = PeerScorer(data_dir=str(tmp_path), trusted_cidrs=["10.0.0.0/8"])
    for _ in range(10):
        s.record("10.1.2.3", "invalid_block")
    assert not s.is_banned("10.1.2.3")
    assert "10.1.2.3" not in s._records  # no record created


def test_disabled_scorer_no_op(tmp_path):
    s = PeerScorer(data_dir=str(tmp_path), disabled=True)
    s.record("8.8.8.8", "invalid_block")
    assert not s.is_banned("8.8.8.8")


def test_unknown_event_ignored(tmp_path):
    s = PeerScorer(data_dir=str(tmp_path))
    s.record("1.1.1.1", "nope")
    assert "1.1.1.1" not in s._records


def test_save_and_load_roundtrip(tmp_path):
    s = PeerScorer(data_dir=str(tmp_path))
    s.record("2.2.2.2", "bad_sig")
    s.record("2.2.2.2", "bad_sig")
    s.save()
    data = json.loads((tmp_path / "bans.json").read_text())
    assert data["2.2.2.2"]["score"] == 20
