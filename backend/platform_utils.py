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

# Device-type detection: checked in priority order.
# tv must come before phone/android to handle "Android TV" correctly.
# laptop must come before desktop to handle "MacBook" before generic "mac".
_DEVICE_TYPE_MAP: list[tuple[list[str], str]] = [
    (["android tv", "smart tv", "tvos", " tv"], "tv"),
    (["ipad", "tablet"], "tablet"),
    (["iphone", "android", "mobile", "galaxy"], "phone"),
    (["macbook", "laptop"], "laptop"),
    (["windows", "linux", "ubuntu", "debian", "fedora", "arch", "pc", "mac", "darwin"], "desktop"),
]

# Maps device_type → icon filename.
# "pc" is a legacy DB value (stored before laptop/desktop split); alias to desktop.
_DEVICE_TYPE_ICON: dict[str, str] = {
    "phone": "phone.svg",
    "tablet": "tablet.svg",
    "laptop": "laptop.svg",
    "desktop": "desktop.svg",
    "tv": "tv.svg",
    "pc": "desktop.svg",  # legacy alias
}


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


def detect_device_type(platform: str | None, device_name: str | None) -> str:
    """Infer a device type from raw platform and device name strings.

    Checks both fields together (case-insensitive substring search) in
    priority order so that "Android TV" is caught as tv before the
    generic "android" → phone rule fires.

    Returns one of: phone | tablet | laptop | desktop | tv | unknown.
    """
    combined = " ".join(
        s.lower()
        for s in (platform or "", device_name or "")
        if s
    ).strip()
    if not combined:
        return "unknown"
    for substrings, device_type in _DEVICE_TYPE_MAP:
        if any(s in combined for s in substrings):
            return device_type
    return "unknown"


def resolve_device_type(device) -> str:
    """Return the best-available device type for a Device ORM object.

    Prefers device.device_type when it is present and meaningful.
    Falls back to detect_device_type() when the stored value is None,
    empty, or "unknown".
    """
    stored = (device.device_type or "").strip()
    if stored and stored != "unknown":
        return stored
    return detect_device_type(
        getattr(device, "platform", None),
        getattr(device, "device_name", None),
    )


def device_type_icon(device_type: str) -> str:
    """Return the icon filename for a resolved device type.

    Handles the legacy "pc" value (stored before the laptop/desktop split)
    by aliasing it to desktop.svg.  Returns "unknown.svg" for anything not
    in the map.
    """
    return _DEVICE_TYPE_ICON.get(device_type, "unknown.svg")
