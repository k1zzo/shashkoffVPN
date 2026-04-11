"""Unit tests for backend.platform_utils."""
from __future__ import annotations

import pytest

from backend.platform_utils import (
    format_device_title,
    normalize_platform,
    platform_icon,
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
