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


def _bulk_stats_for(per_user: dict[str, tuple[int, int]]) -> list[tuple[str, int]]:
    """Build raw _call_query_stats output for the given per-user (up, down) totals.

    Used to drive snapshot_all_users_traffic_before_reload in tests without
    going through a real gRPC call.
    """
    rows: list[tuple[str, int]] = []
    for username, (up, down) in per_user.items():
        rows.append((f"user>>>{username}/dev>>>traffic>>>uplink", up))
        rows.append((f"user>>>{username}/dev>>>traffic>>>downlink", down))
    return rows


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
        """Deleting a device snapshots ALL active users' Xray traffic via
        snapshot_all_users_traffic_before_reload.  The full user total is
        added to the user's stored counters before Xray's reload zeroes them.
        """
        user = _make_user(db, username="snap-user", token="snap-token")
        _make_device(db, user_id=user.id, device_id="device-to-delete")

        # Bulk stats for this single active user.
        bulk_raw = _bulk_stats_for({"snap-user": (30_000_000, 70_000_000)})

        import backend.routes.devices as devices_mod
        patched = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )

        with patch.object(devices_mod, "settings", patched):
            with patch("backend.xray_stats._call_query_stats", return_value=bulk_raw):
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
            with patch("backend.xray_stats._call_query_stats", return_value=None):
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

        # Bulk live stats since the last snapshot (reset=True cleared previous).
        bulk_raw = _bulk_stats_for({"accum-user": (5_000_000, 15_000_000)})

        import backend.routes.devices as devices_mod
        patched = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )

        with patch.object(devices_mod, "settings", patched):
            with patch("backend.xray_stats._call_query_stats", return_value=bulk_raw):
                resp = client.post(
                    "/api/device/remove",
                    json={"token": "accum-token", "device_id": "second-device"},
                )

        assert resp.status_code == 200
        db.refresh(user)
        assert user.traffic_up_bytes == 15_000_000   # 10M stored + 5M live
        assert user.traffic_down_bytes == 35_000_000  # 20M stored + 15M live


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


# ── Bulk snapshot ─────────────────────────────────────────────────────────────


class TestBulkSnapshotBeforeReload:
    """The reload-induced regression bug: when user A adds a device, Xray's
    SIGHUP reload zeroes EVERY user's StatsService counters.  Without a bulk
    snapshot, user B's accumulated live traffic vanishes from the dashboard
    until they generate enough new traffic to "catch up".

    These tests verify that the bulk snapshot captures every active user's
    live traffic into the DB before the reload, so cumulative traffic remains
    monotonic across a reload boundary.
    """

    def test_snapshot_captures_traffic_for_multiple_users(self, db):
        """A single bulk snapshot call must persist deltas for every user."""
        from backend.xray_stats import snapshot_all_users_traffic_before_reload

        u1 = _make_user(db, username="alice", token="alice-tok")
        u2 = _make_user(db, username="bob",   token="bob-tok")

        bulk_raw = _bulk_stats_for({
            "alice": (1_000_000, 4_000_000),
            "bob":   (500_000,   2_500_000),
        })

        with patch("backend.xray_stats._call_query_stats", return_value=bulk_raw):
            updated = snapshot_all_users_traffic_before_reload(
                db=db, xray_api_addr="127.0.0.1:10085",
            )

        assert updated == 2
        db.refresh(u1)
        db.refresh(u2)
        assert u1.traffic_up_bytes == 1_000_000
        assert u1.traffic_down_bytes == 4_000_000
        assert u2.traffic_up_bytes == 500_000
        assert u2.traffic_down_bytes == 2_500_000

    def test_snapshot_returns_zero_when_xray_unreachable(self, db):
        """When Xray is unreachable, no DB write occurs and zero is returned."""
        from backend.xray_stats import snapshot_all_users_traffic_before_reload

        u = _make_user(db, username="alone", token="alone-tok")

        with patch("backend.xray_stats._call_query_stats", return_value=None):
            updated = snapshot_all_users_traffic_before_reload(
                db=db, xray_api_addr="127.0.0.1:10085",
            )

        assert updated == 0
        db.refresh(u)
        assert u.traffic_up_bytes == 0
        assert u.traffic_down_bytes == 0

    def test_dashboard_does_not_regress_across_reload(self, client, db):
        """Reproduces the bug from the report: bystander's dashboard shows
        N GB; actor adds/removes a device → reload happens → bystander's live
        counter resets to 0.  Without the bulk snapshot, bystander's dashboard
        now shows stored + 0 = stored < N GB (regression).

        With the bulk snapshot, bystander's stored has already absorbed the
        live traffic before the reload; combined = stored + 0 = N GB.
        """
        # Actor — whose device-remove triggers the reload.
        user_a = _make_user(db, username="actor", token="actor-tok")
        _make_device(db, user_id=user_a.id, device_id="actor-device")

        # Bystander — their dashboard must not regress.
        user_b = _make_user(db, username="bystander", token="bystander-tok")
        _make_device(db, user_id=user_b.id, device_id="bystander-device")

        # Pre-reload bulk: bystander has 8 GB live, actor has small live too.
        eight_gb = 8 * 1024 ** 3
        pre_reload_raw = _bulk_stats_for({
            "actor":     (1_000_000, 1_000_000),
            "bystander": (eight_gb, 0),
        })

        # Patch settings the same way other tests do (avoid actual Xray writes).
        import backend.routes.devices as devices_mod
        patched_devices = devices_mod.settings.__class__(
            **{f: getattr(devices_mod.settings, f)
               for f in devices_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085", "xray_clients_config_path": None}
        )
        import backend.routes.user_page as page_mod
        patched_page = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f)
               for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )

        # 1. Actor removes a device — triggers bulk snapshot.
        with patch.object(devices_mod, "settings", patched_devices):
            with patch("backend.xray_stats._call_query_stats", return_value=pre_reload_raw):
                resp = client.post(
                    "/api/device/remove",
                    json={"token": "actor-tok", "device_id": "actor-device"},
                )
        assert resp.status_code == 200

        # 2. After the snapshot, bystander's stored should hold the 8 GB.
        db.refresh(user_b)
        assert user_b.traffic_up_bytes == eight_gb

        # 3. Bystander loads their dashboard AFTER the (simulated) reload.
        # Live counter is now 0 (Xray was reset).
        zero_live = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        with patch.object(page_mod, "settings", patched_page):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=zero_live):
                resp = client.get(f"/{user_b.public_token}")

        assert resp.status_code == 200
        # 8 GB rendered as "8.00 GB" by format_bytes.
        assert "8.00 GB" in resp.text


