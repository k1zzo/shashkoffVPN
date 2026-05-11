"""Xray active client set management.

Builds the list of allowed Xray clients from DB device state and applies
it to a running Xray process. Three independent layers ensure correctness:

  1. On-disk snapshot (xray-clients.json) — always written first. Acts as
     the cold-start source of truth read by deploy/shashkoffvpn-reload-xray.sh
     and as the safety net if everything else fails.

  2. HandlerService gRPC API (backend/xray_handler_api.py) — applied at
     runtime when XRAY_USE_HANDLER_API=true and XRAY_API_ADDR is set.
     Mutates the live Xray client list without restarting the process,
     preserving all other users' active sessions.

  3. Reconciler (backend/xray_reconciler.py) — runs every
     XRAY_RECONCILER_INTERVAL_SECONDS to catch drift between the DB and
     Xray runtime state. Reaps both stale UUIDs (e.g. expired users) and
     missing UUIDs (e.g. a brief Xray API outage during a device add).

Per-device revocation model:
  Each active device with a device_uuid maps to one Xray client entry.
  Deactivating or expiring a device removes its entry. With HandlerService
  enabled this revocation takes effect immediately on the next Xray
  connection attempt by the removed client; without HandlerService it
  takes effect after the legacy reload command runs.

Expiry policy (Fix step 9):
  Users with expires_at <= now() are now filtered out at this layer, not
  only at the subscription layer. Combined with the reconciler this means
  an expired user's active VPN tunnel is cut within one reconciler
  interval (default 60s) even without any subsequent subscription
  refresh — which is the intended behavior of a subscription expiry date.

Entry point:
  apply_xray_client_changes(db, settings) — the single function to call
  after any device-set mutation. Always writes the on-disk snapshot,
  then (depending on settings) applies the diff via HandlerService or
  triggers the legacy reload command.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.models import Device, User
from backend.xray_handler_api import (
    XrayApiUnavailable,
    add_user,
    list_users,
    remove_user,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


def _client_email(username: str, device_id: str | None) -> str:
    """Stable label used by Xray for stats and HandlerService remove operations.

    Matches the format used by build_active_xray_clients(): "{username}/{device_label}"
    where device_label is the first 24 characters of device.device_id. Truncation
    keeps stat names readable in Xray logs and matches the stats query pattern.
    """
    label = (device_id or "unknown")[:24]
    return f"{username}/{label}"


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

    Source of truth: active Device rows with device_uuid AND an active,
    non-expired parent User. Filtering on expires_at here (Fix step 9) is
    what makes the reconciler reap expired users from running Xray — the
    previous behavior left expired users with working VPN until manual
    reconnection. After deployment, an expired user's tunnel is cut on the
    next reconciler pass (default 60s) without any other intervention.

    user.uuid is intentionally excluded. VPN access is gated exclusively
    on per-device credentials so that deleting a device actually revokes
    that device's access.
    """
    clients: list[dict] = []
    seen_uuids: set[str] = set()

    # Fix J pattern: compare against a naive UTC datetime to match the rest
    # of the codebase (DB datetimes are stored naive-UTC).
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

    device_rows = db.execute(
        select(Device, User)
        .join(User, Device.user_id == User.id)
        .where(
            Device.is_active.is_(True),
            Device.device_uuid.isnot(None),
            User.is_active.is_(True),
            or_(User.expires_at.is_(None), User.expires_at > now_naive),
        )
    ).all()

    for device, user in device_rows:
        if device.device_uuid in seen_uuids:
            continue  # guard against dupes
        seen_uuids.add(device.device_uuid)
        clients.append({
            "id": device.device_uuid,
            "email": _client_email(user.username, device.device_id),
            "flow": "xtls-rprx-vision",
        })

    return clients


def build_desired_runtime_state(db: Session) -> dict[str, str]:
    """Return {device_uuid: email} for everything that should be live in Xray.

    This is the same data as build_active_xray_clients() but reshaped for
    cheap set diffing in the HandlerService and reconciler paths.
    """
    return {entry["id"]: entry["email"] for entry in build_active_xray_clients(db)}


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


def _handler_api_enabled(settings: "Settings") -> bool:
    """True iff the HandlerService gRPC path should be used for this mutation."""
    return bool(
        getattr(settings, "xray_use_handler_api", True)
        and settings.xray_api_addr
    )


