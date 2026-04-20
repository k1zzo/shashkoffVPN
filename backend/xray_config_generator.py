"""Build a full Xray JSON config for Happ's custom tunnel feature.

The config is delivered as a JSON array ([config]) via the Happ subscription
endpoint. Happ parses the array and shows each element as a profile labeled
"VLESS | TCP | Reality | JSON".
"""

from __future__ import annotations

from typing import Any

from backend.config import Settings

# ── Geosite / geoip groups ──────────────────────────────────────────────────

BLOCK_GEOSITES = ["geosite:win-spy", "geosite:category-ads"]
BITTORRENT_PROTOCOLS = ["bittorrent"]
PROXY_GEOSITES = ["geosite:github", "geosite:youtube", "geosite:telegram"]
DIRECT_GEOSITES = [
    "geosite:private",
    "geosite:category-ru",
    "geosite:microsoft",
    "geosite:apple",
    "geosite:google-play",
    "geosite:epicgames",
    "geosite:riot",
    "geosite:steam",
]
DIRECT_GEOIPS = ["geoip:private", "geoip:ru"]

DNS_PROXY_GEOSITES = ["geosite:github", "geosite:youtube", "geosite:telegram"]
DNS_DIRECT_GEOSITES = ["geosite:category-ru", "geosite:private"]
DNS_DIRECT_EXPECTED_GEOIPS = ["geoip:ru"]


def build_xray_config(
    user_uuid: str,
    settings: Settings,
) -> dict[str, Any]:
    """Build a full Xray JSON config for Happ's custom tunnel feature."""
    config: dict[str, Any] = {
        "dns": {
            "queryStrategy": "UseIPv4",
            "servers": [
                "https://1.1.1.1/dns-query",
                {
                    "address": "https://1.1.1.1/dns-query",
                    "domains": list(DNS_PROXY_GEOSITES),
                },
                {
                    "address": "76.76.10.41",
                    "domains": list(DNS_DIRECT_GEOSITES),
                    "expectedIPs": list(DNS_DIRECT_EXPECTED_GEOIPS),
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
                "settings": {"udp": True, "auth": "noauth"},
                "sniffing": {
                    "enabled": True,
                    "routeOnly": False,
                    "destOverride": ["http", "tls", "quic"],
                },
            },
            {
                "tag": "http",
                "port": 10809,
                "listen": "127.0.0.1",
                "protocol": "http",
                "settings": {"allowTransparent": False},
                "sniffing": {
                    "enabled": True,
                    "routeOnly": False,
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
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
            {
                "tag": "fragment",
                "protocol": "freedom",
                "settings": {
                    "fragment": {
                        "interval": "10-20",
                        "length": "50-100",
                        "maxSplit": "100-200",
                        "packets": "1-3",
                    }
                },
                "streamSettings": {
                    "network": "raw",
                    "security": "",
                    "sockopt": {"mark": 255, "TcpNoDelay": True},
                },
            },
        ],
        "routing": {
            "domainMatcher": "hybrid",
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {
                    "type": "field",
                    "domain": list(BLOCK_GEOSITES),
                    "outboundTag": "block",
                },
                {
                    "type": "field",
                    "protocol": list(BITTORRENT_PROTOCOLS),
                    "outboundTag": "block",
                },
                {
                    "type": "field",
                    "domain": list(PROXY_GEOSITES),
                    "outboundTag": "proxy",
                },
                {
                    "type": "field",
                    "domain": list(DIRECT_GEOSITES),
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
