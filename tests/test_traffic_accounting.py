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


# ── Cabinet display ───────────────────────────────────────────────────────────


class TestCabinetDisplay:
    def test_new_user_shows_zero_with_api_configured(self, client, active_user):
        """Brand new user (zero stored + zero live) shows '0 B / ∞'."""
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        zero_stats = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=zero_stats):
                resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "0 B / ∞" in resp.text
        assert "N/A" not in resp.text

    def test_new_user_shows_zero_without_api(self, client, active_user):
        """Brand new user with no API configured still shows '0 B / ∞' (stored=0)."""
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": None}
        )
        with patch.object(page_mod, "settings", patched):
            resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "0 B / ∞" in resp.text
        assert "N/A" not in resp.text

    def test_stored_traffic_shown_when_xray_unavailable(self, client, db):
        """If Xray is down, stored historical totals are still shown."""
        user = _make_user(
            db,
            username="stored-user",
            token="stored-token",
            traffic_up_bytes=50_000_000,
            traffic_down_bytes=150_000_000,
        )
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=None):
                resp = client.get(f"/{user.public_token}")
        assert resp.status_code == 200
        # stored total = 50_000_000 + 150_000_000 = 200_000_000 bytes
        # format_bytes uses 1024-based units: 200_000_000 / 1024^2 ≈ 190.73 MB
        assert "190.73 MB" in resp.text
        assert "N/A" not in resp.text

    def test_repeated_page_loads_do_not_mutate_stored_fields(self, client, db):
        """Page views must never write to traffic_up_bytes / traffic_down_bytes."""
        user = _make_user(db, username="readonly-user", token="readonly-token")
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": None}
        )
        with patch.object(page_mod, "settings", patched):
            client.get(f"/{user.public_token}")
            client.get(f"/{user.public_token}")
            client.get(f"/{user.public_token}")

        db.refresh(user)
        assert user.traffic_up_bytes == 0
        assert user.traffic_down_bytes == 0


# ── Happ subscription userinfo ────────────────────────────────────────────────

_HAPP_HEADERS = {
    "user-agent": "Happ/4.2.1",
    "x-hwid": "traffic-test-hwid",
    "x-device-os": "iOS 17.0",
    "x-device-model": "iPhone 15",
}


class TestHappSubscriptionTraffic:
    def test_userinfo_shows_zero_for_new_user_no_xray(self, client, db):
        """Brand new user with no Xray configured shows upload=0; download=0."""
        user = _make_user(db, username="happ-new", token="happ-new-token")
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f)
               for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": None, "xray_clients_config_path": None}
        )
        with patch.object(profile_mod, "settings", patched):
            resp = client.get(f"/{user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert "upload=0" in userinfo
        assert "download=0" in userinfo

    def test_userinfo_includes_stored_traffic_when_xray_unavailable(self, client, db):
        """Stored historical traffic appears in userinfo even when Xray is down."""
        user = _make_user(
            db,
            username="happ-stored",
            token="happ-stored-token",
            traffic_up_bytes=10_000_000,
            traffic_down_bytes=40_000_000,
        )
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f)
               for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )
        with patch.object(profile_mod, "settings", patched):
            with patch("backend.routes.profile.get_user_traffic_active", return_value=None):
                resp = client.get(f"/{user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert f"upload={10_000_000}" in userinfo
        assert f"download={40_000_000}" in userinfo

    def test_userinfo_combines_stored_and_live_traffic(self, client, db):
        """subscription-userinfo must reflect stored + live active-device totals."""
        user = _make_user(
            db,
            username="happ-combined",
            token="happ-combined-token",
            traffic_up_bytes=5_000_000,
            traffic_down_bytes=15_000_000,
        )
        live_stats = UserTrafficStats(
            upload_bytes=3_000_000,
            download_bytes=7_000_000,
            total_bytes=10_000_000,
        )
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f)
               for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )
        with patch.object(profile_mod, "settings", patched):
            with patch("backend.routes.profile.get_user_traffic_active", return_value=live_stats):
                resp = client.get(f"/{user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert f"upload={8_000_000}" in userinfo   # 5M stored + 3M live
        assert f"download={22_000_000}" in userinfo  # 15M stored + 7M live

    def test_happ_subscription_reads_do_not_mutate_stored_fields(self, client, db):
        """Subscription fetches must never write to traffic_up_bytes / traffic_down_bytes."""
        user = _make_user(db, username="happ-readonly", token="happ-readonly-token")
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f)
               for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": None, "xray_clients_config_path": None}
        )
        with patch.object(profile_mod, "settings", patched):
            client.get(f"/{user.public_token}", headers=_HAPP_HEADERS)
            client.get(f"/{user.public_token}", headers=_HAPP_HEADERS)

        db.refresh(user)
        assert user.traffic_up_bytes == 0
        assert user.traffic_down_bytes == 0