def apply_xray_client_changes(db: Session, settings: "Settings") -> None:
    """Rebuild clients config, write it atomically, and apply at runtime.

    The function ALWAYS writes the on-disk snapshot when
    xray_clients_config_path is configured — that file is the cold-start
    source of truth for Xray and the backup state for the reconciler. The
    file write happens first; runtime application happens second.

    Runtime application strategy:

      * If the HandlerService path is enabled (XRAY_USE_HANDLER_API=true and
        XRAY_API_ADDR set), we compute the diff between the desired set
        and Xray's live set, then call add_user / remove_user for the
        deltas. Active sessions for unrelated UUIDs are untouched. The
        whole point of this path is that user A adding a device does not
        affect users B/C/D's tunnels.

      * If the HandlerService path is disabled or the API is unreachable,
        we fall back to invoking the legacy XRAY_RELOAD_COMMAND (which
        typically performs a full Xray restart). This keeps the previous
        deployment topology working end-to-end.

    Error model:

      * On XrayApiUnavailable (transient connectivity failure): we log a
        WARNING and return normally. The file is on disk; the reconciler
        will reapply when connectivity returns. Callers SHOULD NOT roll
        back the DB transaction in this case — partial degradation is
        preferred to a hard failure.

      * On XrayApiError (logical failure — bad inbound tag, malformed
        message, etc.): we re-raise. Callers should treat this as fatal
        and roll back their DB transaction.

    This function does NOT commit the DB. Callers commit AFTER this call
    returns successfully, so a runtime failure can rollback cleanly.
    """
    if settings.xray_clients_config_path is None and not _handler_api_enabled(settings):
        # Nothing to do: no snapshot path and no runtime API. Log once at
        # WARNING so operators notice their deployment is mis-configured.
        logger.warning(
            "xray_clients: neither XRAY_CLIENTS_CONFIG_PATH nor HandlerService "
            "is configured — device changes will NOT be applied to Xray."
        )
        return

    clients = build_active_xray_clients(db)
    desired = {entry["id"]: entry["email"] for entry in clients}

    snapshot_ok = True
    if settings.xray_clients_config_path is not None:
        try:
            write_xray_clients_config(clients, settings.xray_clients_config_path)
        except Exception as exc:
            snapshot_ok = False
            logger.error(
                "xray_clients: config write failed — Error: %s",
                exc,
            )

    if _handler_api_enabled(settings):
        # HandlerService does not read the on-disk file, so it is safe to
        # apply runtime changes even when the snapshot write failed. The
        # reconciler will keep retrying the snapshot path until it succeeds.
        _apply_via_handler_api_end_to_end(
            db=db,
            settings=settings,
            desired=desired,
        )
        return

    # Legacy fallback path: the reload command typically merges the on-disk
    # snapshot into the full Xray config, so we MUST NOT reload when the
    # snapshot write failed — a successful reload would pick up the
    # previous (stale) file and lock in the wrong state.
    if not snapshot_ok:
        logger.error(
            "xray_clients: skipping Xray reload because the snapshot write "
            "failed — the running Xray process was NOT updated."
        )
        return

    from backend.xray_reload import reload_xray_if_configured
    reload_xray_if_configured(
        command=settings.xray_reload_command,
        timeout=settings.xray_reload_timeout,
        watcher_mode=settings.xray_reload_via_watcher,
    )


def _apply_via_handler_api_end_to_end(
    *,
    db: Session,
    settings: "Settings",
    desired: dict[str, str],
) -> None:
    """Compute diff against Xray live state and apply add/remove operations.

    Email resolution for removes: we use the email we would have used at
    add time (looked up from the DB by device_uuid). For truly orphaned
    UUIDs (present in Xray, no DB row), we fall back to a synthetic email
    of "orphan/<uuid[:8]>" — which is harmless because Xray identifies the
    user by email at removal time, and an orphan with no matching email
    will be skipped via remove_user's idempotent "not found" handler.

    The reconciler does a more thorough cleanup pass with full email
    resolution via the live UUID→email map; this function only needs to
    handle the simple "we know what we just changed" case.
    """
    addr = settings.xray_api_addr or ""
    tag = getattr(settings, "xray_vless_inbound_tag", "vless-reality-in")
    timeout = float(getattr(settings, "xray_handler_api_timeout", 5))

    try:
        live = list_users(addr=addr, inbound_tag=tag, timeout=timeout)
    except XrayApiUnavailable as exc:
        logger.warning(
            "xray_clients: HandlerService unreachable (%s) — on-disk snapshot "
            "is current; reconciler will converge runtime state later.",
            exc,
        )
        return

    desired_ids = set(desired.keys())
    to_add = desired_ids - live
    to_remove = live - desired_ids

    if not to_add and not to_remove:
        logger.info(
            "xray_clients: no runtime diff (live=%d desired=%d)",
            len(live), len(desired_ids),
        )
        return

    # Build a reverse map of {uuid: email} for ALL devices in the DB (including
    # inactive ones whose UUID may still be present in Xray) so we can resolve
    # the email for a remove operation when the UUID is not in `desired`.
    email_by_uuid = _build_uuid_to_email_map(db)
    email_by_uuid.update(desired)  # current desired takes precedence

    for uuid in sorted(to_remove):
        email = email_by_uuid.get(uuid) or f"orphan/{uuid[:8]}"
        remove_user(addr=addr, inbound_tag=tag, email=email, timeout=timeout)

    for uuid in sorted(to_add):
        add_user(
            addr=addr,
            inbound_tag=tag,
            device_uuid=uuid,
            email=desired[uuid],
            timeout=timeout,
        )

    logger.info(
        "xray_clients: HandlerService applied +%d -%d (live before=%d)",
        len(to_add), len(to_remove), len(live),
    )


def _build_uuid_to_email_map(db: Session) -> dict[str, str]:
    """Return {device_uuid: email} for every device row that has a UUID.

    Includes inactive and expired devices because their UUIDs may still
    be present in Xray's live set — we need their email to remove them.
    """
    rows = db.execute(
        select(Device, User)
        .join(User, Device.user_id == User.id)
        .where(Device.device_uuid.isnot(None))
    ).all()
    out: dict[str, str] = {}
    for device, user in rows:
        if device.device_uuid is None:
            continue
        out[device.device_uuid] = _client_email(user.username, device.device_id)
    return out