# ── Monotonic display ─────────────────────────────────────────────────────────


class TestMonotonicTrafficDisplay:
    """High-water mark guarantees the displayed total never decreases, even
    when the bulk snapshot did not run (e.g. Xray gRPC API is briefly down)."""

    def test_xray_unavailable_does_not_drop_below_last_total(self, client, db):
        """Page load with Xray unreachable must clamp to the previously seen
        high-water mark instead of dropping to stored-only."""
        user = _make_user(db, username="monouser", token="mono-tok")
        # Simulate a previous successful read that pushed the high-water mark
        # to 2 GB up.
        user.traffic_up_high_water_bytes = 2 * 1024 ** 3
        user.traffic_down_high_water_bytes = 0
        db.commit()

        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )

        # Xray is briefly unreachable — get_user_traffic_active returns None.
        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=None):
                resp = client.get(f"/{user.public_token}")

        assert resp.status_code == 200
        # Without the high-water clamp, this would render "0 B / ∞" because
        # stored=0 and live=None.  With the clamp, it shows 2 GB.
        assert "2.00 GB" in resp.text

    def test_high_water_advances_on_growth(self, client, db):
        """Two consecutive page loads with growing live traffic both render
        the higher value, and the HWM advances accordingly."""
        user = _make_user(db, username="growuser", token="grow-tok")

        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )

        live_1gb = UserTrafficStats(
            upload_bytes=1024 ** 3, download_bytes=0, total_bytes=1024 ** 3,
        )
        live_3gb = UserTrafficStats(
            upload_bytes=3 * 1024 ** 3, download_bytes=0, total_bytes=3 * 1024 ** 3,
        )

        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic_active", return_value=live_1gb):
                client.get(f"/{user.public_token}")
            db.refresh(user)
            assert user.traffic_up_high_water_bytes == 1024 ** 3

            with patch("backend.routes.user_page.get_user_traffic_active", return_value=live_3gb):
                resp = client.get(f"/{user.public_token}")

        db.refresh(user)
        assert user.traffic_up_high_water_bytes == 3 * 1024 ** 3
        assert resp.status_code == 200
        assert "3.00 GB" in resp.text
