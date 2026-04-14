"""Shared database query helpers and user access policy.

Thin wrappers around common lookups used across multiple route modules,
plus the single source of truth for "can this user use the product".
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import Device, User


def get_user_by_token(db: Session, token: str) -> User | None:
    return db.scalar(select(User).where(User.public_token == token))


def is_user_accessible(user: User) -> bool:
    """Return True if the user is allowed to use the product.

    A user is accessible when:
    - is_active is True, AND
    - expires_at is either NULL (never expires) or in the future.
    """
    if not user.is_active:
        return False
    # Fix J: replace deprecated utcnow(); strip tzinfo to compare with naive DB datetimes.
    if user.expires_at is not None and user.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        return False
    return True


def count_active_devices(db: Session, user_id: int) -> int:
    count = db.scalar(
        select(func.count(Device.id)).where(
            Device.user_id == user_id,
            Device.is_active.is_(True),
        )
    )
    return count or 0


def get_device(db: Session, user_id: int, device_id: str) -> Device | None:
    return db.scalar(
        select(Device).where(
            Device.user_id == user_id,
            Device.device_id == device_id,
        )
    )


def get_active_device(db: Session, user_id: int, device_id: str) -> Device | None:
    return db.scalar(
        select(Device).where(
            Device.user_id == user_id,
            Device.device_id == device_id,
            Device.is_active.is_(True),
        )
    )


def list_active_devices(db: Session, user_id: int) -> list[Device]:
    return list(
        db.scalars(
            select(Device)
            .where(Device.user_id == user_id, Device.is_active.is_(True))
            .order_by(Device.last_seen_at.desc())
        ).all()
    )


def deactivate_device(db: Session, device: Device) -> None:
    device.is_active = False
    device.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None)  # Fix J
    db.commit()
