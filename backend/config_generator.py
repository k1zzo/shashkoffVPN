from __future__ import annotations

from typing import Any

from backend.config import Settings

LOCAL_IP_CIDRS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
]

YOUTUBE_DOMAIN_SUFFIXES = [
    "youtube.com",
    "youtu.be",
    "googlevideo.com",
    "ytimg.com",
    "youtubei.googleapis.com",
]

TELEGRAM_DOMAIN_SUFFIXES = [
    "telegram.org",
    "t.me",
    "telegra.ph",
    "telegram.me",
    "tdesktop.com",
]


def _profile_name_for_user(settings: Settings, username: str) -> str:
    prefix = settings.vpn_profile_name_prefix.strip() or "SHASHKOFFVPN"
    return f"{prefix} {username}"


def _profile_warnings(settings: Settings) -> list[str]:
    return list(settings.vpn_config_warnings)


def build_vpn_profile(user_uuid: str, username: str, settings: Settings) -> dict[str, Any]:
    transport = "tcp"
    warnings = _profile_warnings(settings)

    return {
        "profile_type": "shashkoffvpn-happ-mvp",
        "profile_version": 1,
        "meta": {
            "profile_name": _profile_name_for_user(settings=settings, username=username),
            "server": settings.vpn_server,
            "sni": settings.vpn_sni,
            "transport": transport,
            "warnings": warnings,
        },
        "dns": {
            "strategy": "prefer_ipv4",
            "servers": [
                {
                    "tag": "dns-remote",
                    "address": "https://1.1.1.1/dns-query",
                    "detour": "proxy",
                },
                {
                    "tag": "dns-local",
                    "address": "local",
                    "detour": "direct",
                },
            ],
        },
        "outbounds": [
            {
                "tag": "proxy",
                "type": "vless",
                "server": settings.vpn_server,
                "server_port": settings.vpn_port,
                "uuid": user_uuid,
                "packet_encoding": "xudp",
                "tls": {
                    "enabled": True,
                    "server_name": settings.vpn_sni,
                    "utls": {
                        "enabled": True,
                        "fingerprint": "chrome",
                    },
                    "reality": {
                        "enabled": True,
                        "public_key": settings.vpn_reality_public_key,
                        "short_id": settings.vpn_reality_short_id,
                    },
                },
                "transport": {
                    "type": transport,
                },
            },
            {
                "tag": "direct",
                "type": "direct",
            },
            {
                "tag": "block",
                "type": "block",
            },
        ],
        "route": {
            "final": "proxy",
            "rules": [
                {
                    "name": "private-local-ip",
                    "ip_cidr": LOCAL_IP_CIDRS,
                    "outbound": "direct",
                },
                {
                    "name": "private-local-domain",
                    "domain_suffix": ["localhost", "local", "lan"],
                    "outbound": "direct",
                },
                {
                    "name": "ru-traffic",
                    "geoip": ["ru"],
                    "outbound": "direct",
                },
                {
                    "name": "youtube",
                    "domain_suffix": YOUTUBE_DOMAIN_SUFFIXES,
                    "outbound": "proxy",
                },
                {
                    "name": "telegram",
                    "domain_suffix": TELEGRAM_DOMAIN_SUFFIXES,
                    "outbound": "proxy",
                },
                {
                    "name": "default",
                    "outbound": "proxy",
                },
            ],
        },
    }
