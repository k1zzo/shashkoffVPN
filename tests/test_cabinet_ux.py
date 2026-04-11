"""Tests for cabinet UX: Moscow-time formatting and deep-link behavior.

Covers:
- Device last_seen_at shown in Europe/Moscow (UTC+3)
- expires_at shown in Europe/Moscow (matching Happ's display)
- Happ deep-link HTML attributes present for JS to use
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest

from backend.models import Device, User


_MSK = timezone(timedelta(hours=3))


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute)


def _make_user(db, *, username, token, expires_at=None, **kwargs):
    user = User(
        username=username,
        public_token=token,
        uuid=str(uuid4()),
        is_active=True,
        max_devices=3,
        expires_at=expires_at,
        **kwargs,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_device(db, *, user_id, device_id, last_seen_at):
    device = Device(
        user_id=user_id,
        device_id=device_id,
        device_name=f"Device {device_id[-4:]}",
        platform="iOS",
        device_uuid=str(uuid4()),
        is_active=True,
        last_seen_at=last_seen_at,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


# ── Moscow-time device activity ───────────────────────────────────────────────


class TestDeviceLastSeenMoscowTime:
    def test_last_seen_converted_from_utc_to_msk(self, client, db):
        """last_seen_at stored as UTC must be displayed in Moscow time (UTC+3)."""
        # 21:00 UTC = 00:00 MSK next day
        user = _make_user(db, username="msk-user", token="msk-token",
                          expires_at=_utc(2099, 12, 31))
        _make_device(db, user_id=user.id, device_id="dev-msk-1",
                     last_seen_at=_utc(2025, 4, 9, 21, 0))  # 21:00 UTC

        resp = client.get("/msk-token")
        assert resp.status_code == 200
        # 21:00 UTC = 00:00 MSK April 10
        assert "2025-04-10 00:00:00" in resp.text

    def test_last_seen_not_shown_as_utc(self, client, db):
        """Raw UTC timestamp must not appear in the cabinet when MSK differs."""
        user = _make_user(db, username="msk-utc-user", token="msk-utc-token",
                          expires_at=_utc(2099, 12, 31))
        _make_device(db, user_id=user.id, device_id="dev-msk-2",
                     last_seen_at=_utc(2025, 4, 9, 21, 0))

        resp = client.get("/msk-utc-token")
        assert resp.status_code == 200
        # UTC value must not appear
        assert "2025-04-09 21:00:00" not in resp.text

    def test_last_seen_midday_utc_stays_same_date_in_msk(self, client, db):
        """12:00 UTC = 15:00 MSK same day — date must not shift."""
        user = _make_user(db, username="msk-noon-user", token="msk-noon-token",
                          expires_at=_utc(2099, 12, 31))
        _make_device(db, user_id=user.id, device_id="dev-noon",
                     last_seen_at=_utc(2025, 6, 15, 12, 0))  # 12:00 UTC = 15:00 MSK

        resp = client.get("/msk-noon-token")
        assert resp.status_code == 200
        assert "2025-06-15 15:00:00" in resp.text


# ── Moscow-time expiry date ───────────────────────────────────────────────────


class TestExpiriesAtMoscowTime:
    def test_expiry_shifts_to_next_day_when_utc_late_evening(self, client, db):
        """expires_at = 21:00 UTC = midnight MSK → cabinet shows the MSK date (next day)."""
        # April 9 21:00 UTC = April 10 00:00 MSK
        user = _make_user(db, username="exp-msk-user", token="exp-msk-token",
                          expires_at=_utc(2025, 4, 9, 21, 0))

        resp = client.get("/exp-msk-token")
        assert resp.status_code == 200
        # Cabinet must show April 10 (MSK), not April 9 (UTC)
        assert "10.04.2025" in resp.text
        assert "09.04.2025" not in resp.text

    def test_expiry_same_day_for_utc_midnight(self, client, db):
        """expires_at = 00:00 UTC = 03:00 MSK same day → cabinet shows same date."""
        user = _make_user(db, username="exp-midnight-user", token="exp-midnight-token",
                          expires_at=_utc(2025, 7, 20, 0, 0))  # 00:00 UTC = 03:00 MSK

        resp = client.get("/exp-midnight-token")
        assert resp.status_code == 200
        # 00:00 UTC = 03:00 MSK same calendar day
        assert "20.07.2025" in resp.text

    def test_expiry_none_shows_never(self, client, db):
        """Users with no expiry must show 'Never', not a date."""
        user = _make_user(db, username="exp-never-user", token="exp-never-token",
                          expires_at=None)

        resp = client.get("/exp-never-token")
        assert resp.status_code == 200
        assert "Never" in resp.text

    def test_expiry_matches_happ_unix_timestamp_semantics(self, client, db):
        """Cabinet date must match what Happ shows for the same Unix timestamp.

        Happ reads expire= from subscription-userinfo and shows it in local time.
        For Moscow (UTC+3): ts = Apr 9 21:00 UTC → Apr 10 00:00 MSK → Happ shows Apr 10.
        Cabinet must also show Apr 10.
        """
        expires_utc = _utc(2025, 4, 9, 21, 0)
        user = _make_user(db, username="ts-match-user", token="ts-match-token",
                          expires_at=expires_utc)

        resp = client.get("/ts-match-token")
        assert resp.status_code == 200

        # The Unix timestamp Happ uses:
        import calendar
        expected_ts = calendar.timegm(expires_utc.timetuple())
        # MSK date for that timestamp:
        msk_dt = datetime.fromtimestamp(expected_ts, tz=_MSK)
        expected_date_str = msk_dt.strftime("%d.%m.%Y")

        assert expected_date_str in resp.text


# ── Happ deep-link button attributes ─────────────────────────────────────────


class TestHappDeepLinkAttributes:
    def test_add_button_has_happ_link_attribute(self, client, active_user):
        """data-happ-link attribute must be present so JS can navigate directly."""
        resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert 'data-happ-link="happ://' in resp.text

    def test_add_button_has_subscription_url_attribute(self, client, active_user):
        """data-subscription-url must be present for the copy fallback."""
        resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "data-subscription-url=" in resp.text

    def test_add_button_no_about_blank_in_html(self, client, active_user):
        """The cabinet HTML must not contain about:blank — that was the broken flow."""
        resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "about:blank" not in resp.text


# ── Device title and icon rendering ──────────────────────────────────────────


class TestDeviceTitleAndIcon:
    def _make_ios_device(self, db, user_id):
        device = Device(
            user_id=user_id,
            device_id="dev-icon-1",
            device_name="iPhone 15",
            platform="iOS",
            device_uuid=str(uuid4()),
            is_active=True,
            last_seen_at=_utc(2025, 6, 1, 12, 0),
        )
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    def test_device_title_rendered_in_html(self, client, db):
        """Cabinet must render 'iOS - iPhone 15', not bare device_name."""
        user = _make_user(db, username="icon-user", token="icon-token",
                          expires_at=_utc(2099, 12, 31))
        self._make_ios_device(db, user.id)

        resp = client.get("/icon-token")
        assert resp.status_code == 200
        assert "iOS - iPhone 15" in resp.text

    def test_local_icon_path_in_html(self, client, db):
        """Cabinet must reference the local SVG path, not a CDN URL."""
        user = _make_user(db, username="icon-path-user", token="icon-path-token",
                          expires_at=_utc(2099, 12, 31))
        self._make_ios_device(db, user.id)

        resp = client.get("/icon-path-token")
        assert resp.status_code == 200
        assert "/static/icons/ios.svg" in resp.text

    def test_no_cdn_urls_in_html(self, client, db):
        """Cabinet must not reference any Wikipedia/CDN URLs for device icons."""
        user = _make_user(db, username="no-cdn-user", token="no-cdn-token",
                          expires_at=_utc(2099, 12, 31))
        self._make_ios_device(db, user.id)

        resp = client.get("/no-cdn-token")
        assert resp.status_code == 200
        assert "wikipedia.org" not in resp.text
        assert "wikimedia.org" not in resp.text
