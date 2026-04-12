from datetime import timedelta, timezone
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from backend import happ_limited_links
from backend.config import get_settings
from backend.db import get_db
from backend.platform_utils import (
    device_type_icon,
    format_device_title,
    resolve_device_type,
)
from backend.queries import (
    count_active_devices,
    get_user_by_token,
    is_user_accessible,
    list_active_devices,
)
from backend.reserved import is_token_reserved
from backend.routes.profile import build_happ_subscription_response
from backend.subscription_utils import (
    build_happ_deep_link,
    build_subscription_url,
    is_happ_request,
)
from backend.xray_stats import combined_traffic, format_bytes, get_user_traffic_active

router = APIRouter(tags=["user-page"])
settings = get_settings()

# Moscow Standard Time — UTC+3, no DST (Russia abolished DST in 2014).
_MSK = timezone(timedelta(hours=3))

# Russian month names in genitive case (used after a day number: "12 апреля").
_RU_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _fmt_msk_datetime(dt) -> str:
    """Format a naive UTC datetime as Moscow local time: DD.MM.YYYY, HH:MM:SS."""
    if dt is None:
        return "Never"
    msk = dt.replace(tzinfo=timezone.utc).astimezone(_MSK)
    return msk.strftime("%d.%m.%Y, %H:%M:%S")


def _fmt_msk_date(dt) -> str:
    """Format a naive UTC datetime as a Russian-language Moscow local date.

    Output: '<day> <month_genitive>, <year>' — e.g. '11 мая, 2026'.
    """
    if dt is None:
        return "Never"
    msk = dt.replace(tzinfo=timezone.utc).astimezone(_MSK)
    return f"{msk.day} {_RU_MONTHS_GEN[msk.month - 1]}, {msk.year}"


@router.get("/{token}")
@router.get("/u/{token}", include_in_schema=False)
def render_user_page(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
) -> Response:
    # Guard: reserved prefixes should never match a real user, but if the
    # catch-all somehow receives one (e.g. "health"), bail early so we don't
    # query for a nonsensical token.
    if is_token_reserved(token):
        return request.app.state.templates.TemplateResponse(
            request,
            "error.html",
            {"request": request, "message": "Profile not found"},
            status_code=404,
        )

    user = get_user_by_token(db, token)

    if user is None:
        return request.app.state.templates.TemplateResponse(
            request,
            "error.html",
            {"request": request, "message": "Profile not found"},
            status_code=404,
        )

    # /{token} is the single universal personal link.
    # Happ subscription clients are identified by their characteristic headers
    # (x-hwid, x-device-os, x-device-model, or user-agent: Happ/…) and served
    # the subscription response. Normal browser requests fall through to the
    # HTML cabinet below.
    if is_happ_request(request):
        return build_happ_subscription_response(user, request, token=token, db=db)

    accessible = is_user_accessible(user)
    if not accessible:
        if not user.is_active:
            inactive_reason = "inactive"
        else:
            inactive_reason = "expired"
    else:
        inactive_reason = None

    active_devices = count_active_devices(db, user.id)
    device_rows = list_active_devices(db, user.id)

    subscription_url = build_subscription_url(
        token=user.public_token,
        request=request,
        settings=settings,
    )

    # ── Happ Limited Links (optional) ────────────────────────────────────────
    # When enabled, request a Happ-side install_code and build a limited
    # subscription URL (/{token}?InstallID=...).  The "Добавить в подписку"
    # deep link wraps this limited URL so Happ enforces the install cap
    # server-side on happ-proxy.com.
    #
    # The copy button always uses the raw subscription_url — it must stay
    # stable across page loads and be importable without Happ infrastructure.
    #
    # If the feature is disabled, or the API call fails, or the user is
    # inaccessible, the plain subscription URL / deep link is used unchanged.
    #
    # NOTE: a new install_code is requested on every page render.  Whether old
    # codes accumulate or expire on happ-proxy.com is UNKNOWN.  Add per-user
    # caching once API behaviour is confirmed.
    if accessible and happ_limited_links.is_enabled(settings):
        install_code = happ_limited_links.get_limited_install_code(
            subscription_url=subscription_url,
            max_devices=user.max_devices,
            settings=settings,
        )
        if install_code:
            limited_url = happ_limited_links.build_limited_subscription_url(
                subscription_url=subscription_url,
                install_code=install_code,
            )
            happ_deep_link = build_happ_deep_link(limited_url)
        else:
            happ_deep_link = build_happ_deep_link(subscription_url)
    else:
        happ_deep_link = build_happ_deep_link(subscription_url)

    expires_at_label = _fmt_msk_date(user.expires_at)

    # ── Traffic stats: stored historical + live active devices ────────────────
    # active_labels is the set of device_id[:24] for all currently active
    # devices.  get_user_traffic_active() queries Xray and filters to those
    # labels only, so deleted devices' counters are never double-counted with
    # the stored historical totals.
    #
    # combined_traffic() always returns a UserTrafficStats (never None):
    #   - When Xray is available: stored + live.
    #   - When Xray is down or not configured: stored only.
    # Both cases produce "0 B / ∞" for a brand new user (stored=0, live=0/None).
    _active_labels = frozenset(d.device_id[:24] for d in device_rows)
    _live = None
    if accessible and settings.xray_api_addr:
        _live = get_user_traffic_active(
            username=user.username,
            active_labels=_active_labels,
            xray_api_addr=settings.xray_api_addr,
        )
    _total = combined_traffic(
        stored_up=user.traffic_up_bytes or 0,
        stored_down=user.traffic_down_bytes or 0,
        live=_live,
    )
    traffic_used = format_bytes(_total.total_bytes)
    traffic_summary = f"{traffic_used} / ∞"
    # ─────────────────────────────────────────────────────────────────────────

    return request.app.state.templates.TemplateResponse(
        request,
        "user_page.html",
        {
            "request": request,
            "profile": {
                "username": user.username,
                "public_token": user.public_token,
                "accessible": accessible,
                "inactive_reason": inactive_reason,
                "status": "active" if accessible else "inactive",
                "expires_at": expires_at_label,
                "devices_used": active_devices or 0,
                "max_devices": user.max_devices,
                "devices_summary": f"{active_devices or 0} / {user.max_devices}",
                "traffic_used": traffic_used,
                "traffic_total": "∞",
                "traffic_summary": traffic_summary,
            },
            "devices": [
                {
                    "device_id": device.device_id,
                    "device_name": device.device_name,
                    "platform": device.platform,
                    "last_seen_at": _fmt_msk_datetime(device.last_seen_at),
                    "device_title": format_device_title(device.platform, device.device_name),
                    "icon": device_type_icon(resolve_device_type(device)),
                }
                for device in device_rows
            ],
            "actions": {
                "subscription_url": subscription_url,
                "happ_deep_link": happ_deep_link,
            },
        },
    )
