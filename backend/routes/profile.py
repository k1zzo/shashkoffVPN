from datetime import datetime
import re

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.config_generator import build_vpn_profile
from backend.db import get_db
from backend.models import Device, User
from backend.url_utils import build_app_url

router = APIRouter(tags=["profile"])
settings = get_settings()


def _error_response(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _count_active_devices(db: Session, user_id: int) -> int:
    count = db.scalar(
        select(func.count(Device.id)).where(
            Device.user_id == user_id,
            Device.is_active.is_(True),
        )
    )
    return count or 0


def _build_profile_payload(
    token: str,
    device_id: str | None,
    db: Session,
) -> tuple[dict | None, dict | None, JSONResponse | None]:
    clean_token = token.strip()
    clean_device_id = device_id.strip() if device_id is not None else ""

    if not clean_token:
        return None, None, _error_response(400, "token is required")

    if not clean_device_id:
        return None, None, _error_response(400, "device_id is required")

    user = db.scalar(select(User).where(User.public_token == clean_token))

    if user is None:
        return None, None, _error_response(404, "Profile not found")

    if not user.is_active:
        return None, None, _error_response(403, "User is inactive")

    existing_device = db.scalar(
        select(Device).where(
            Device.user_id == user.id,
            Device.device_id == clean_device_id,
        )
    )
    now = datetime.utcnow()
    known_device = existing_device is not None

    if existing_device is None:
        if _count_active_devices(db=db, user_id=user.id) >= user.max_devices:
            return None, None, _error_response(403, "device limit reached")

        # Auto-register new device in normal profile flow.
        db.add(
            Device(
                user_id=user.id,
                device_id=clean_device_id,
                device_name=f"Web Device {clean_device_id[-6:]}",
                platform="web",
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            )
        )
        db.commit()
    else:
        existing_device.last_seen_at = now
        existing_device.is_active = True
        db.commit()

    active_devices = _count_active_devices(db=db, user_id=user.id)

    profile = build_vpn_profile(user_uuid=user.uuid, username=user.username, settings=settings)
    profile_meta = profile.setdefault("meta", {})
    profile_meta.update({
        "username": user.username,
        "public_token": user.public_token,
        "expires_at": user.expires_at.isoformat() if user.expires_at else None,
        "device_id": clean_device_id,
        "known_device": known_device,
        "config_incomplete": settings.vpn_config_incomplete,
    })
    context = {
        "username": user.username,
        "public_token": user.public_token,
        "device_id": clean_device_id,
        "devices_used": active_devices,
        "max_devices": user.max_devices,
        "known_device": known_device,
        "app_base_url_configured": settings.app_base_url_configured,
    }

    return profile, context, None


def _json_or_download_response(
    profile: dict,
    mode: str,
    username: str,
    token: str,
    device_id: str,
) -> JSONResponse:
    clean_mode = mode.strip().lower()

    if clean_mode in {"json", "raw"}:
        return JSONResponse(content=profile)

    if clean_mode == "download":
        safe_username = re.sub(r"[^a-zA-Z0-9_-]", "-", username).strip("-").lower() or "user"
        safe_token = re.sub(r"[^a-zA-Z0-9_-]", "-", token).strip("-").lower() or "token"
        filename = f"{safe_username}-{safe_token}-profile.json"
        profile_title = profile.get("meta", {}).get("profile_name", "SHASHKOFFVPN profile")
        return JSONResponse(
            content=profile,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Profile-Title": str(profile_title),
                "Cache-Control": "no-store",
            },
        )

    return JSONResponse(content=profile)


@router.get("/api/profile/{token}")
def get_vpn_profile(
    token: str,
    device_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    profile, _, error = _build_profile_payload(token=token, device_id=device_id, db=db)
    if error is not None:
        return error
    return JSONResponse(content=profile)


@router.get("/open/{token}")
def open_profile(
    request: Request,
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

    profile, context, error = _build_profile_payload(token=token, device_id=device_id, db=db)
    if error is not None:
        return error

    clean_mode = (mode or "").strip().lower()
    if clean_mode in {"json", "raw", "download"}:
        return _json_or_download_response(
            profile=profile,
            mode=clean_mode,
            username=context["username"],
            token=context["public_token"],
            device_id=context["device_id"],
        )

    return request.app.state.templates.TemplateResponse(
        "open_profile.html",
        {
            "request": request,
            "context": context,
            "open_download_url": build_app_url(
                path=(
                    f"/open/{context['public_token']}?device_id={context['device_id']}&mode=download"
                ),
                request=request,
                settings=settings,
            ),
            "open_raw_url": build_app_url(
                path=f"/open/{context['public_token']}?device_id={context['device_id']}&mode=raw",
                request=request,
                settings=settings,
            ),
        },
    )
