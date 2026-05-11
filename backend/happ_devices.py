"""Happ device identity helpers and server-side DB registration.

Converts raw Happ request headers into normalised Device records.

Key invariants:
- Registration only happens when x-hwid is present. Without a stable hardware
  identifier we cannot safely attribute a subscription request to a specific
  device, so we skip registration entirely and serve the subscription as before.
- Device limit enforcement (max_devices) is the responsibility of the CALLER,
  not of register_or_update_happ_device(). The caller must check
  count_active_devices before calling this function for new devices.
- register_or_update_happ_device() returns a HappRegistrationResult with
  explicit boolean flags (created_new, reactivated, uuid_backfilled). Callers
  MUST use result.active_client_set_changed to decide whether to call
  apply_xray_client_changes() — never add implicit conditions in the caller.
- register_or_update_happ_device() FLUSHES the session but does NOT commit.
  The caller commits after apply_xray_client_changes succeeds so the DB
  write and Xray runtime application are atomic from the operator's
  perspective — an XrayApiError rolls back the device row too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Device
from backend.platform_utils import detect_device_type  # Fix G: unified device type classifier


@dataclass(frozen=True)
class HappRegistrationResult:
    """Structured result from register_or_update_happ_device.

    Contains the mutated device and explicit boolean flags for every change
    that affects the Xray active client set.  Callers must use
    active_client_set_changed to decide whether apply_xray_client_changes()
    is needed — do NOT re-derive this from the raw flags in the caller.

    Flags:
      created_new      — a new Device row was inserted (new HWID first seen)
      reactivated      — device was is_active=False, now set to True
      uuid_backfilled  — device had device_uuid=None, now assigned a UUID4

    Plain refreshes (known active device, already has UUID) set all flags to
    False — apply_xray_client_changes() must NOT be called in that case.
    """

    device: Device
    created_new: bool
    reactivated: bool
    uuid_backfilled: bool

    @property
    def active_client_set_changed(self) -> bool:
        """True when the Xray active client list changed as a result."""
        return self.created_new or self.reactivated or self.uuid_backfilled

    def xray_change_reason(self) -> str | None:
        """Human-readable reason string for logging, or None if no change."""
        if self.created_new:
            return "new_device"
        if self.reactivated:
            return "reactivated"
        if self.uuid_backfilled:
            return "uuid_backfilled"
        return None


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
    # TV — check model for TV markers BEFORE the generic Android/iOS → phone
    # fallback so "Android - Smart TV Pro" is not mis-classified as phone.
    if ("tvos" in os_lower or "android tv" in os_lower
            or "smart tv" in model_lower or " tv" in model_lower):
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
) -> HappRegistrationResult:
    """Upsert a device record from a Happ subscription request.

    Returns a HappRegistrationResult with explicit change flags.
    Use result.active_client_set_changed to decide whether
    apply_xray_client_changes() must be called — do NOT call it on plain
    refreshes where only last_seen_at or metadata changed.

    IMPORTANT: this function does NOT check max_devices. The caller is
    responsible for checking the limit before calling this function for
    new or inactive devices.

    device_uuid lifecycle:
      New device       → a fresh UUID4 is generated and persisted.
      Existing device  → device_uuid is preserved. If the row predates
                         this field (device_uuid is NULL), a UUID4 is
                         assigned now (legacy backfill). UUIDs are never
                         rotated on normal refresh.
      Reactivation     → same policy: reuse existing device_uuid so the
                         device reconnects with the same credential.
    """
    existing = db.scalar(
        select(Device).where(
            Device.user_id == user_id,
            Device.device_id == device_info.hwid,
        )
    )

    name = derive_device_name(device_info.os, device_info.model)
    # Fix G: use detect_device_type from platform_utils so stored device_type
    # matches what resolve_device_type() would compute at render time.
    # derive_device_type() returned legacy "pc"; detect_device_type() returns
    # the finer-grained "laptop"/"desktop" split, eliminating the immediate mismatch.
    device_type = detect_device_type(device_info.os, name)
    platform = device_info.os if device_info.os else "unknown"

    if existing is not None:
        # Snapshot mutable state BEFORE mutation so the result flags are accurate.
        reactivated = not existing.is_active
        uuid_backfilled = existing.device_uuid is None

        existing.device_name = name
        existing.platform = platform
        existing.device_type = device_type
        existing.last_seen_at = now
        existing.is_active = True
        # Backfill device_uuid for legacy rows that pre-date this field.
        # Never rotate an existing UUID — stable credential for active devices.
        if uuid_backfilled:
            existing.device_uuid = str(uuid4())
        # Flush staged writes so build_active_xray_clients (called later by
        # the route's apply_xray_client_changes) sees the new state. We do
        # NOT commit here — the caller commits after Xray runtime apply
        # succeeds so a failed runtime apply rolls this row back.
        db.flush()
        return HappRegistrationResult(
            device=existing,
            created_new=False,
            reactivated=reactivated,
            uuid_backfilled=uuid_backfilled,
        )

    device = Device(
        user_id=user_id,
        device_id=device_info.hwid,
        device_name=name,
        platform=platform,
        device_type=device_type,
        source="happ",
        device_uuid=str(uuid4()),
        first_seen_at=now,
        last_seen_at=now,
        is_active=True,
    )
    db.add(device)
    db.flush()
    return HappRegistrationResult(
        device=device,
        created_new=True,
        reactivated=False,
        uuid_backfilled=False,
    )
