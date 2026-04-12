"""Unit tests for backend.platform_utils."""
from __future__ import annotations

import pytest

from backend.platform_utils import (
    detect_device_type,
    device_type_icon,
    format_device_title,
    normalize_platform,
    platform_icon,
    resolve_device_type,
)


class TestNormalizePlatform:
    # ── iPad must be checked before iOS ──────────────────────────────────────
    def test_ipad_exact(self):
        assert normalize_platform("iPad") == "iPadOS"

    def test_ipados_exact(self):
        assert normalize_platform("iPadOS") == "iPadOS"

    def test_ipados_with_version(self):
        assert normalize_platform("iPadOS 17.2") == "iPadOS"

    def test_ipad_beats_ios_when_both_present(self):
        # Contrived but verifies priority: "ipad" matches before "ios" check
        assert normalize_platform("ipadios") == "iPadOS"

    # ── iOS / iPhone ─────────────────────────────────────────────────────────
    def test_ios_exact_lowercase(self):
        assert normalize_platform("ios") == "iOS"

    def test_ios_with_version(self):
        assert normalize_platform("iOS 17") == "iOS"

    def test_iphone_maps_to_ios(self):
        assert normalize_platform("iPhone") == "iOS"

    def test_iphone_with_model(self):
        assert normalize_platform("iPhone OS 16.5") == "iOS"

    # ── macOS ─────────────────────────────────────────────────────────────────
    def test_macos_exact(self):
        assert normalize_platform("macOS") == "macOS"

    def test_mac_maps_to_macos(self):
        assert normalize_platform("Mac") == "macOS"

    def test_darwin_maps_to_macos(self):
        assert normalize_platform("darwin") == "macOS"

    def test_macos_with_version(self):
        assert normalize_platform("macOS 14.4") == "macOS"

    # ── Android ───────────────────────────────────────────────────────────────
    def test_android_exact(self):
        assert normalize_platform("Android") == "Android"

    def test_android_with_version(self):
        assert normalize_platform("Android 13") == "Android"

    # ── Windows ───────────────────────────────────────────────────────────────
    def test_windows_exact_lowercase(self):
        assert normalize_platform("windows") == "Windows"

    def test_win_maps_to_windows(self):
        assert normalize_platform("Win") == "Windows"

    def test_windows_with_version(self):
        assert normalize_platform("Windows 11") == "Windows"

    # ── Linux ─────────────────────────────────────────────────────────────────
    def test_linux_exact(self):
        assert normalize_platform("Linux") == "Linux"

    def test_ubuntu_maps_to_linux(self):
        assert normalize_platform("Ubuntu") == "Linux"

    def test_debian_maps_to_linux(self):
        assert normalize_platform("Debian") == "Linux"

    def test_fedora_maps_to_linux(self):
        assert normalize_platform("Fedora") == "Linux"

    def test_arch_maps_to_linux(self):
        assert normalize_platform("Arch") == "Linux"

    # ── Fallback ──────────────────────────────────────────────────────────────
    def test_unknown_platform_uppercased(self):
        assert normalize_platform("BeOS") == "BEOS"

    def test_none_returns_unknown(self):
        assert normalize_platform(None) == "Unknown"

    def test_empty_string_returns_unknown(self):
        assert normalize_platform("") == "Unknown"

    def test_whitespace_only_returns_unknown(self):
        assert normalize_platform("   ") == "Unknown"


class TestPlatformIcon:
    def test_ipad(self):
        assert platform_icon("iPad") == "ipad.svg"

    def test_ipados_with_version(self):
        assert platform_icon("iPadOS 17") == "ipad.svg"

    def test_ios(self):
        assert platform_icon("ios") == "ios.svg"

    def test_iphone(self):
        assert platform_icon("iPhone") == "ios.svg"

    def test_macos(self):
        assert platform_icon("macOS") == "macos.svg"

    def test_darwin(self):
        assert platform_icon("darwin") == "macos.svg"

    def test_android(self):
        assert platform_icon("Android 13") == "android.svg"

    def test_windows(self):
        assert platform_icon("windows") == "windows.svg"

    def test_win(self):
        assert platform_icon("Win") == "windows.svg"

    def test_linux(self):
        assert platform_icon("Linux") == "linux.svg"

    def test_ubuntu(self):
        assert platform_icon("ubuntu") == "linux.svg"

    def test_unknown_platform_returns_unknown_svg(self):
        assert platform_icon("SomeOS") == "unknown.svg"

    def test_none_returns_unknown_svg(self):
        assert platform_icon(None) == "unknown.svg"

    def test_empty_returns_unknown_svg(self):
        assert platform_icon("") == "unknown.svg"


