from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    public_token: Mapped[str] = mapped_column(
        String(120), unique=True, index=True, nullable=False
    )
    uuid: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_devices: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    devices: Mapped[list["Device"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    device_name: Mapped[str] = mapped_column(String(120), nullable=False)
    platform: Mapped[str] = mapped_column(String(60), nullable=False)
    # device_type: phone | tablet | pc | tv | unknown
    # Derived from x-device-os / x-device-model on Happ registration.
    # NULL for devices registered before this field was added or via /api/device/register.
    device_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # source: "happ" | "api" | NULL (legacy / pre-field)
    # "happ" = registered by a Happ subscription request via x-hwid
    # "api"  = registered via POST /api/device/register
    # NULL   = registered before this field was added, or via /api/profile auto-register
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped[User] = relationship(back_populates="devices")
