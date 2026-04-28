from base64 import b64encode
from datetime import datetime, timezone
import json
import logging
import re
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.db import get_db
from backend.happ_devices import extract_happ_device_info, register_or_update_happ_device
from backend.models import Device, User
from backend.queries import (
    count_active_devices,
    get_device,
    get_user_by_token,
    is_user_accessible,
    list_active_devices,
)
from backend.subscription_utils import build_subscription_url, build_vless_url
from backend.url_utils import build_app_url
from backend.xray_clients import apply_xray_client_changes
from backend.xray_config_generator import build_xray_config
from backend.xray_stats import (
    get_user_traffic_active,
    monotonic_combined_traffic,
    snapshot_all_users_traffic_before_reload,
)

router = APIRouter(tags=["profile"])
settings = get_settings()
logger = logging.getLogger(__name__)

# ── TEMP: HAPP REQUEST INSPECTION ────────────────────────────────────────────
# Purpose: observe what Happ actually sends on subscription import/refresh so
# we can determine whether it includes a stable per-device identifier (HWID,
# InstallID, etc.) that could support server-side registration.
#
# Gated by: DEBUG_HAPP_SUB_REQUESTS=true in .env
# Log target: root logger at WARNING level — guaranteed visible in Docker/uvicorn
# Grep for: [HAPP-DIAG]
#
# To enable on the server:
#   Add DEBUG_HAPP_SUB_REQUESTS=true to .env and restart the container.
#   docker compose logs -f | grep HAPP-DIAG
#
# To remove later: delete this block and the _log_happ_sub_request() call in
# build_happ_subscription_response(). Also remove debug_happ_sub_requests from
# config.py and .env.example.
# ─────────────────────────────────────────────────────────────────────────────

# Headers that may contain secrets — redact their values in diagnostics logs.
_REDACTED_HEADER_NAMES: frozenset[str] = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth",
    "x-auth-token",
    "x-secret",
})


def _log_happ_sub_request(request: Request, token: str) -> None:
    """TEMP: Emit diagnostics for an incoming Happ subscription request via root logger WARNING.

    Uses logging.warning() (root logger) so output is guaranteed to appear in
    Docker/uvicorn stdout regardless of how named loggers are configured.

    Call this only when settings.debug_happ_sub_requests is True.
    """
    safe_token = f"{token[:8]}..." if len(token) > 8 else token

    # Client IP: trust X-Forwarded-For when running behind a reverse proxy.
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    elif request.client:
        client_ip = request.client.host
    else:
        client_ip = "unknown"

    raw_query = str(request.url.query) or "(none)"
    parsed_params = dict(request.query_params)

    sanitized_headers: dict[str, str] = {}
    for name, value in request.headers.items():
        sanitized_headers[name] = "[REDACTED]" if name.lower(
        ) in _REDACTED_HEADER_NAMES else value

    # One-line summary — easy to grep in Docker logs.
    logging.warning(
        "[HAPP-DIAG] token=%s ip=%s path=%s qs=%s",
        safe_token, client_ip, request.url.path, raw_query,
    )
    # Structured detail block.
    logging.warning("[HAPP-DIAG] query_params=%s",
                    json.dumps(parsed_params, ensure_ascii=False))
    logging.warning("[HAPP-DIAG] headers=%s",
                    json.dumps(sanitized_headers, ensure_ascii=False))

# ── END TEMP: HAPP REQUEST INSPECTION ────────────────────────────────────────


# Stable far-future timestamp used when a user has no expires_at (unlimited plan).
# 2099-12-31 00:00:00 UTC — avoids a sliding "now + 30 days" that changes on every request.
_UNLIMITED_EXPIRE_TS: int = 4102444800

# Happ profile-update-interval is in hours.
_HAPP_UPDATE_INTERVAL_HOURS: str = "1"


