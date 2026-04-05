from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.db import get_db
from backend.queries import (
    count_active_devices,
    get_user_by_token,
    is_user_accessible,
    list_active_devices,
)
from backend.reserved import is_token_reserved
from backend.subscription_utils import (
    build_happ_deep_link,
    build_subscription_url,
)

router = APIRouter(tags=["user-page"])
settings = get_settings()


@router.get("/{token}", response_class=HTMLResponse)
@router.get("/u/{token}", response_class=HTMLResponse, include_in_schema=False)
def render_user_page(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
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
