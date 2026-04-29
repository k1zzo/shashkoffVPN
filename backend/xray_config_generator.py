"""Build a full Xray JSON config for Happ's custom tunnel feature.

The config is delivered as a JSON array ([config]) via the Happ subscription
endpoint. Happ parses the array and shows each element as a profile labeled
"VLESS | TCP | Reality | JSON".
"""

from __future__ import annotations

from typing import Any

from backend.config import Settings

# ── Geosite / geoip groups ──────────────────────────────────────────────────

BLOCK_GEOSITES = ["geosite:win-spy", "geosite:category-ads-all"]
BITTORRENT_PROTOCOLS = ["bittorrent"]
PROXY_GEOSITES = [
    "geosite:github",
    "geosite:youtube",
    "geosite:telegram",
    "geosite:google",
    "geosite:netflix",
    "geosite:openai",
    "geosite:meta",
]
DIRECT_GEOSITES = [
    "geosite:category-ru",
    "geosite:apple",
    "geosite:microsoft",
    "geosite:google-play",
    "geosite:epicgames",
    "geosite:riot",
    "geosite:steam",
]
DIRECT_GEOIPS = ["geoip:private", "geoip:ru"]

DNS_PROXY_GEOSITES = [
    "geosite:github",
    "geosite:youtube",
    "geosite:telegram",
    "geosite:google",
    "geosite:netflix",
    "geosite:openai",
    "geosite:meta",
]
DNS_DIRECT_GEOSITES = ["geosite:category-ru", "geosite:private"]

GOV_DOMAINS = ["domain:nalog.ru", "domain:gosuslugi.ru"]

DNS_HOSTS = {
    "domain:googleapis.cn": "googleapis.com",
    "lkfl2.nalog.ru": "213.24.64.175",
    "lknpd.nalog.ru": "213.24.64.181",
}


def build_xray_config(
    user_uuid: str,
    settings: Settings,
) -> dict[str, Any]:
    """Build a full Xray JSON config for Happ's custom tunnel feature."""
    config: dict[str, Any] = {
        "dns": {
            "tag": "dns-in",
            "hosts": dict(DNS_HOSTS),
            "queryStrategy": "UseIPv4",
            "servers": [
                "https://1.1.1.1/dns-query",
                {
                    "address": "https://1.1.1.1/dns-query",
                    "domains": list(DNS_PROXY_GEOSITES),
                },
                {
                    "address": "https://77.88.8.8/dns-query",
                    "domains": list(DNS_DIRECT_GEOSITES) + list(GOV_DOMAINS),
                    "skipFallback": True,
                },
            ],
        },
        "inbounds": [
            {
                "tag": "socks",
                "port": 10808,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True, "auth": "noauth", "userLevel": 8},
                "sniffing": {
                    "enabled": True,
                    "routeOnly": True,
                    "destOverride": ["http", "tls", "quic"],
                },
            },
            {
                "tag": "http",
                "port": 10809,
                "listen": "127.0.0.1",
                "protocol": "http",
                "settings": {"allowTransparent": False, "userLevel": 8},
                "sniffing": {
                    "enabled": True,
                    "routeOnly": True,
                    "destOverride": ["http", "tls", "quic"],
                },
            },
        ],
        "outbounds": [
            {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": settings.vpn_server,
                            "port": settings.vpn_port,
                            "users": [
                                {
                                    "id": user_uuid,
                                    "encryption": "none",
                                    "flow": "xtls-rprx-vision",
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "fingerprint": "chrome",
                        "publicKey": settings.vpn_reality_public_key,
                        "serverName": settings.vpn_sni,
                        "shortId": settings.vpn_reality_short_id,
                        "spiderX": "/",
                    },
                    "tcpSettings": {},
                },
            },
            {
                "tag": "direct",
                "protocol": "freedom",
                "settings": {"domainStrategy": "UseIPv4"},
            },
            {
                "tag": "block",
                "protocol": "blackhole",
                "settings": {"response": {"type": "http"}},
            },
            {"tag": "dns-out", "protocol": "dns"},
        ],
        "routing": {
            "domainMatcher": "hybrid",
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["dns-in"],
                    "outboundTag": "dns-out",
                },
                {
                    "type": "field",
                    "protocol": list(BITTORRENT_PROTOCOLS),
                    "outboundTag": "block",
                },
                {
                    "type": "field",
                    "domain": list(BLOCK_GEOSITES),
                    "outboundTag": "block",
                },
                {
                    "type": "field",
                    "domain": list(PROXY_GEOSITES),
                    "outboundTag": "proxy",
                },
                {
                    "type": "field",
                    "domain": list(DIRECT_GEOSITES) + list(GOV_DOMAINS),
                    "outboundTag": "direct",
                },
                {
                    "type": "field",
                    "ip": list(DIRECT_GEOIPS),
                    "outboundTag": "direct",
                },
                {
                    "type": "field",
                    "network": "tcp,udp",
                    "outboundTag": "proxy",
                },
            ],
        },
        "remarks": settings.vpn_location_label,
    }
    if settings.happ_server_description.strip():
        config["meta"] = {"serverDescription": settings.happ_server_description}
    return config
