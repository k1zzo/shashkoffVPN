from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.db import get_db
from backend.models import Device, User
from backend.url_utils import build_app_url

router = APIRouter(tags=["user-page"])
settings = get_settings()


@router.get("/u/{token}", response_class=HTMLResponse)
def render_user_page(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = db.scalar(select(User).where(User.public_token == token))

    if user is None:
        return request.app.state.templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "message": "Profile not found",
            },
            status_code=404,
        )

    active_devices = db.scalar(
        select(func.count(Device.id)).where(
            Device.user_id == user.id,
            Device.is_active.is_(True),
        )
    )
    device_rows = db.scalars(
        select(Device)
        .where(Device.user_id == user.id)
        .order_by(Device.last_seen_at.desc())
    ).all()

    expires_at_label = user.expires_at.strftime("%Y-%m-%d") if user.expires_at else "Never"

    return request.app.state.templates.TemplateResponse(
        "user_page.html",
        {
            "request": request,
            "profile": {
                "username": user.username,
                "public_token": user.public_token,
                "status": "active" if user.is_active else "inactive",
                "expires_at": expires_at_label,
                "devices_used": active_devices or 0,
                "max_devices": user.max_devices,
            },
            "devices": [
                {
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
            "server_status": {
                "vpn_server": settings.vpn_server,
                "vpn_sni": settings.vpn_sni,
                "vpn_transport": settings.vpn_transport,
                "config_incomplete": settings.vpn_config_incomplete,
                "config_warnings": list(settings.vpn_config_warnings),
                "app_base_url": settings.app_base_url or "not set",
                "app_base_url_configured": settings.app_base_url_configured,
                "max_devices": user.max_devices,
            },
            "open_base_url": build_app_url(
                path=f"/open/{user.public_token}",
                request=request,
                settings=settings,
            ),
        },
    )
