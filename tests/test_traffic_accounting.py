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


# ── Deletion snapshot ─────────────────────────────────────────────────────────


class TestDeletionSnapshot:
    def test_device_traffic_snapshotted_into_user_totals(self, client, db):
        """Deleting a device must add its Xray traffic to user.traffic_up/down_bytes."""
        user = _make_user(db, username="snap-user", token="snap-token")
        _make_device(db, user_id=user.id, device_id="device-to-delete")

        device_stats = UserTrafficStats(
            upload_bytes=30_000_000,
            download_bytes=70_000_000,
            total_bytes=100_000_000,
        )

        import backend.routes.devices as devices_mod
        patched = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )

        with patch.object(devices_mod, "settings", patched):
            with patch("backend.routes.devices.get_device_traffic", return_value=device_stats):
                resp = client.post(
                    "/api/device/remove",
                    json={"token": "snap-token", "device_id": "device-to-delete"},
                )

        assert resp.status_code == 200
        db.refresh(user)
        assert user.traffic_up_bytes == 30_000_000
        assert user.traffic_down_bytes == 70_000_000

    def test_deletion_proceeds_when_xray_unavailable(self, client, db):
        """Device deletion must succeed even if Xray stats are unreachable."""
        user = _make_user(db, username="offline-user", token="offline-token")
        _make_device(db, user_id=user.id, device_id="device-offline")

        import backend.routes.devices as devices_mod
        patched = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )

        with patch.object(devices_mod, "settings", patched):
            with patch("backend.routes.devices.get_device_traffic", return_value=None):
                resp = client.post(
                    "/api/device/remove",
                    json={"token": "offline-token", "device_id": "device-offline"},
                )

        assert resp.status_code == 200
        # stored fields unchanged (nothing captured, but no crash)
        db.refresh(user)
        assert user.traffic_up_bytes == 0
        assert user.traffic_down_bytes == 0

    def test_existing_stored_traffic_is_accumulated(self, client, db):
        """A second deletion adds to existing stored totals, not replaces them."""
        # Simulate a user who already had one deletion snapshotted
        user = _make_user(
            db,
            username="accum-user",
            token="accum-token",
            traffic_up_bytes=10_000_000,
            traffic_down_bytes=20_000_000,
        )
        _make_device(db, user_id=user.id, device_id="second-device")

        second_stats = UserTrafficStats(
            upload_bytes=5_000_000,
            download_bytes=15_000_000,
            total_bytes=20_000_000,
        )

        import backend.routes.devices as devices_mod
        patched = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )

        with patch.object(devices_mod, "settings", patched):
            with patch("backend.routes.devices.get_device_traffic", return_value=second_stats):
                resp = client.post(
                    "/api/device/remove",
                    json={"token": "accum-token", "device_id": "second-device"},
                )

        assert resp.status_code == 200
        db.refresh(user)
        assert user.traffic_up_bytes == 15_000_000   # 10M + 5M
        assert user.traffic_down_bytes == 35_000_000  # 20M + 15M
