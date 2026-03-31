from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models import Device, User

router = APIRouter(tags=["devices"])


class DeviceRegistrationRequest(BaseModel):
    token: str
    device_id: str
    device_name: str
    platform: str


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

    user = db.scalar(select(User).where(User.public_token == token))
    if user is None:
        return JSONResponse(status_code=404, content={"detail": "User not found"})

    if not user.is_active:
        return JSONResponse(status_code=403, content={"detail": "User is inactive"})

    now = datetime.utcnow()
    existing_device = db.scalar(
        select(Device).where(
            Device.user_id == user.id,
            Device.device_id == device_id,
        )
    )
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

    active_device_count = db.scalar(
        select(func.count(Device.id)).where(
            Device.user_id == user.id,
            Device.is_active.is_(True),
        )
    )
    if (active_device_count or 0) >= user.max_devices:
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
