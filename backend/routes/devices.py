from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

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

router = APIRouter(tags=["devices"])


class DeviceRegistrationRequest(BaseModel):
    token: str
    device_id: str
    device_name: str
    platform: str


class DeviceRemoveRequest(BaseModel):
    token: str
    device_id: str


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

    now = datetime.utcnow()
    existing_device = get_device(db, user.id, device_id)
    if existing_device is not None:
        existing_device.device_name = device_name
        existing_device.platform = platform
        existing_device.last_seen_at = now
        existing_device.is_active = True
        db.commit()
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
            first_seen_at=now,
            last_seen_at=now,
            is_active=True,
        )
    )
    db.commit()

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

    deactivate_device(db, device)

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
