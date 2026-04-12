from __future__ import annotations

from base64 import b64encode
from urllib.parse import quote

from fastapi import Request

from backend.config import Settings
from backend.url_utils import build_app_url


def build_vless_url(
    *, user_uuid: str, settings: Settings, server_description: str = ""
) -> str:
    fragment = "🇳🇱 Нидерланды"
    if server_description:
        encoded = b64encode(server_description.encode("utf-8")).decode("ascii")
        fragment = f"{fragment}?serverDescription={encoded}"
    return (
        f"vless://{user_uuid}@{settings.vpn_server}:{settings.vpn_port}"
        f"?type=tcp"
        f"&security=reality"
        f"&pbk={settings.vpn_reality_public_key}"
        f"&fp=chrome"
        f"&sni={settings.vpn_sni}"
        f"&sid={settings.vpn_reality_short_id}"
        f"&flow=xtls-rprx-vision"
        f"#{fragment}"
    )


def build_subscription_url(
    *,
    token: str,
    request: Request,
    settings: Settings,
) -> str:
    # /{token} is the canonical personal link — both browsers and Happ use it.
    return build_app_url(path=f"/{token}", request=request, settings=settings)


def build_happ_deep_link(subscription_url: str) -> str:
    encoded = quote(subscription_url, safe="")
    return f"happ://add/{encoded}"


def is_happ_request(request: Request) -> bool:
    """Return True if the request comes from a Happ subscription client.

    Detection uses any of the headers confirmed to be sent by Happ on
    subscription import and refresh:

      x-hwid          — device hardware ID sent by Happ
      x-device-os     — OS identifier sent by Happ
      x-device-model  — device model string sent by Happ
      user-agent      — Happ sets this to "Happ/<version>"

    OR logic: any single signal is sufficient. This handles cases where Happ
    omits one header (e.g. first import before HWID is assigned). Detection
    is used for server-side routing only; it does not affect response content.
    """
    headers = request.headers
    if headers.get("x-hwid"):
        return True
    if headers.get("x-device-os"):
        return True
    if headers.get("x-device-model"):
        return True
    if headers.get("user-agent", "").lower().startswith("happ/"):
        return True
    return False

