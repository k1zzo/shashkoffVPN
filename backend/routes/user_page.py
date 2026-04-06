from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from backend import happ_limited_links
from backend.config import get_settings
from backend.db import get_db
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

router = APIRouter(tags=["user-page"])
settings = get_settings()


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

    expires_at_label = user.expires_at.strftime(
        "%d.%m.%Y") if user.expires_at else "Never"

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
                "traffic_used": "0 GB",
                "traffic_total": "∞",
                "traffic_summary": "0 GB / ∞",
            },
            "devices": [
                {
                    "device_id": device.device_id,
                    "device_name": device.device_name,
                    "platform": device.platform,
                    "last_seen_at": (
                        device.last_seen_at.strftime("%Y-%m-%d %H:%M:%S")
                        if device.last_seen_at
                        else "Never"
                    ),
                }
                for device in device_rows
            ],
            "actions": {
                "subscription_url": subscription_url,
                "happ_deep_link": happ_deep_link,
            },
        },
    )
