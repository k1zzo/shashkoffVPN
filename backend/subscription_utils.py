from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import Request

from backend.config import Settings
from backend.url_utils import build_app_url


def build_vless_url(*, user_uuid: str, username: str, settings: Settings) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", username).strip("-") or "user"
    return (
        f"vless://{user_uuid}@{settings.vpn_server}:{settings.vpn_port}"
        f"?type=tcp"
        f"&security=reality"
        f"&pbk={settings.vpn_reality_public_key}"
        f"&fp=chrome"
        f"&sni={settings.vpn_sni}"
        f"&sid={settings.vpn_reality_short_id}"
        f"&flow=xtls-rprx-vision"
        f"#SHASHKOFFVPN-{safe_name}"
    )


def build_subscription_url(
    *,
    token: str,
    request: Request,
    settings: Settings,
) -> str:
    return build_app_url(path=f"/sub/{token}", request=request, settings=settings)


def build_happ_deep_link(subscription_url: str) -> str:
    encoded = quote(subscription_url, safe="")
    return f"happ://add/{encoded}"

