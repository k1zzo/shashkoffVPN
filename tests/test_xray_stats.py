"""Tests for the Xray stats module.

Covers:
  - Protobuf encoder (encode_query_stats_request)
  - Protobuf decoder (decode_query_stats_response / _decode_stat_message)
  - Traffic aggregation (get_user_traffic with mocked gRPC transport)
  - format_bytes helper
  - Cabinet response includes real traffic when API is available
  - Happ subscription-userinfo contains real traffic when API is available
  - Zero is returned honestly when Xray reports zero (not confused with unavailable)
  - None is returned honestly when stats are unavailable
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.xray_stats import (
    UserTrafficStats,
    _call_query_stats,
    _decode_stat_message,
    _varint_encode,
    combined_traffic,
    decode_query_stats_response,
    encode_query_stats_request,
    format_bytes,
    get_user_traffic,
)


# ── format_bytes ──────────────────────────────────────────────────────────────


class TestFormatBytes:
    def test_bytes(self):
        assert format_bytes(0) == "0 B"
        assert format_bytes(999) == "999 B"
        assert format_bytes(1023) == "1023 B"

    def test_kilobytes(self):
        assert format_bytes(1024) == "1.00 KB"
        assert format_bytes(2048) == "2.00 KB"

    def test_megabytes(self):
        assert format_bytes(1024 ** 2) == "1.00 MB"
        assert format_bytes(5 * 1024 ** 2) == "5.00 MB"

    def test_gigabytes(self):
        assert format_bytes(1024 ** 3) == "1.00 GB"
        assert format_bytes(int(1.5 * 1024 ** 3)) == "1.50 GB"


# ── Protobuf encoder ──────────────────────────────────────────────────────────


class TestEncodeQueryStatsRequest:
    def test_empty_request_is_empty_bytes(self):
        # pattern="" and reset=False are both default values in proto3
        # → all fields omitted → empty message
        result = encode_query_stats_request(pattern="", reset=False)
        assert result == b""

    def test_pattern_only(self):
        result = encode_query_stats_request(pattern="user>>>alice/")
        # Field 1 (string, wire type 2): tag=0x0a, then varint length, then UTF-8
        assert result[0:1] == b"\x0a"
        encoded = "user>>>alice/".encode("utf-8")
        assert result[1] == len(encoded)  # length byte (single byte for short strings)
        assert result[2:] == encoded

    def test_reset_flag(self):
        result = encode_query_stats_request(pattern="", reset=True)
        # Field 2 (bool, wire type 0): tag=0x10, value=0x01
        assert result == b"\x10\x01"

    def test_pattern_and_reset(self):
        result = encode_query_stats_request(pattern="x", reset=True)
        assert b"\x0a" in result  # field 1 tag
        assert b"\x10\x01" in result  # field 2 + value

    def test_unicode_pattern(self):
        # Ensure UTF-8 encoding works correctly
        result = encode_query_stats_request(pattern="tëst/")
        encoded = "tëst/".encode("utf-8")
        assert encoded in result


# ── Protobuf decoder ──────────────────────────────────────────────────────────


class TestDecodeStatMessage:
    def _build_stat_bytes(self, name: str, value: int) -> bytes:
        """Hand-encode a Stat message for testing."""
        buf = bytearray()
        # Field 1: name (string, wire type 2)
        enc_name = name.encode("utf-8")
        buf += b"\x0a" + _varint_encode(len(enc_name)) + enc_name
        # Field 2: value (int64, wire type 0)
        if value != 0:
            buf += b"\x10" + _varint_encode(value)
        return bytes(buf)

    def test_basic_stat(self):
        data = self._build_stat_bytes("user>>>alice/dev>>>traffic>>>uplink", 12345)
        result = _decode_stat_message(data)
        assert result is not None
        name, value = result
        assert name == "user>>>alice/dev>>>traffic>>>uplink"
        assert value == 12345

    def test_zero_value(self):
        data = self._build_stat_bytes("user>>>alice/dev>>>traffic>>>uplink", 0)
        result = _decode_stat_message(data)
        assert result is not None
        assert result[1] == 0

    def test_large_value(self):
        large = 10 * 1024 ** 3  # 10 GB in bytes
        data = self._build_stat_bytes("user>>>bob/phone>>>traffic>>>downlink", large)
        result = _decode_stat_message(data)
        assert result is not None
        assert result[1] == large

    def test_empty_bytes_returns_none(self):
        result = _decode_stat_message(b"")
        assert result is None

    def test_missing_name_returns_none(self):
        # Only encode value, no name
        buf = b"\x10" + _varint_encode(999)
        result = _decode_stat_message(buf)
        assert result is None


class TestDecodeQueryStatsResponse:
    def _build_response(self, stats: list[tuple[str, int]]) -> bytes:
        """Build a QueryStatsResponse containing the given (name, value) pairs."""
        buf = bytearray()
        for name, value in stats:
            # Encode Stat submessage
            sub = bytearray()
            enc_name = name.encode("utf-8")
            sub += b"\x0a" + _varint_encode(len(enc_name)) + enc_name
            if value != 0:
                sub += b"\x10" + _varint_encode(value)
            sub_bytes = bytes(sub)
            # Field 1: repeated Stat (wire type 2)
            buf += b"\x0a" + _varint_encode(len(sub_bytes)) + sub_bytes
        return bytes(buf)

    def test_empty_response(self):
        assert decode_query_stats_response(b"") == []

    def test_single_stat(self):
        data = self._build_response([("user>>>alice/dev>>>traffic>>>uplink", 500)])
        result = decode_query_stats_response(data)
        assert len(result) == 1
        assert result[0] == ("user>>>alice/dev>>>traffic>>>uplink", 500)

    def test_multiple_stats(self):
        pairs = [
            ("user>>>alice/dev1>>>traffic>>>uplink", 1000),
            ("user>>>alice/dev1>>>traffic>>>downlink", 5000),
            ("user>>>alice/dev2>>>traffic>>>uplink", 200),
            ("user>>>alice/dev2>>>traffic>>>downlink", 8000),
        ]
        data = self._build_response(pairs)
        result = decode_query_stats_response(data)
        assert len(result) == 4
        assert set(result) == set(pairs)

    def test_zero_value_stats_included(self):
        data = self._build_response([("user>>>alice/dev>>>traffic>>>uplink", 0)])
        result = decode_query_stats_response(data)
        # Zero-value stat with a name should still be present
        assert len(result) == 1
        assert result[0][1] == 0


# ── get_user_traffic ──────────────────────────────────────────────────────────


class TestGetUserTraffic:
    def _make_raw_stats(self, pairs: list[tuple[str, int]]) -> list[tuple[str, int]]:
        return pairs

    def test_returns_none_when_addr_empty(self):
        result = get_user_traffic(username="alice", xray_api_addr="")
        assert result is None

    def test_returns_none_when_username_empty(self):
        result = get_user_traffic(username="", xray_api_addr="127.0.0.1:10085")
        assert result is None

    def test_returns_none_when_api_unreachable(self):
        with patch("backend.xray_stats._call_query_stats", return_value=None):
            result = get_user_traffic(username="alice", xray_api_addr="127.0.0.1:10085")
        assert result is None

    def test_aggregates_uplink_and_downlink(self):
        raw = [
            ("user>>>alice/device-1>>>traffic>>>uplink", 1000),
            ("user>>>alice/device-1>>>traffic>>>downlink", 5000),
            ("user>>>alice/device-2>>>traffic>>>uplink", 200),
            ("user>>>alice/device-2>>>traffic>>>downlink", 8000),
        ]
        with patch("backend.xray_stats._call_query_stats", return_value=raw):
            result = get_user_traffic(username="alice", xray_api_addr="127.0.0.1:10085")

        assert result is not None
        assert result.upload_bytes == 1200      # 1000 + 200
        assert result.download_bytes == 13000   # 5000 + 8000
        assert result.total_bytes == 14200      # 1200 + 13000

    def test_returns_zero_stats_when_api_returns_empty_list(self):
        # API reachable, user has 0 traffic — this is honest zero, not None
        with patch("backend.xray_stats._call_query_stats", return_value=[]):
            result = get_user_traffic(username="alice", xray_api_addr="127.0.0.1:10085")

        assert result is not None
        assert result.upload_bytes == 0
        assert result.download_bytes == 0
        assert result.total_bytes == 0

    def test_single_device_one_direction(self):
        raw = [("user>>>bob/phone>>>traffic>>>downlink", 999_000_000)]
        with patch("backend.xray_stats._call_query_stats", return_value=raw):
            result = get_user_traffic(username="bob", xray_api_addr="127.0.0.1:10085")

        assert result is not None
        assert result.upload_bytes == 0
        assert result.download_bytes == 999_000_000
        assert result.total_bytes == 999_000_000

    def test_ignores_other_users_stats(self):
        # Pattern should be specific to the queried user; this test verifies
        # that stats for other users are not summed in (they should not appear
        # in the response when the pattern is correct, but we guard defensively).
        raw = [
            ("user>>>alice/dev>>>traffic>>>uplink", 1000),
            ("user>>>alice/dev>>>traffic>>>downlink", 2000),
        ]
        with patch("backend.xray_stats._call_query_stats", return_value=raw) as mock_fn:
            result = get_user_traffic(username="alice", xray_api_addr="127.0.0.1:10085")
            # Verify the correct pattern was used
            mock_fn.assert_called_once()
            call_kwargs = mock_fn.call_args
            assert "user>>>alice/" in (call_kwargs[1].get("pattern") or call_kwargs[0][1])

        assert result is not None
        assert result.total_bytes == 3000

    def test_uses_correct_pattern(self):
        with patch("backend.xray_stats._call_query_stats", return_value=[]) as mock_fn:
            get_user_traffic(username="testuser", xray_api_addr="127.0.0.1:10085")
            args, kwargs = mock_fn.call_args
            pattern = kwargs.get("pattern") or args[1]
            assert pattern == "user>>>testuser/"

    def test_large_traffic_values(self):
        # Multi-TB traffic should not overflow
        one_tb = 1024 ** 4
        raw = [
            ("user>>>biguser/server>>>traffic>>>uplink", one_tb),
            ("user>>>biguser/server>>>traffic>>>downlink", 2 * one_tb),
        ]
        with patch("backend.xray_stats._call_query_stats", return_value=raw):
            result = get_user_traffic(username="biguser", xray_api_addr="127.0.0.1:10085")

        assert result is not None
        assert result.total_bytes == 3 * one_tb


# ── Cabinet integration ───────────────────────────────────────────────────────


_HAPP_HEADERS = {
    "user-agent": "Happ/4.2.1",
    "x-hwid": "stats-test-hwid",
    "x-device-os": "iOS 17.0",
}


class TestCabinetTrafficDisplay:
    def test_shows_na_when_xray_api_not_configured(self, client, active_user):
        """When XRAY_API_ADDR is not set, cabinet shows N/A for traffic."""
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{**page_mod.settings.__dataclass_fields__,
               **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__},
               "xray_api_addr": None}
        )
        with patch.object(page_mod, "settings", patched):
            resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "N/A" in resp.text

    def test_shows_real_traffic_when_api_available(self, client, active_user):
        """Cabinet shows formatted bytes when Xray API returns stats."""
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        stats = UserTrafficStats(
            upload_bytes=10 * 1024 ** 2,    # 10 MB
            download_bytes=500 * 1024 ** 2,  # 500 MB
            total_bytes=510 * 1024 ** 2,
        )
        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic", return_value=stats):
                resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        # Total is 510 MB → "510.00 MB"
        assert "510.00 MB" in resp.text

    def test_shows_zero_traffic_honestly(self, client, active_user):
        """0 bytes of traffic shows '0 B', not 'N/A'."""
        import backend.routes.user_page as page_mod
        patched = page_mod.settings.__class__(
            **{f: getattr(page_mod.settings, f) for f in page_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        stats = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        with patch.object(page_mod, "settings", patched):
            with patch("backend.routes.user_page.get_user_traffic", return_value=stats):
                resp = client.get(f"/{active_user.public_token}")
        assert resp.status_code == 200
        assert "0 B" in resp.text
        assert "N/A" not in resp.text


# ── Happ subscription-userinfo integration ───────────────────────────────────


class TestHappUserinfoTraffic:
    def test_userinfo_contains_real_upload_and_download(self, client, active_user):
        """When Xray API is available, subscription-userinfo has real byte counts."""
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f) for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        stats = UserTrafficStats(
            upload_bytes=1_000_000,
            download_bytes=9_000_000,
            total_bytes=10_000_000,
        )
        with patch.object(profile_mod, "settings", patched):
            with patch("backend.routes.profile.get_user_traffic", return_value=stats):
                resp = client.get(f"/{active_user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert "upload=1000000" in userinfo
        assert "download=9000000" in userinfo
        assert "total=0" in userinfo  # quota unchanged

    def test_userinfo_uses_zero_when_api_unavailable(self, client, active_user):
        """When Xray API is unreachable, subscription-userinfo falls back to 0."""
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f) for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        with patch.object(profile_mod, "settings", patched):
            with patch("backend.routes.profile.get_user_traffic", return_value=None):
                resp = client.get(f"/{active_user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert "upload=0" in userinfo
        assert "download=0" in userinfo

    def test_userinfo_zero_bytes_is_honest_zero(self, client, active_user):
        """When API reports 0, subscription-userinfo shows 0 (not a fake value)."""
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f) for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        stats = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        with patch.object(profile_mod, "settings", patched):
            with patch("backend.routes.profile.get_user_traffic", return_value=stats):
                resp = client.get(f"/{active_user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert "upload=0" in userinfo
        assert "download=0" in userinfo

    def test_blocked_user_userinfo_unchanged(self, client, inactive_user):
        """Blocked users always get upload=0; download=0 regardless of stats config."""
        import backend.routes.profile as profile_mod
        patched = profile_mod.settings.__class__(
            **{f: getattr(profile_mod.settings, f) for f in profile_mod.settings.__dataclass_fields__}
            | {"xray_api_addr": "127.0.0.1:10085"}
        )
        with patch.object(profile_mod, "settings", patched):
            resp = client.get(f"/{inactive_user.public_token}", headers=_HAPP_HEADERS)

        assert resp.status_code == 200
        userinfo = resp.headers["subscription-userinfo"]
        assert "upload=0" in userinfo
        assert "download=0" in userinfo
        assert "total=0" in userinfo


# ── combined_traffic ──────────────────────────────────────────────────────────


class TestCombinedTraffic:
    def test_zero_stored_zero_live(self):
        live = UserTrafficStats(upload_bytes=0, download_bytes=0, total_bytes=0)
        result = combined_traffic(stored_up=0, stored_down=0, live=live)
        assert result.upload_bytes == 0
        assert result.download_bytes == 0
        assert result.total_bytes == 0

    def test_stored_only_when_live_is_none(self):
        result = combined_traffic(stored_up=100, stored_down=50, live=None)
        assert result.upload_bytes == 100
        assert result.download_bytes == 50
        assert result.total_bytes == 150

    def test_live_only_when_stored_is_zero(self):
        live = UserTrafficStats(
            upload_bytes=1_000_000, download_bytes=9_000_000, total_bytes=10_000_000
        )
        result = combined_traffic(stored_up=0, stored_down=0, live=live)
        assert result.upload_bytes == 1_000_000
        assert result.download_bytes == 9_000_000
        assert result.total_bytes == 10_000_000

    def test_stored_plus_live(self):
        live = UserTrafficStats(upload_bytes=10, download_bytes=20, total_bytes=30)
        result = combined_traffic(stored_up=100, stored_down=50, live=live)
        assert result.upload_bytes == 110
        assert result.download_bytes == 70
        assert result.total_bytes == 180

    def test_never_returns_none(self):
        result = combined_traffic(stored_up=0, stored_down=0, live=None)
        assert result is not None
        assert isinstance(result, UserTrafficStats)

    def test_xray_unavailable_preserves_stored(self):
        """When live=None (Xray down), return stored totals unchanged."""
        result = combined_traffic(stored_up=500_000_000, stored_down=2_000_000_000, live=None)
        assert result.total_bytes == 2_500_000_000
