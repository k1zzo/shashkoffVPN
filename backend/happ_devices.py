"""Happ device identity helpers and server-side DB registration.

Converts raw Happ request headers into normalised Device records.

Key invariants:
- Registration only happens when x-hwid is present. Without a stable hardware
  identifier we cannot safely attribute a subscription request to a specific
  device, so we skip registration entirely and serve the subscription as before.
- Device limit enforcement (max_devices) is the responsibility of the CALLER,
  not of register_or_update_happ_device(). The caller must check
  count_active_devices before calling this function for new devices.
- VPN access model: all devices belonging to a user share the same per-user
  Xray UUID. Registering or deleting a device row affects the cabinet DB state
  only. It does NOT yet perform per-device Xray UUID revocation. This is an
  explicit staged limitation — see the next-stage notes in CLAUDE.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Device


@dataclass(frozen=True)
class HappDeviceInfo:
    """Normalised device identity extracted from a Happ subscription request.

    Fields map directly to the request headers Happ sends on import/refresh:
      hwid        ← x-hwid        (hardware identifier, stable across reinstalls)
      os          ← x-device-os   ("iOS 17.0", "macOS 14.0", etc.)
      model       ← x-device-model ("iPhone 15 Pro", "Mac mini", etc.)
      app_version ← x-app-version
      user_agent  ← user-agent
    """

    hwid: str
    os: str           # empty string when header is absent
    model: str        # empty string when header is absent
    app_version: str  # empty string when header is absent
    user_agent: str   # empty string when header is absent


def extract_happ_device_info(request: Request) -> HappDeviceInfo | None:
    """Extract device identity from Happ request headers.

    Returns None when x-hwid is absent — callers must treat None as
    "unknown device, skip registration" and still serve the subscription.
    """
    hwid = request.headers.get("x-hwid", "").strip()
    if not hwid:
        return None
    return HappDeviceInfo(
        hwid=hwid,
        os=request.headers.get("x-device-os", "").strip(),
        model=request.headers.get("x-device-model", "").strip(),
        app_version=request.headers.get("x-app-version", "").strip(),
        user_agent=request.headers.get("user-agent", "").strip(),
    )


def derive_device_name(os: str, model: str) -> str:
    """Return a human-readable device label for the cabinet.

    Prefers the x-device-model string when present (e.g. "iPhone 15 Pro").
    Falls back to OS-derived labels when model is absent.
    """
    clean_model = model.strip()
    if clean_model:
        return clean_model

    os_lower = os.strip().lower()
    if "iphone" in os_lower:
        return "iPhone"
    if "ipad" in os_lower:
        return "iPad"
    if "ios" in os_lower:
        return "iOS Device"
    if "macos" in os_lower or "mac os" in os_lower:
        return "Mac"
    if "android" in os_lower:
        return "Android Device"
    if "windows" in os_lower:
        return "Windows PC"
    if "linux" in os_lower:
        return "Linux PC"
    return "Mobile Device"


def derive_device_type(os: str, model: str) -> str:
    """Return a device type category.

    Categories: phone | tablet | pc | tv | unknown

    This field is stored for display purposes and future per-type limit
    enforcement. It does NOT currently affect max_devices enforcement —
    the overall active-device count is used for that.
    """
    os_lower = os.strip().lower()
    model_lower = model.strip().lower()

    # Explicit iPhone signals
    if "iphone" in os_lower or "iphone" in model_lower:
        return "phone"
    # Explicit iPad signals
    if "ipad" in os_lower or "ipad" in model_lower:
        return "tablet"
    # Desktop OSes
    if "macos" in os_lower or "mac os" in os_lower:
        return "pc"
    if "windows" in os_lower:
        return "pc"
    if "linux" in os_lower:
        return "pc"
    # TV
    if "tvos" in os_lower or "android tv" in os_lower:
        return "tv"
    # Generic iOS without model hint → most likely iPhone
    if "ios" in os_lower:
        return "phone"
    # Generic Android without model hint → most likely phone
    if "android" in os_lower:
        return "phone"
    return "unknown"


def register_or_update_happ_device(
    db: Session,
    user_id: int,
    device_info: HappDeviceInfo,
    now: datetime,
) -> tuple[Device, bool]:
    """Upsert a device record from a Happ subscription request.

    Returns (device, is_new):
      is_new=True  — a new Device row was inserted and committed.
      is_new=False — an existing device was found; metadata and
                     last_seen_at were updated and committed.

    IMPORTANT: this function does NOT check max_devices. The caller
    is responsible for checking count_active_devices < user.max_devices
    before calling this function for new devices (is_new=True path).

    Reactivation: if a device was previously deactivated (is_active=False)
    and reconnects with the same HWID, it is reactivated. Deactivation from
    the cabinet frees a DB slot, but if the same hardware reconnects the
    device is recognised and reactivated. This is intentional — full
    permanent blocking at the Xray layer is not yet implemented.
    """
    existing = db.scalar(
        select(Device).where(
            Device.user_id == user_id,
            Device.device_id == device_info.hwid,
        )
    )

    name = derive_device_name(device_info.os, device_info.model)
    device_type = derive_device_type(device_info.os, device_info.model)
    platform = device_info.os if device_info.os else "unknown"

    if existing is not None:
        existing.device_name = name
        existing.platform = platform
        existing.device_type = device_type
        existing.last_seen_at = now
        existing.is_active = True
        db.commit()
        return existing, False

    device = Device(
        user_id=user_id,
        device_id=device_info.hwid,
        device_name=name,
        platform=platform,
        device_type=device_type,
        source="happ",
        first_seen_at=now,
        last_seen_at=now,
        is_active=True,
    )
    db.add(device)
    db.commit()
    return device, True
