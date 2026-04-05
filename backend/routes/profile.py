from datetime import datetime
from base64 import b64encode
import json
import re

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.config_generator import build_vpn_profile
from backend.db import get_db
from backend.models import Device, User
from backend.queries import (
    count_active_devices,
    get_device,
    get_user_by_token,
    is_user_accessible,
)
from backend.subscription_utils import build_subscription_url, build_vless_url
from backend.url_utils import build_app_url

router = APIRouter(tags=["profile"])
settings = get_settings()

# Stable far-future timestamp used when a user has no expires_at (unlimited plan).
# 2099-12-31 00:00:00 UTC — avoids a sliding "now + 30 days" that changes on every request.
_UNLIMITED_EXPIRE_TS: int = 4102444800

# Happ profile-update-interval is in hours.
_HAPP_UPDATE_INTERVAL_HOURS: str = "1"


def _error_response(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _build_real_subscription_body(user: User) -> str:
    return build_vless_url(user_uuid=user.uuid, username=user.username, settings=settings)


def _build_happ_routing_payload() -> dict:
    return {
        "Name": "RU Direct",
        "GlobalProxy": "true",
        "RemoteDNSType": "DoH",
        "RemoteDNSDomain": "https://cloudflare-dns.com/dns-query",
        "RemoteDNSIP": "1.1.1.1",
        "DomesticDNSType": "DoH",
        "DomesticDNSDomain": "https://dns.google/dns-query",
        "DomesticDNSIP": "8.8.8.8",
        "Geoipurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
        "DnsHosts": {
            "cloudflare-dns.com": "1.1.1.1",
            "dns.google": "8.8.8.8",
        },
        "DirectSites": [
            # RU consumer/utility
            "regexp:(^|\\.)2ip\\.ru$",
            "regexp:(^|\\.)yandex\\.(ru|by|kz|uz|com)$",
            "regexp:(^|\\.)ya\\.ru$",
            "regexp:(^|\\.)vk\\.com$",
            "regexp:(^|\\.)mail\\.ru$",
            "regexp:(^|\\.)gosuslugi\\.ru$",
            "regexp:(^|\\.)dzen\\.ru$",
            "regexp:(^|\\.)avito\\.ru$",
            "regexp:(^|\\.)ozon\\.ru$",
            "regexp:(^|\\.)wildberries\\.ru$",
            # RU banking / payments
            "regexp:(^|\\.)sberbank\\.ru$",
            "regexp:(^|\\.)sber\\.ru$",
            "regexp:(^|\\.)sberpay\\.ru$",
            "regexp:(^|\\.)tinkoff\\.ru$",
            "regexp:(^|\\.)alfabank\\.ru$",
            "regexp:(^|\\.)vtb\\.ru$",
        ],
        "DirectIp": [
            "geoip:ru",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "224.0.0.0/4",
            "255.255.255.255",
            "::1/128",
            "fc00::/7",
            "fe80::/10",
        ],
        "ProxySites": [
            # Google / YouTube
            "regexp:(^|\\.)google\\.com$",
            "regexp:(^|\\.)googleapis\\.com$",
            "regexp:(^|\\.)gstatic\\.com$",
            "regexp:(^|\\.)youtube\\.com$",
            "regexp:(^|\\.)youtu\\.be$",
            "regexp:(^|\\.)googlevideo\\.com$",
            "regexp:(^|\\.)ytimg\\.com$",
            "regexp:(^|\\.)youtubei\\.googleapis\\.com$",
            # Telegram
            "regexp:(^|\\.)telegram\\.org$",
            "regexp:(^|\\.)t\\.me$",
            "regexp:(^|\\.)telegra\\.ph$",
            "regexp:(^|\\.)telegram\\.me$",
        ],
        "ProxyIp": [],
        "BlockSites": [],
        "BlockIp": [],
        "DomainStrategy": "IPIfNonMatch",
        "FakeDNS": "false",
    }


def _build_happ_routing_link() -> str:
    payload = _build_happ_routing_payload()
    encoded = b64encode(
        json.dumps(payload, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    ).decode("utf-8")
    return f"happ://routing/onadd/{encoded}"


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

    user = get_user_by_token(db, clean_token)

    if user is None:
        return None, None, _error_response(404, "Profile not found")

    if not is_user_accessible(user):
        return None, None, _error_response(403, "User is inactive")

    existing_device = get_device(db, user.id, clean_device_id)
    now = datetime.utcnow()
    known_device = existing_device is not None

    if existing_device is None:
        if count_active_devices(db, user.id) >= user.max_devices:
            return None, None, _error_response(403, "device limit reached")

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

    active_devices = count_active_devices(db, user.id)

    profile = build_vpn_profile(
        user_uuid=user.uuid,
        username=user.username,
        settings=settings,
    )
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
) -> Response:
    url = build_vless_url(
        user_uuid=profile["outbounds"][0]["uuid"],
        username=username,
        settings=settings,
    )

    if mode in {"raw", "json"}:
        return Response(content=url, media_type="text/plain")

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


# NOTE: /api/profile enforces device limits (requires device_id, auto-registers
# devices, rejects when max_devices is reached). This is the per-device config
# endpoint used by direct Xray clients. In the subscription-only flow, the Happ
# client uses /sub/{token} instead, which intentionally does NOT enforce device
# limits — Happ manages its own HWID-based device tracking via subscription
# headers (x-hwid-limit, x-hwid-active). The two endpoints serve different
# distribution models; this is by design, not an oversight.

@router.get("/api/profile/{token}")
def get_vpn_profile(
    token: str,
    device_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    profile, _, error = _build_profile_payload(
        token=token, device_id=device_id, db=db)
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

    profile, context, error = _build_profile_payload(
        token=token, device_id=device_id, db=db)
    if error is not None:
        return error

    # All modes (json, raw, download) return the VLESS URL in various forms.
    # Default (no mode) also returns raw — the legacy open_profile.html template
    # has been removed; this route is kept for internal/manual workflows only.
    clean_mode = (mode or "raw").strip().lower()
    return _json_or_download_response(
        profile=profile,
        mode=clean_mode,
        username=context["username"],
    )


# NOTE: /sub/{token} is the Happ-compatible subscription endpoint. It returns a
# plain VLESS URL and does NOT enforce server-side device limits. Device tracking
# is delegated to the Happ client via subscription headers (x-hwid-limit,
# x-hwid-active, subscription-always-hwid-enable). This is intentional — see the
# comment above /api/profile for the full rationale.

@router.get("/sub/{token}")
def happ_subscription(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
) -> Response:
    clean_token = token.strip()
    if not clean_token:
        return Response("not found", status_code=404)

    user = get_user_by_token(db, clean_token)
    if user is None:
        return Response("not found", status_code=404)

    if not is_user_accessible(user):
        expired = user.expires_at is not None and user.expires_at <= datetime.utcnow()
        blocked_label = "СРОК ДЕЙСТВИЯ ИСТЕК" if expired else "ПОДПИСКА ОТКЛЮЧЕНА"
        profile_title = b64encode(blocked_label.encode("utf-8")).decode("utf-8")
        # Use actual expiry timestamp when present; fall back to 0 (signals
        # expired/invalid to Happ clients that parse subscription-userinfo).
        expire_ts = int(user.expires_at.timestamp()) if user.expires_at else 0
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

    profile_title = b64encode(
        f"SHASHKOFFVPN {user.username}".encode("utf-8")).decode("utf-8")
    announce = b64encode(
        "Subscription | SHASHKOFFVPN".encode("utf-8")).decode("utf-8")
    expire_ts = int(user.expires_at.timestamp()) if user.expires_at else _UNLIMITED_EXPIRE_TS

    sub_url = build_subscription_url(
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
    # These four headers together request the "local HWID enforcement" model:
    # Happ tracks hardware IDs inside the app, refuses the (N+1)th unique
    # device, and never calls back to our server for validation.
    #
    #   x-hwid-active: true
    #       Enables HWID tracking for this subscription.
    #
    #   x-hwid-limit: <N>  (user.max_devices, numeric string)
    #       The maximum number of distinct HWIDs Happ should allow.
    #       Must be a number — the previous value "true" was likely ignored.
    #
    #   x-hwid-not-supported: true
    #       INFERENCE (not confirmed from Happ docs): means "this provider has
    #       no server-side HWID validation API". Happ should enforce the cap
    #       locally without making a server callback. Do NOT interpret this as
    #       "disable HWID" — if that were the intent the other HWID headers
    #       would be pointless. This interpretation must be verified with a
    #       real 6-device test before treating enforcement as guaranteed.
    #
    #   subscription-always-hwid-enable: 1
    #       Forces Happ to use HWID tracking even when the provider has no
    #       server HWID API (i.e., when x-hwid-not-supported is true).
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
    #   - Device limit enforcement for non-Happ clients (/api/profile,
    #     /api/device/register)
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
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Content-Disposition": f'attachment; filename="user_{user.id}_{user.public_token}"',
        "fallback-url": fallback_url,
        "hide-settings": "1",
        "mux-enable": "0",
        "notification-subs-expire": "1",
        "profile-title": f"base64:{profile_title}",
        "profile-update-interval": _HAPP_UPDATE_INTERVAL_HOURS,
        "profile-web-page-url": sub_url,
        "providerid": "6QlMYR5q",
        "subscription-always-hwid-enable": "1",
        "subscription-userinfo": f"upload=0; download=0; total=0; expire={expire_ts}",
        "subscriptions-collapse": "0",
        "support-url": fallback_url,
        "announce": f"base64:{announce}",
        "x-hwid-active": "true",
        "x-hwid-limit": str(user.max_devices),
        "x-hwid-not-supported": "true",
        "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
        "cache-control": "no-store",
        "routing": _build_happ_routing_link(),
    }

    body = _build_real_subscription_body(user)

    return Response(
        content=body,
        headers=headers,
        media_type="text/plain",
    )
