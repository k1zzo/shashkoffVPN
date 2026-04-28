from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.config import get_settings
from backend.models import Base

logger = logging.getLogger(__name__)

settings = get_settings()


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return

    db_path = Path(database_url.removeprefix(prefix))
    db_path.parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent_dir(settings.database_url)
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def upgrade_db_schema() -> None:
    """Apply additive schema changes to existing tables.

    Called after init_db() on every startup. Only safe, additive changes
    (new nullable columns) belong here — never dropping or renaming.

    SQLAlchemy's create_all uses CREATE TABLE IF NOT EXISTS, so it never
    adds columns to tables that already exist. This function fills that gap
    for databases created before a column was introduced.

    Columns added here:
      devices.device_type  — Happ device category (phone/tablet/pc/tv/unknown)
      devices.source       — registration origin ("happ", "api", or NULL for legacy)
      devices.device_uuid  — per-device VPN UUID (UUID4); NULL for legacy rows
      users.traffic_up_bytes   — cumulative upload bytes from deleted devices (default 0)
      users.traffic_down_bytes — cumulative download bytes from deleted devices (default 0)
      users.traffic_up_high_water_bytes   — monotonic upload display floor (default 0)
      users.traffic_down_high_water_bytes — monotonic download display floor (default 0)
    """
    with engine.connect() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(devices)")).fetchall()
        }
        if "device_type" not in existing:
            conn.execute(text("ALTER TABLE devices ADD COLUMN device_type TEXT"))
            conn.commit()
        if "source" not in existing:
            conn.execute(text("ALTER TABLE devices ADD COLUMN source TEXT"))
            conn.commit()
        if "device_uuid" not in existing:
            conn.execute(text("ALTER TABLE devices ADD COLUMN device_uuid TEXT"))
            conn.commit()

        existing_users = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()
        }
        if "traffic_up_bytes" not in existing_users:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN traffic_up_bytes INTEGER NOT NULL DEFAULT 0")
            )
            conn.commit()
        if "traffic_down_bytes" not in existing_users:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN traffic_down_bytes INTEGER NOT NULL DEFAULT 0")
            )
            conn.commit()
        if "traffic_up_high_water_bytes" not in existing_users:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN traffic_up_high_water_bytes INTEGER NOT NULL DEFAULT 0")
            )
            conn.commit()
        if "traffic_down_high_water_bytes" not in existing_users:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN traffic_down_high_water_bytes INTEGER NOT NULL DEFAULT 0")
            )
            conn.commit()

        # Fix B: add unique indexes for device identity integrity.
        # SQLite does not support ALTER TABLE ADD CONSTRAINT, so unique indexes
        # are used instead. They are semantically equivalent and are reflected
        # in the SQLAlchemy UniqueConstraint added to the Device model.
        #
        # NOTE: if existing data has duplicate (user_id, device_id) or duplicate
        # device_uuid rows, index creation will fail — this surfaces data
        # integrity violations that need manual resolution before re-starting.
        try:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_user_device "
                "ON devices(user_id, device_id)"
            ))
            conn.commit()
        except Exception as exc:
            logger.warning(
                "upgrade_db_schema: could not create uq_devices_user_device — "
                "check for duplicate (user_id, device_id) rows: %s", exc
            )

        try:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_device_uuid "
                "ON devices(device_uuid) WHERE device_uuid IS NOT NULL"
            ))
            conn.commit()
        except Exception as exc:
            logger.warning(
                "upgrade_db_schema: could not create uq_devices_device_uuid — "
                "check for duplicate device_uuid rows: %s", exc
            )


def get_db() -> Generator[Session, None, None]:
    # Fix H: rollback on exception to match session_scope() behaviour and
    # prevent a session with uncommitted error state from being silently closed.
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
