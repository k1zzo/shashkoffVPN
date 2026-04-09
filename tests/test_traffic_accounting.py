"""Integration tests for cumulative traffic accounting.

Verifies that:
- new users start with zero traffic fields
- device deletion snapshots traffic into user totals
- cabinet and Happ both show stored + live totals
- repeated reads do not mutate stored fields
- Xray unavailability after deletion preserves stored totals
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from backend.models import Device, User
from backend.xray_stats import UserTrafficStats


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_user(db, *, username: str, token: str, **kwargs) -> User:
    user = User(
        username=username,
        public_token=token,
        uuid=str(uuid4()),
        is_active=True,
        max_devices=3,
        expires_at=datetime.utcnow() + timedelta(days=30),
        **kwargs,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_device(db, *, user_id: int, device_id: str, **kwargs) -> Device:
    device = Device(
        user_id=user_id,
        device_id=device_id,
        device_name=f"Device {device_id[-6:]}",
        platform="iOS",
        device_uuid=str(uuid4()),
        is_active=True,
        **kwargs,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


# ── Model field defaults ──────────────────────────────────────────────────────


class TestUserTrafficFields:
    def test_new_user_has_zero_traffic_fields(self, db):
        """A freshly created user must have traffic_up_bytes == traffic_down_bytes == 0."""
        user = _make_user(db, username="new-user", token="new-token")
        assert user.traffic_up_bytes == 0
        assert user.traffic_down_bytes == 0
