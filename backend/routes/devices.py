from __future__ import annotations

from datetime import datetime, timezone
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.db import get_db
from backend.models import Device
from backend.queries import (
    count_active_devices,
    deactivate_device,
    get_active_device,
    get_device,
    get_user_by_token,
    is_user_accessible,
)
from backend.xray_clients import apply_xray_client_changes
from backend.xray_stats import snapshot_all_users_traffic_before_reload

settings = get_settings()
logger = logging.getLogger(__name__)

router = APIRouter(tags=["devices"])


# Fix F: max_length mirrors the DB column sizes (String(120)/String(60)) so
# SQLite never silently stores oversized strings from malicious clients.
class DeviceRegistrationRequest(BaseModel):
    token: str = Field(max_length=200)
    device_id: str = Field(max_length=120)
    device_name: str = Field(max_length=120)
    platform: str = Field(max_length=60)


class DeviceRemoveRequest(BaseModel):
    token: str = Field(max_length=200)
    device_id: str = Field(max_length=120)


@router.post("/api/device/register")
def register_device(
    payload: DeviceRegistrationRequest,
    db: Session = Depends(get_db),
) -> JSONResponse:
    token = payload.token.strip()
    device_id = payload.device_id.strip()
    device_name = payload.device_name.strip()
    platform = payload.platform.strip()

    if not token:
        return JSONResponse(status_code=400, content={"detail": "token is required"})

    if not device_id:
        return JSONResponse(status_code=400, content={"detail": "device_id is required"})

    if not device_name:
        return JSONResponse(status_code=400, content={"detail": "device_name is required"})

    if not platform:
        return JSONResponse(status_code=400, content={"detail": "platform is required"})

    user = get_user_by_token(db, token)
    if user is None:
        return JSONResponse(status_code=404, content={"detail": "User not found"})

    if not is_user_accessible(user):
        return JSONResponse(status_code=403, content={"detail": "User is inactive"})

    now = datetime.now(timezone.utc).replace(tzinfo=None)  # Fix J: replace deprecated utcnow()
    existing_device = get_device(db, user.id, device_id)
    if existing_device is not None:
        # Snapshot mutable state BEFORE mutation so change flags are accurate.
        was_inactive = not existing_device.is_active
        uuid_backfilled = existing_device.device_uuid is None

        # Fix A: enforce device limit before reactivating an inactive device.
        # An inactive device does not hold a slot, so reactivating it counts as
        # a new device for limit purposes — same logic as the Happ registration path.
        if was_inactive and count_active_devices(db, user.id) >= user.max_devices:
            return JSONResponse(status_code=403, content={"detail": "device limit reached"})

        existing_device.device_name = device_name
        existing_device.platform = platform
        existing_device.last_seen_at = now
        existing_device.is_active = True
        if uuid_backfilled:
            existing_device.device_uuid = str(uuid4())
        db.commit()

        # Only reload Xray when the active client set actually changed.
        # Metadata-only updates (name/platform/last_seen_at) on a known active
        # device must NOT trigger apply_xray_client_changes.
        if uuid_backfilled or was_inactive:
            reason = "uuid_backfilled" if uuid_backfilled else "reactivated"
            logger.info(
                "XRAY-APPLY: reason=%s token=%.8s device_id=%.24s",
                reason, token, device_id,
            )
            snapshot_all_users_traffic_before_reload(db=db, xray_api_addr=settings.xray_api_addr)
            apply_xray_client_changes(db, settings)

        return JSONResponse(
            content={
                "detail": "device already registered",
                "device_id": existing_device.device_id,
                "status": "updated",
            }
        )

    if count_active_devices(db, user.id) >= user.max_devices:
        return JSONResponse(
            status_code=403,
            content={"detail": "device limit reached"},
        )

    db.add(
        Device(
            user_id=user.id,
            device_id=device_id,
            device_name=device_name,
            platform=platform,
            device_uuid=str(uuid4()),
            first_seen_at=now,
            last_seen_at=now,
            is_active=True,
        )
    )
    db.commit()
    logger.info(
        "XRAY-APPLY: reason=new_device token=%.8s device_id=%.24s",
        token, device_id,
    )
    snapshot_all_users_traffic_before_reload(db=db, xray_api_addr=settings.xray_api_addr)
    apply_xray_client_changes(db, settings)

    return JSONResponse(
        content={
            "detail": "device registered",
            "device_id": device_id,
            "status": "created",
        }
    )


# NOTE: device removal is intentionally allowed for inactive/expired users.
# This is a cleanup operation — blocking it would prevent users from managing
# their devices before resubscribing. Only user existence is validated.

@router.post("/api/device/remove")
def remove_device(
    payload: DeviceRemoveRequest,
    db: Session = Depends(get_db),
) -> JSONResponse:
    token = payload.token.strip()
    device_id = payload.device_id.strip()

    if not token:
        return JSONResponse(status_code=400, content={"detail": "token is required"})

    if not device_id:
        return JSONResponse(status_code=400, content={"detail": "device_id is required"})

    user = get_user_by_token(db, token)
    if user is None:
        return JSONResponse(status_code=404, content={"detail": "User not found"})

    device = get_active_device(db, user.id, device_id)
    if device is None:
        return JSONResponse(status_code=404, content={"detail": "Device not found"})

    # ── Traffic snapshot ──────────────────────────────────────────────────────
    # Capture ALL active users' current Xray traffic before the reload that
    # follows device removal.  `systemctl reload xray` (SIGHUP) reinitialises
    # Xray's stats manager and resets every in-memory counter to zero — for
    # every user, not just the one acting on this request.  Without a bulk
    # snapshot, other users' accumulated live traffic is silently destroyed
    # on each reload, which produces visible regressions in their dashboards
    # ("10 GB → refresh → 8 GB → refresh → 11 GB").
    #
    # If Xray is unreachable, we proceed and accept that the pre-reload traffic
    # is not captured for this cycle — honest limitation, not a crash.
    snapshot_all_users_traffic_before_reload(
        db=db,
        xray_api_addr=settings.xray_api_addr,
    )
    # ─────────────────────────────────────────────────────────────────────────

    deactivate_device(db, device)
    # Rebuild Xray clients config to exclude this device's UUID.
    # After Xray is reloaded, the device_uuid is no longer accepted → real VPN
    # access revocation. Without Xray reload the config file is correct but the
    # running Xray process still holds the old client set in memory.
    logger.info(
        "XRAY-APPLY: reason=device_deleted token=%.8s device_id=%.24s",
        token, device_id,
    )
    apply_xray_client_changes(db, settings)

    active_device_count = count_active_devices(db, user.id)

    return JSONResponse(
        content={
            "detail": "device removed",
            "device_id": device_id,
            "status": "deactivated",
            "active_devices": active_device_count or 0,
            "max_devices": user.max_devices,
        }
    )