class TestFormatDeviceTitle:
    def test_normal_ios(self):
        assert format_device_title("iOS", "iPhone 15") == "iOS - iPhone 15"

    def test_platform_with_version(self):
        assert format_device_title("iOS 17", "iPhone 15") == "iOS - iPhone 15"

    def test_ipad_priority(self):
        assert format_device_title("iPad", "iPad Air") == "iPadOS - iPad Air"

    def test_android_with_version(self):
        assert format_device_title("Android 13", "Pixel 7") == "Android - Pixel 7"

    def test_none_device_name_becomes_unknown_device(self):
        assert format_device_title("Android", None) == "Android - Unknown device"

    def test_empty_device_name_becomes_unknown_device(self):
        assert format_device_title("Android", "") == "Android - Unknown device"

    def test_whitespace_device_name_becomes_unknown_device(self):
        assert format_device_title("Android", "   ") == "Android - Unknown device"

    def test_none_platform_produces_unknown_label(self):
        assert format_device_title(None, "My Device") == "Unknown - My Device"

    def test_both_none(self):
        assert format_device_title(None, None) == "Unknown - Unknown device"

    def test_unknown_platform_uppercased_in_title(self):
        assert format_device_title("BeOS", "Box") == "BEOS - Box"


class TestDetectDeviceType:
    # ── TV must fire before Android ───────────────────────────────────────────
    def test_android_tv_platform_is_tv_not_phone(self):
        assert detect_device_type("Android TV", "Samsung Smart TV") == "tv"

    def test_tvos_is_tv(self):
        assert detect_device_type("tvOS", "Apple TV") == "tv"

    def test_smart_tv_in_name(self):
        assert detect_device_type("Android", "Smart TV Box") == "tv"

    def test_android_smart_tv_pro_is_tv_not_phone(self):
        # "Smart TV Pro" contains "smart tv" — must win over the generic
        # "android" → phone rule even though platform is plain "Android".
        assert detect_device_type("Android", "Smart TV Pro") == "tv"

    def test_android_tv_platform_with_generic_name_is_tv(self):
        assert detect_device_type("Android TV", "Samsung TV") == "tv"

    # ── Tablet ────────────────────────────────────────────────────────────────
    def test_ipad_platform_is_tablet(self):
        assert detect_device_type("iPadOS", "iPad Air") == "tablet"

    def test_tablet_keyword_in_name(self):
        assert detect_device_type("Android", "Samsung Galaxy Tablet") == "tablet"

    # ── Phone ─────────────────────────────────────────────────────────────────
    def test_iphone_in_name_is_phone(self):
        assert detect_device_type("iOS", "iPhone 15 Pro") == "phone"

    def test_android_plain_is_phone(self):
        assert detect_device_type("Android 13", "Pixel 7") == "phone"

    def test_galaxy_in_name_is_phone(self):
        assert detect_device_type("Android", "Samsung Galaxy S24") == "phone"

    def test_mobile_keyword_is_phone(self):
        assert detect_device_type("Mobile", "Generic Phone") == "phone"

    # ── Laptop ────────────────────────────────────────────────────────────────
    def test_macbook_in_name_is_laptop(self):
        assert detect_device_type("macOS", "MacBook Pro") == "laptop"

    def test_laptop_keyword_in_name(self):
        assert detect_device_type("Windows", "Dell Laptop") == "laptop"

    def test_macos_mac_is_laptop(self):
        # "macOS - Mac" is the Happ fallback name when no x-device-model is sent.
        # Must resolve to laptop, not desktop.
        assert detect_device_type("macOS", "Mac") == "laptop"

    def test_macos_macbook_air_is_laptop(self):
        assert detect_device_type("macOS", "MacBook Air") == "laptop"

    # ── Desktop ───────────────────────────────────────────────────────────────
    def test_windows_is_desktop(self):
        assert detect_device_type("Windows 11", "DESKTOP-ABC") == "desktop"

    def test_linux_is_desktop(self):
        assert detect_device_type("Linux", "Ubuntu PC") == "desktop"

    def test_mac_mini_is_desktop(self):
        # "Mac mini" must resolve to desktop, not laptop.
        assert detect_device_type("macOS", "Mac mini") == "desktop"

    def test_imac_is_desktop(self):
        assert detect_device_type("macOS", "iMac") == "desktop"

    def test_darwin_is_desktop(self):
        assert detect_device_type("darwin", None) == "desktop"

    # ── Unknown ───────────────────────────────────────────────────────────────
    def test_none_both_is_unknown(self):
        assert detect_device_type(None, None) == "unknown"

    def test_empty_strings_is_unknown(self):
        assert detect_device_type("", "") == "unknown"

    def test_unrecognised_platform_is_unknown(self):
        assert detect_device_type("BeOS", "Workstation X") == "unknown"


