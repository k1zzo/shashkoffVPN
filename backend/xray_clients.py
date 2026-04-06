"""Xray active client set management.

Builds the list of allowed Xray clients from DB device state and writes
it to a config file, then triggers an automatic Xray reload.

Per-device revocation model:
  Each active device with a device_uuid maps to one Xray client entry.
  Deactivating a device removes its entry from the clients list.
  Once Xray is reloaded (automatically, if XRAY_RELOAD_COMMAND is set),
  that UUID is no longer accepted → real VPN access revocation.

Transitional state:
  Two categories of entries are written:

  1. Per-device entries (primary model):
     Active devices with a non-null device_uuid. Happ clients receive
     their device_uuid in the VLESS URL and connect using it. Deleting
     a device removes its device_uuid from this list.

  2. User-level fallback entries (transitional/legacy):
     Each active user's user.uuid is also included. This covers:
       - Non-Happ clients using /open/{token} or /api/profile.
       - Legacy Happ installs still holding the old shared UUID.
     Remove this block in a future pass once all clients have migrated.

Entry point:
  apply_xray_client_changes(db, settings) — the single function to call
  after any device-set mutation. It writes the config and triggers reload.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Device, User

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


def build_active_xray_clients(db: Session) -> list[dict]:
    """Build Xray client entries for all currently allowed VPN connections.

    Returns a list of dicts suitable for the ``clients`` field in an Xray
    VLESS inbound settings block:

        {
          "inbounds": [{
            "protocol": "vless",
            "settings": {
              "clients": <return value of this function>,
              "decryption": "none"
            }
          }]
        }

    Source of truth: active Device rows with device_uuid, plus user.uuid
    for the transitional legacy path. Only users with is_active=True are
    included; expired users (expires_at in the past) are NOT filtered here —
    expiry is enforced at the subscription layer, not the Xray layer.
    Filtering by expiry here would risk cutting off active sessions mid-use.
    """
    clients: list[dict] = []
    seen_uuids: set[str] = set()

    # ── 1. Per-device entries (primary, new model) ────────────────────────────
    device_rows = db.execute(
        select(Device, User)
        .join(User, Device.user_id == User.id)
        .where(
            Device.is_active.is_(True),
            Device.device_uuid.isnot(None),
            User.is_active.is_(True),
        )
    ).all()

    for device, user in device_rows:
        if device.device_uuid in seen_uuids:
            continue  # shouldn't happen, but guard against dupes
        seen_uuids.add(device.device_uuid)
        # Truncate device_id to keep email labels readable in Xray logs.
        label = (device.device_id or "unknown")[:24]
        clients.append({
            "id": device.device_uuid,
            "email": f"{user.username}/{label}",
            "flow": "xtls-rprx-vision",
        })

    # ── 2. User-level fallback entries (transitional, legacy) ─────────────────
    # Include user.uuid for non-Happ clients and legacy Happ installs that
    # still hold the old shared UUID. Remove this block in a future pass once
    # all Happ clients have re-imported and user.uuid is safe to revoke.
    user_rows = db.scalars(
        select(User).where(User.is_active.is_(True))
    ).all()

    for user in user_rows:
        if user.uuid in seen_uuids:
            continue
        seen_uuids.add(user.uuid)
        clients.append({
            "id": user.uuid,
            "email": f"{user.username}/legacy",
            "flow": "xtls-rprx-vision",
        })

    return clients


def write_xray_clients_config(clients: list[dict], path: Path) -> None:
    """Write the Xray clients array to a JSON file atomically.

    Uses a write-then-rename pattern so Xray never sees a partial file.
    Creates parent directories if needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(clients, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)
    logger.info(
        "xray_clients: wrote %d client entries to %s", len(clients), path
    )


def apply_xray_client_changes(db: Session, settings: "Settings") -> None:
    """Rebuild clients config, write it atomically, then reload Xray.

    This is the single entry point for all device-set mutations that must
    be reflected in the running Xray process.

    Sequence:
      1. Build active client list from DB.
      2. Write to XRAY_CLIENTS_CONFIG_PATH (if configured). If write
         fails, log the error and return without attempting reload —
         a failed write must not cause a reload with stale data.
      3. Call reload_xray_if_configured (if XRAY_RELOAD_COMMAND is set).
         Reload failure is logged but never propagated to the caller.

    No-op when xray_clients_config_path is None (XRAY_CLIENTS_CONFIG_PATH
    not set). In that case neither write nor reload occurs, and revocation
    remains incomplete at the Xray layer.
    """
    if settings.xray_clients_config_path is None:
        return

    clients = build_active_xray_clients(db)
    try:
        write_xray_clients_config(clients, settings.xray_clients_config_path)
    except Exception as exc:
        logger.error(
            "xray_clients: config write failed — skipping Xray reload. "
            "The running Xray process was NOT updated. Error: %s",
            exc,
        )
        return

    # Import here to avoid a module-level cycle (xray_reload has no backend imports).
    from backend.xray_reload import reload_xray_if_configured
    reload_xray_if_configured(
        command=settings.xray_reload_command,
        timeout=settings.xray_reload_timeout,
    )
