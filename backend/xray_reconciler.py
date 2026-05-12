"""Periodic drift reconciler between the DB and the running Xray instance.

Background task that compares ``build_active_xray_clients`` (DB-derived
truth) against ``list_users`` (Xray runtime state) every
``XRAY_RECONCILER_INTERVAL_SECONDS`` and applies any add/remove operations
needed to converge the two.

Why this exists
---------------
Even with HandlerService applied synchronously on every device mutation,
runtime state can drift from the DB. A few examples we have observed or
must defend against:

  * The HandlerService call fails transiently with XrayApiUnavailable
    (e.g. Xray briefly restarted by an operator). The on-disk snapshot is
    still correct, but the live set is stale until the next reconciler
    pass.
  * A user's expires_at timestamp passes while no device operation
    touches their account. With the Fix-step-9 expiry filter,
    build_active_xray_clients stops returning their UUIDs — but Xray
    keeps serving them until somebody triggers a reload, OR the
    reconciler reaps them. This task is what makes expiry a real
    revocation rather than a label.
  * An operator hand-edits xray-clients.json (rare, but possible during
    incident response). The next reconciler pass brings live state in
    line with the file.

Behavior
--------
- Runs as an asyncio task started from the FastAPI lifespan handler in
  backend/app.py.
- One iteration: open a short DB session, compute the desired set,
  diff against Xray live set, apply add/remove.
- All errors are caught and logged. The loop never crashes; it must
  outlive the application.
- XrayApiUnavailable: skipped this iteration, INFO log entry.
- XrayApiError: caught, ERROR-logged with details; loop continues.
- DB or other Python errors: caught, ERROR-logged with traceback; loop
  continues.

The reconciler is a no-op when:
  - settings.xray_use_handler_api is False, OR
  - settings.xray_api_addr is unset.

CLI access
----------
backend/cli.py exposes ``xray-state`` and ``xray-reconcile`` for manual
drift inspection and one-shot reconciliation respectively.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from backend.xray_clients import build_desired_runtime_state
from backend.xray_handler_api import (
    AddStatus,
    RemoveStatus,
    XrayApiError,
    XrayApiUnavailable,
    add_user,
    list_users,
    remove_user,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)

# Cap the number of diff entries we log to keep WARNING lines readable.
_DRIFT_LOG_SAMPLE = 5


def _enabled(settings: "Settings") -> bool:
    return bool(
        getattr(settings, "xray_use_handler_api", True)
        and settings.xray_api_addr
    )


def run_reconciler_once(settings: "Settings") -> dict:
    """Run a single reconciliation pass synchronously.

    Returns a dict suitable for diagnostics output (CLI / tests):
        {
          "enabled": bool,
          "live": int,
          "desired": int,
          "to_add": int,
          "to_remove": int,
          "added": int,
          "already_present_same_uuid": int,
          "replaced_different_uuid": int,
          "removed": int,
          "skipped_already_absent": int,
          "skipped_no_email": int,
          "skipped_reason": str | None,
        }

    ``added`` / ``removed`` count REAL state changes only; the other
    counters track idempotent no-ops and decoder skips. The split is what
    catches the production drift scenario: prior versions reported
    ``removed: 3`` while every call returned "not found" and Xray state
    never changed.

    Never raises — exceptions are caught and recorded in ``skipped_reason``.
    """
    result: dict = {
        "enabled": False,
        "live": 0,
        "desired": 0,
        "to_add": 0,
        "to_remove": 0,
        "added": 0,
        "already_present_same_uuid": 0,
        "replaced_different_uuid": 0,
        "removed": 0,
        "skipped_already_absent": 0,
        "skipped_no_email": 0,
        "skipped_reason": None,
    }

    if not _enabled(settings):
        result["skipped_reason"] = "handler api disabled or XRAY_API_ADDR unset"
        return result
    result["enabled"] = True

    # Local imports avoid a top-level dependency on backend.db, which keeps
    # this module importable from CLI and unit tests that build their own
    # session factory.
    from backend.db import session_scope  # noqa: PLC0415

    addr = settings.xray_api_addr or ""
    tag = getattr(settings, "xray_vless_inbound_tag", "vless-reality-in")
    timeout = float(getattr(settings, "xray_handler_api_timeout", 5))

    try:
        with session_scope() as db:
            desired = build_desired_runtime_state(db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("xray_reconciler: DB read failed: %s", exc)
        result["skipped_reason"] = f"db error: {exc}"
        return result

    result["desired"] = len(desired)

    try:
        # ``live`` is sourced from HandlerService/GetInboundUsers — Xray's
        # runtime user list — NOT ListInbounds (which returns only config).
        # Before this distinction was understood, the reconciler reported a
        # false ``+1 -0`` drift on every tick because live was always empty.
        live = list_users(addr=addr, inbound_tag=tag, timeout=timeout)
    except XrayApiUnavailable as exc:
        logger.info(
            "xray_reconciler: Xray API unavailable, deferring reconciliation: %s",
            exc,
        )
        result["skipped_reason"] = "api unavailable"
        return result
    except XrayApiError as exc:
        logger.error("xray_reconciler: list_users failed: %s", exc)
        result["skipped_reason"] = f"list failed: {exc}"
        return result

    result["live"] = len(live)
    desired_ids = set(desired.keys())
    live_ids = set(live.keys())
    to_add = desired_ids - live_ids
    to_remove = live_ids - desired_ids
    result["to_add"] = len(to_add)
    result["to_remove"] = len(to_remove)

    if not to_add and not to_remove:
        logger.info(
            "xray_reconciler: no drift (live=%d desired=%d)",
            len(live), len(desired_ids),
        )
        return result

    sample_add = sorted(to_add)[:_DRIFT_LOG_SAMPLE]
    sample_remove = sorted(to_remove)[:_DRIFT_LOG_SAMPLE]
    logger.warning(
        "xray_reconciler: drift detected: +%d -%d (live=%d desired=%d) "
        "add_sample=%s remove_sample=%s",
        len(to_add), len(to_remove), len(live), len(desired_ids),
        sample_add, sample_remove,
    )

    for uuid in sorted(to_remove):
        email = live.get(uuid, "")
        if not email:
            logger.error(
                "xray_reconciler: cannot remove uuid=%s — Xray returned no "
                "email for this entry (decoder glitch?); skipping. Manual "
                "cleanup required (try xray-purge-orphans).",
                uuid,
            )
            result["skipped_no_email"] += 1
            continue
        try:
            status = remove_user(
                addr=addr, inbound_tag=tag, email=email, timeout=timeout,
            )
        except XrayApiUnavailable as exc:
            logger.info(
                "xray_reconciler: remove_user(email=%s) — API became unavailable mid-pass: %s",
                email, exc,
            )
            result["skipped_reason"] = "api unavailable mid-pass"
            return result
        except XrayApiError as exc:
            logger.error(
                "xray_reconciler: remove_user(email=%s) failed: %s",
                email, exc,
            )
            continue
        if status is RemoveStatus.REMOVED:
            result["removed"] += 1
        elif status is RemoveStatus.NOT_PRESENT:
            result["skipped_already_absent"] += 1

    for uuid in sorted(to_add):
        email = desired[uuid]
        try:
            status = add_user(
                addr=addr,
                inbound_tag=tag,
                device_uuid=uuid,
                email=email,
                timeout=timeout,
            )
        except XrayApiUnavailable as exc:
            logger.info(
                "xray_reconciler: add_user(email=%s) — API became unavailable mid-pass: %s",
                email, exc,
            )
            result["skipped_reason"] = "api unavailable mid-pass"
            return result
        except XrayApiError as exc:
            logger.error(
                "xray_reconciler: add_user(email=%s) failed: %s",
                email, exc,
            )
            continue
        if status is AddStatus.ADDED:
            result["added"] += 1
        elif status is AddStatus.ALREADY_PRESENT_SAME_UUID:
            result["already_present_same_uuid"] += 1
        elif status is AddStatus.REPLACED_DIFFERENT_UUID:
            result["replaced_different_uuid"] += 1

    logger.info(
        "xray_reconciler: drift resolved: added=%d replaced=%d "
        "already_present=%d removed=%d skipped_already_absent=%d "
        "skipped_no_email=%d",
        result["added"], result["replaced_different_uuid"],
        result["already_present_same_uuid"], result["removed"],
        result["skipped_already_absent"], result["skipped_no_email"],
    )
    return result


async def reconciler_loop(
    settings: "Settings",
    stop_event: asyncio.Event,
) -> None:
    """Async loop that runs run_reconciler_once at the configured interval.

    Designed to be created via asyncio.create_task from the FastAPI lifespan
    handler. Stops when ``stop_event`` is set; cancellation is supported as
    a fallback path.

    The loop never propagates exceptions out — a bug in the reconciler must
    not crash the application. Any unexpected error is logged with traceback
    and the loop waits one interval before retrying.
    """
    if not _enabled(settings):
        logger.info(
            "xray_reconciler: disabled (XRAY_USE_HANDLER_API=false or "
            "XRAY_API_ADDR unset) — loop not started"
        )
        return

    interval = max(5, int(getattr(settings, "xray_reconciler_interval", 60)))
    logger.info("xray_reconciler: loop starting, interval=%ds", interval)

    while not stop_event.is_set():
        try:
            await asyncio.to_thread(run_reconciler_once, settings)
        except Exception:  # noqa: BLE001
            # to_thread re-raises on the awaiting side; the inner function
            # already catches everything, but defend against a bug in this
            # outer layer.
            logger.exception("xray_reconciler: unexpected error in iteration")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            logger.info("xray_reconciler: loop cancelled")
            raise

    logger.info("xray_reconciler: loop stopped")
