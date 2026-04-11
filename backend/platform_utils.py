"""Platform normalization helpers for the device cabinet."""
from __future__ import annotations

# Each entry: (substrings_to_match, clean_label, icon_filename)
# Checked in order — iPad/iPadOS must come before iOS/iPhone.
_PLATFORM_MAP: list[tuple[list[str], str, str]] = [
    (["ipad", "ipados"], "iPadOS", "ipad.svg"),
    (["iphone", "ios"], "iOS", "ios.svg"),
    (["mac", "macos", "darwin"], "macOS", "macos.svg"),
    (["android"], "Android", "android.svg"),
    (["windows", "win"], "Windows", "windows.svg"),
    (["linux", "ubuntu", "debian", "fedora", "arch"], "Linux", "linux.svg"),
]


def normalize_platform(platform: str | None) -> str:
    """Return a clean OS label from a raw platform string.

    Matching is case-insensitive substring search in priority order so
    version suffixes ("iOS 17", "Android 13") are handled automatically.
    Returns platform.upper() for unrecognised values, "Unknown" for None/empty.
    """
    if not platform or not platform.strip():
        return "Unknown"
    p = platform.lower()
    for substrings, label, _ in _PLATFORM_MAP:
        if any(s in p for s in substrings):
            return label
    return platform.upper()


def platform_icon(platform: str | None) -> str:
    """Return the icon filename (e.g. 'ios.svg') for a raw platform string.

    Returns 'unknown.svg' for None, empty, or unrecognised values.
    """
    if not platform or not platform.strip():
        return "unknown.svg"
    p = platform.lower()
    for substrings, _, icon in _PLATFORM_MAP:
        if any(s in p for s in substrings):
            return icon
    return "unknown.svg"


def format_device_title(platform: str | None, device_name: str | None) -> str:
    """Return '<OS label> - <device name>' for display in the cabinet.

    If device_name is None or blank, substitutes 'Unknown device'.
    Never returns an empty string.
    """
    label = normalize_platform(platform)
    name = device_name.strip() if device_name and device_name.strip() else "Unknown device"
    return f"{label} - {name}"