def _error_response(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _json_or_download_response(
    uuid: str,
    mode: str,
    username: str,
) -> Response:
    url = build_vless_url(
        user_uuid=uuid,
        settings=settings,
        server_description=settings.happ_server_description,
    )

    if mode == "download":
        filename = f"{re.sub(r'[^a-zA-Z0-9_-]', '-', username).strip('-') or 'user'}.txt"
        return Response(
            content=url,
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    return Response(content=url, media_type="text/plain")


@router.get("/open/{token}")
def open_profile(
    token: str,
    device_id: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    protect: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Response:
    if protect == "1" and (device_id is None or not device_id.strip()):
        return JSONResponse(
            content={
                "detail": "protected placeholder",
                "message": "device_id is required for protected access",
            }
        )

    clean_token = token.strip()
    if not clean_token:
        return _error_response(400, "token is required")

    user = get_user_by_token(db, clean_token)
    if user is None:
        return _error_response(404, "Profile not found")
    if not is_user_accessible(user):
        return _error_response(403, "User is inactive")

    # Resolve per-device UUID if device_id is provided.
    effective_uuid = user.uuid
    clean_device_id = device_id.strip() if device_id else ""
    if clean_device_id:
        device = get_device(db, user.id, clean_device_id)
        if device and device.device_uuid:
            effective_uuid = device.device_uuid

    clean_mode = (mode or "raw").strip().lower()
    return _json_or_download_response(
        uuid=effective_uuid,
        mode=clean_mode,
        username=user.username,
    )


def _happ_device_limit_response(user: "User") -> Response:
    """Return a Happ-compatible blocked response when max_devices is exceeded.

    Only returned for newly seen HWIDs. Known active devices always refresh
    successfully regardless of current device count.

    Uses the same header shape as the inactive/expired blocked response so
    Happ handles it consistently (empty body, human-readable profile-title).
    """
    profile_title = b64encode(
        "ЛИМИТ УСТРОЙСТВ ДОСТИГНУТ".encode("utf-8")
    ).decode("utf-8")
    # Fix C: treat stored naive datetime as UTC before converting to Unix timestamp.
    # datetime.timestamp() interprets naive datetimes as local time, which is
    # wrong on non-UTC servers and across DST boundaries.
    expire_ts = (
        int(user.expires_at.replace(tzinfo=timezone.utc).timestamp())
        if user.expires_at else _UNLIMITED_EXPIRE_TS
    )
    return Response(
        content="",
        media_type="text/plain",
        headers={
            "profile-title": f"base64:{profile_title}",
            "subscription-userinfo": f"upload=0; download=0; total=0; expire={expire_ts}",
            "profile-update-interval": _HAPP_UPDATE_INTERVAL_HOURS,
            "cache-control": "no-store",
            "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
        },
    )


def build_happ_subscription_response(
    user: "User",
    request: Request,
    *,
    token: str,
    db: Session,
) -> Response:
    """Build the Happ subscription response for a Happ subscription client.

    Called from /{token} when the request is identified as a Happ client.
    Both active and blocked/expired users are handled here.

    Device registration (when x-hwid is present):
      - Known active device → update metadata + last_seen_at, serve subscription.
      - New device, under limit → register device, serve subscription.
      - New device, at limit → return device-limit blocked response.
      - Known inactive (deleted) device, under limit → reactivate, serve subscription.
      - Known inactive (deleted) device, at limit → return device-limit blocked response.
      - No x-hwid → skip registration, serve subscription unchanged.

    Limit enforcement: strictly covers both new and reactivating devices.
    Only currently active devices refresh without a limit check.
    """
    # TEMP: emit diagnostics when DEBUG_HAPP_SUB_REQUESTS=true.
    # Fires for /{token} Happ requests.
    # Grep for [HAPP-DIAG] in container logs.
    if settings.debug_happ_sub_requests:
        _log_happ_sub_request(request, token)

    if not is_user_accessible(user):
        # Fix J: replace deprecated utcnow(); strip tzinfo for comparison with naive DB datetimes.
        _now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        expired = user.expires_at is not None and user.expires_at <= _now_utc
        blocked_label = "СРОК ДЕЙСТВИЯ ИСТЕК" if expired else "ПОДПИСКА ОТКЛЮЧЕНА"
        profile_title = b64encode(
            blocked_label.encode("utf-8")).decode("utf-8")
        # Use actual expiry timestamp when present; fall back to 0 (signals
        # expired/invalid to Happ clients that parse subscription-userinfo).
        # Fix C: attach UTC tzinfo before converting to Unix timestamp.
        expire_ts = (
            int(user.expires_at.replace(tzinfo=timezone.utc).timestamp())
            if user.expires_at else 0
        )
        return Response(
            content="",
            media_type="text/plain",
            headers={
                "profile-title": f"base64:{profile_title}",
                "subscription-userinfo": f"upload=0; download=0; total=0; expire={expire_ts}",
                "profile-update-interval": _HAPP_UPDATE_INTERVAL_HOURS,
                "cache-control": "no-store",
                "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
            },
        )

    # ── Server-side device registration from Happ headers ────────────────────
    #
    # Extract the hardware identifier Happ sends in every subscription request.
    # If x-hwid is absent we skip registration — no fake device rows are created.
    #
    # For known devices: update metadata + last_seen_at, reactivate if needed.
    # For new devices:   enforce max_devices before inserting.
    #
    # Note: count_active_devices is queried BEFORE registration so the check
    # correctly reflects the current state without the candidate device.
    happ_device: Device | None = None
    device_info = extract_happ_device_info(request)
    if device_info is not None:
        pre_existing = get_device(db, user.id, device_info.hwid)

        # Fix A (verified correct): the Happ path already enforces the limit
        # before reactivation.  Only currently active devices refresh freely.
        # New devices (no row) and deleted/inactive devices must pass the check.
        # This prevents a deleted device from silently reactivating and exceeding
        # the device cap when another device has already claimed the slot.
        is_known_active = pre_existing is not None and pre_existing.is_active
        if not is_known_active and count_active_devices(db, user.id) >= user.max_devices:
            return _happ_device_limit_response(user)

        result = register_or_update_happ_device(
            db, user.id, device_info, datetime.now(
                timezone.utc).replace(tzinfo=None)  # Fix J
        )
        happ_device = result.device
        # Only reload Xray when the active client set actually changed.
        # Plain refreshes (known active device, last_seen_at / metadata update)
        # must NOT trigger apply_xray_client_changes — the reload is expensive.
        if result.active_client_set_changed:
            logger.info(
                "XRAY-APPLY: reason=%s token=%.8s device_id=%.24s",
                result.xray_change_reason(), token, device_info.hwid,
            )
            snapshot_all_users_traffic_before_reload(
                db=db, xray_api_addr=settings.xray_api_addr)
            apply_xray_client_changes(db, settings)
    # ─────────────────────────────────────────────────────────────────────────

    profile_title = b64encode(
        "SHASHKOFF VPN".encode("utf-8")).decode("utf-8")
    announce = b64encode(
        f"Subscription | {user.username}".encode("utf-8")).decode("utf-8")
    # Fix C: attach UTC tzinfo before converting to Unix timestamp.
    expire_ts = (
        int(user.expires_at.replace(tzinfo=timezone.utc).timestamp())
        if user.expires_at else _UNLIMITED_EXPIRE_TS
    )

    # /{token} is now the canonical personal link — both the web page and the
    # subscription URL resolve to it (browser gets cabinet, Happ gets VLESS).
    canonical_url = build_subscription_url(
        token=user.public_token,
        request=request,
        settings=settings,
    )
    fallback_url = build_app_url(
        path=f"/{user.public_token}",
        request=request,
        settings=settings,
    )

    # ── Happ HWID device-cap headers ─────────────────────────────────────
    #
    # These three headers request the "local HWID enforcement" model:
    # Happ tracks hardware IDs inside the app, refuses the (N+1)th unique
    # device, and does not need a server callback to validate.
    #
    #   x-hwid-active: true
    #       Enables HWID tracking for this subscription.
    #
    #   x-hwid-limit: <N>  (user.max_devices, numeric string)
    #       The maximum number of distinct HWIDs Happ should allow.
    #       Must be a number — the previous value "true" was likely ignored.
    #
    #   subscription-always-hwid-enable: 1
    #       Forces Happ to use HWID tracking even when no server HWID API
    #       is present. This is the intended signal for "enforce locally."
    #
    # NOTE: x-hwid-not-supported was removed. Its plain meaning ("HWID is
    #   not supported") directly contradicts x-hwid-active: true. A real-
    #   device test (max_devices=1, two devices) confirmed that the full
    #   4-header combo did NOT block the second device. The most likely
    #   cause: x-hwid-not-supported overrides the active headers and
    #   disables HWID enforcement entirely. subscription-always-hwid-enable
    #   already covers the "no server callback — enforce locally" intent
    #   and is the unambiguous replacement.
    #   STILL UNKNOWN: whether removing it fixes enforcement. Requires a
    #   real 2-device test (limit=1) to confirm.
    #
    #   providerid: 6QlMYR5q
    #       Identifies this provider to Happ. UNKNOWN whether this ID is
    #       registered with Happ or has any special HWID behavior attached.
    #       If it is unrecognised, Happ may fall back to default enforcement.
    #
    # ── Source-of-truth boundary ─────────────────────────────────────────
    #
    # ARCHITECTURE: Hybrid (B).
    # Happ is responsible for enforcing the device cap on Happ-imported
    # subscriptions (if the headers above work as described).
    # Our SQLite DB is responsible for everything else:
    #   - The user cabinet device list
    #   - Device names and platform labels
    #   - last_seen_at timestamps
    #   - Device removal ("free a slot") for the DB layer
    #   - Device limit enforcement for non-Happ clients (/api/device/register)
    #
    # KNOWN LIMITATIONS:
    #   - Happ HWID state is opaque: we never receive the HWID list or the
    #     active HWID count. The cabinet will always show 0 devices for users
    #     who only import via Happ and never call /api/device/register.
    #   - Removing a device in the cabinet sets is_active=False in our DB.
    #     It does NOT remove the HWID from Happ's local storage. A removed
    #     cabinet device will still occupy a Happ slot until the user manually
    #     removes it inside the Happ app.
    #   - Device names and last_seen_at in the cabinet reflect DB state only
    #     (what the client told us at registration). They are not sourced from
    #     Happ and do not reflect actual connection activity for Happ users.
    # ── Traffic stats: stored historical + live active devices ───────────────
    # upload/download in subscription-userinfo are the cumulative bytes consumed
    # by the user. total=0 means no quota (unlimited).
    #
    # We query only active-device stats from Xray (get_user_traffic_active) to
    # avoid double-counting deleted devices whose traffic was already snapshotted
    # into user.traffic_up/down_bytes at deletion time.
    #
    # monotonic_combined_traffic() always returns a non-None result and
    # clamps against user.traffic_*_high_water_bytes so the displayed total
    # never decreases across calls — even when Xray is briefly unreachable
    # (live=None) or a reload-induced live-counter reset slips through.
    _device_rows = list_active_devices(db, user.id)
    _active_labels = frozenset(d.device_id[:24] for d in _device_rows)
    _live = None
    if settings.xray_api_addr:
        _live = get_user_traffic_active(
            username=user.username,
            active_labels=_active_labels,
            xray_api_addr=settings.xray_api_addr,
        )
    _total = monotonic_combined_traffic(db=db, user=user, live=_live)
    _upload_bytes = _total.upload_bytes
    _download_bytes = _total.download_bytes
    # ─────────────────────────────────────────────────────────────────────────

    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Content-Disposition": f'attachment; filename="user_{user.id}_{user.public_token}"',
        "fallback-url": fallback_url,
        "mux-enable": "0",
        "notification-subs-expire": "1",
        "profile-title": f"base64:{profile_title}",
        "profile-update-interval": _HAPP_UPDATE_INTERVAL_HOURS,
        "profile-web-page-url": canonical_url,
        "providerid": "IwssWKy1",
        "subscription-always-hwid-enable": "1",
        "subscription-userinfo": f"upload={_upload_bytes}; download={_download_bytes}; total=0; expire={expire_ts}",
        "subscriptions-collapse": "0",
        "support-url": "https://t.me/freeretard",
        "announce": f"base64:{announce}",
        "x-hwid-active": "true",
        "x-hwid-limit": str(user.max_devices),
        "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
        "cache-control": "no-store",
        "routing": "happ://routing/off",
    }

    # ── Per-device VLESS credential ───────────────────────────────────────────
    #
    # Use the device's own UUID in the subscription body so each Happ device
    # connects with a unique VPN credential. Revoking that device_uuid from
    # the Xray config terminates its VPN access independently of other devices.
    #
    # Fallback to user.uuid when x-hwid is absent (non-HWID Happ path).
    effective_uuid = happ_device.device_uuid if happ_device and happ_device.device_uuid else user.uuid
    config = build_xray_config(user_uuid=effective_uuid, settings=settings)
    body = json.dumps([config], ensure_ascii=False, separators=(",", ":"))

    if settings.happ_hide_server_settings:
        resp_headers["hide-settings"] = "1"

    return Response(
        content=body,
        headers=resp_headers,
        media_type="application/json; charset=utf-8",
    )