class TestDeviceTypeIcon:
    def test_phone(self):
        assert device_type_icon("phone") == "phone.svg"

    def test_tablet(self):
        assert device_type_icon("tablet") == "tablet.svg"

    def test_laptop(self):
        assert device_type_icon("laptop") == "laptop.svg"

    def test_desktop(self):
        assert device_type_icon("desktop") == "desktop.svg"

    def test_tv(self):
        assert device_type_icon("tv") == "tv.svg"

    def test_legacy_pc_aliases_to_desktop(self):
        assert device_type_icon("pc") == "desktop.svg"

    def test_unknown_type_returns_unknown_svg(self):
        assert device_type_icon("unknown") == "unknown.svg"

    def test_empty_string_returns_unknown_svg(self):
        assert device_type_icon("") == "unknown.svg"


class TestResolveDeviceType:
    class _Device:
        """Minimal stand-in for the Device ORM object."""
        def __init__(self, device_type, platform=None, device_name=None):
            self.device_type = device_type
            self.platform = platform
            self.device_name = device_name

    def test_uses_stored_device_type_when_present(self):
        d = self._Device(device_type="tablet", platform="Android", device_name="Galaxy Tab")
        assert resolve_device_type(d) == "tablet"

    def test_falls_back_when_device_type_is_none(self):
        d = self._Device(device_type=None, platform="iOS", device_name="iPhone 15")
        assert resolve_device_type(d) == "phone"

    def test_falls_back_when_device_type_is_unknown(self):
        d = self._Device(device_type="unknown", platform="macOS", device_name="MacBook Air")
        assert resolve_device_type(d) == "laptop"

    def test_falls_back_when_device_type_is_empty_string(self):
        d = self._Device(device_type="", platform="Windows 11", device_name="PC")
        assert resolve_device_type(d) == "desktop"

    def test_legacy_pc_redetects_to_desktop_for_mac_mini(self):
        # "pc" is re-detected rather than passed through. "macOS - Mac mini"
        # re-detects as "desktop" (same cabinet icon as the old "pc" alias).
        d = self._Device(device_type="pc", platform="macOS", device_name="Mac mini")
        assert resolve_device_type(d) == "desktop"

    def test_legacy_pc_redetects_to_laptop_for_mac(self):
        # "macOS - Mac" was stored as "pc" before the laptop/desktop split.
        # Re-detection now returns "laptop" for the bare "Mac" device name.
        d = self._Device(device_type="pc", platform="macOS", device_name="Mac")
        assert resolve_device_type(d) == "laptop"

    def test_tv_wins_over_stored_phone(self):
        # A device registered before TV markers were checked against the model
        # may have "phone" stored. TV detection must still win at display time.
        d = self._Device(device_type="phone", platform="Android", device_name="Smart TV Pro")
        assert resolve_device_type(d) == "tv"

    def test_tv_stored_type_not_overridden_by_platform(self):
        d = self._Device(device_type="tv", platform="Android", device_name="Chromecast")
        assert resolve_device_type(d) == "tv"
