from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_project_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def _normalize_sqlite_url(value: str) -> str:
    prefix = "sqlite:///"
    if value.startswith(prefix):
        return f"{prefix}{_resolve_project_path(value.removeprefix(prefix))}"
    return f"{prefix}{_resolve_project_path(value)}"


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _normalize_base_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"https://{raw}"


def _parse_trusted_hosts(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ("*",)

    hosts = tuple(part.strip() for part in value.split(",") if part.strip())
    return hosts or ("*",)


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return True
    placeholder_fragments = [
        "change_me",
        "replace_with",
        "example",
        "placeholder",
        "your_",
        "your-",
    ]
    return any(fragment in normalized for fragment in placeholder_fragments)


def _build_vpn_config_warnings(
    *,
    vpn_server: str,
    vpn_port: int,
    vpn_sni: str,
    vpn_reality_public_key: str,
    vpn_reality_short_id: str,
    vpn_transport: str,
) -> list[str]:
    warnings: list[str] = []

    if _looks_like_placeholder(vpn_server):
        warnings.append("VPN_SERVER looks like a placeholder value.")
    if vpn_port <= 0 or vpn_port > 65535:
        warnings.append("VPN_PORT is outside the valid TCP/UDP port range.")
    if _looks_like_placeholder(vpn_sni):
        warnings.append("VPN_SNI looks like a placeholder value.")
    if _looks_like_placeholder(vpn_reality_public_key):
        warnings.append("VPN_REALITY_PUBLIC_KEY looks like a placeholder value.")
    if _looks_like_placeholder(vpn_reality_short_id):
        warnings.append("VPN_REALITY_SHORT_ID looks like a placeholder value.")
    if vpn_transport.strip().lower() != "tcp":
        warnings.append("VPN_TRANSPORT is not tcp. The profile generator will use tcp.")

    return warnings


@dataclass(frozen=True)
class Settings:
    app_name: str
    environment: str
    database_url: str
    templates_dir: Path
    static_dir: Path
    app_base_url: str
    app_base_url_configured: bool
    app_trusted_hosts: tuple[str, ...]
    vpn_server: str
    vpn_port: int
    vpn_reality_public_key: str
    vpn_reality_short_id: str
    vpn_sni: str
    vpn_transport: str
    vpn_profile_name_prefix: str
    vpn_location_label: str
    vpn_config_warnings: tuple[str, ...]
    vpn_config_incomplete: bool
    # ── Happ Limited Links (optional) ────────────────────────────────────────
    # When happ_limited_links_enabled is True and provider_code + auth_key are
    # set, the user cabinet will request a Happ-side install_code and wrap the
    # subscription URL in ?InstallID=<code> for the "Добавить в подписку" action.
    # See backend/happ_limited_links.py for full documentation and assumptions.
    happ_limited_links_enabled: bool
    happ_provider_code: str
    happ_auth_key: str
    happ_api_url: str  # override base URL; defaults to https://happ-proxy.com
    # When True, send `hide-settings: 1` HTTP header and prepend `#hide-settings: 1`
    # to the subscription body so Happ hides the server settings UI.
    # Set HAPP_HIDE_SERVER_SETTINGS=false to disable (default: true).
    happ_hide_server_settings: bool
    # When non-empty, embed this text as a base64 `?serverDescription=` param in
    # the VLESS URL fragment so Happ displays it as the server description.
    # Set HAPP_SERVER_DESCRIPTION='' to disable. Default: 'SHASHKOFF VPN'.
    happ_server_description: str
    # ── Xray server-side clients config ──────────────────────────────────────
    # When set, the path where the active Xray clients JSON array is written
    # whenever the device set changes. The file contains only the `clients`
    # array for an Xray VLESS inbound — not a full Xray config.
    # Set XRAY_CLIENTS_CONFIG_PATH=/etc/xray/clients.json (or similar).
    # If unset, no file is written and per-device Xray revocation is incomplete.
    xray_clients_config_path: Path | None
    # When set, the app runs this command after each successful clients config
    # write so the running Xray process picks up the new client set. The command
    # is parsed via shlex.split and executed without shell=True.
    # Examples:
    #   XRAY_RELOAD_COMMAND=systemctl reload xray
    #   XRAY_RELOAD_COMMAND=docker kill --signal=SIGHUP xray
    #   XRAY_RELOAD_COMMAND=/opt/scripts/apply-xray-clients.sh
    # If unset and xray_reload_via_watcher is False, a WARNING is logged.
    # If unset and xray_reload_via_watcher is True, a low-noise INFO is logged
    # instead — the host-side systemd watcher is expected to apply changes.
    xray_reload_command: str | None
    # Seconds to wait for the reload command before giving up (default: 10).
    # Increase if your reload script is slow; decrease for faster failure detection.
    xray_reload_timeout: int
    # Set XRAY_RELOAD_VIA_WATCHER=true in Docker production deployments that use
    # the host-side systemd inotify watcher to apply Xray client changes.
    # When True and XRAY_RELOAD_COMMAND is unset, the missing-reload warning is
    # suppressed — because the watcher is the intended reload mechanism.
    # When False (default), the warning is kept to alert operators that changes
    # are not being applied automatically.
    xray_reload_via_watcher: bool
    # ── Xray stats API ───────────────────────────────────────────────────────────
    # When set, the gRPC address of the Xray StatsService.  The backend queries
    # this address when serving the user cabinet and Happ subscription to report
    # real total traffic usage instead of a hardcoded 0.
    #
    # Xray must be configured with stats enabled (see CLAUDE.md § Xray stats).
    # In Docker deployments the address must be reachable from inside the
    # container — typically the host's Docker bridge IP, e.g. 172.17.0.1:10085.
    # In bare-metal deployments use 127.0.0.1:10085 (or whatever port Xray
    # listens on for the API inbound).
    #
    # Example:
    #   XRAY_API_ADDR=127.0.0.1:10085      (non-Docker)
    #   XRAY_API_ADDR=172.17.0.1:10085     (Docker, Linux host)
    #
    # If unset, traffic stats are reported as "N/A" in the cabinet and the
    # subscription-userinfo upload/download fields remain 0 (honest: unknown).
    xray_api_addr: str | None
    # ── Temporary diagnostics ─────────────────────────────────────────────────
    # When debug_happ_sub_requests is True, /{token} logs full request
    # metadata (headers, query params, IP) to the "happ.sub.diag" logger.
    # Set DEBUG_HAPP_SUB_REQUESTS=true in .env to enable. Remove after analysis.
    debug_happ_sub_requests: bool

    @classmethod
    def from_env(cls) -> "Settings":
        default_db_path = os.getenv("DATABASE_PATH", "data/app.db")
        database_url = os.getenv("DATABASE_URL", _normalize_sqlite_url(default_db_path))

        if not database_url.startswith("sqlite:///"):
            raise ValueError("Only sqlite:/// URLs are supported in this MVP.")

        templates_dir = _resolve_project_path(os.getenv("TEMPLATES_DIR", "templates"))
        static_dir = _resolve_project_path(os.getenv("STATIC_DIR", "static"))
        app_base_url = _normalize_base_url(os.getenv("APP_BASE_URL", ""))
        app_trusted_hosts = _parse_trusted_hosts(os.getenv("APP_TRUSTED_HOSTS", "*"))
        vpn_server = os.getenv("VPN_SERVER", "your-vpn-host.example.com")
        vpn_port = _env_int("VPN_PORT", 443)
        vpn_sni = os.getenv("VPN_SNI", "your-sni.example.com")
        vpn_reality_public_key = os.getenv(
            "VPN_REALITY_PUBLIC_KEY",
            "CHANGE_ME_REALITY_PUBLIC_KEY",
        )
        vpn_reality_short_id = os.getenv(
            "VPN_REALITY_SHORT_ID",
            "CHANGE_ME_SHORT_ID",
        )
        vpn_transport = os.getenv("VPN_TRANSPORT", "tcp")
        vpn_profile_name_prefix = os.getenv("VPN_PROFILE_NAME_PREFIX", "SHASHKOFFVPN")
        vpn_location_label = os.getenv("VPN_LOCATION_LABEL", "🇳🇱 Нидерланды")

        vpn_config_warnings = _build_vpn_config_warnings(
            vpn_server=vpn_server,
            vpn_port=vpn_port,
            vpn_sni=vpn_sni,
            vpn_reality_public_key=vpn_reality_public_key,
            vpn_reality_short_id=vpn_reality_short_id,
            vpn_transport=vpn_transport,
        )

        happ_enabled_raw = os.getenv("HAPP_LIMITED_LINKS_ENABLED", "").strip().lower()
        happ_limited_links_enabled = happ_enabled_raw in ("1", "true", "yes")

        debug_raw = os.getenv("DEBUG_HAPP_SUB_REQUESTS", "").strip().lower()
        debug_happ_sub_requests = debug_raw in ("1", "true", "yes")

        xray_clients_path_raw = os.getenv("XRAY_CLIENTS_CONFIG_PATH", "").strip()
        xray_clients_config_path = (
            _resolve_project_path(xray_clients_path_raw) if xray_clients_path_raw else None
        )

        xray_reload_cmd_raw = os.getenv("XRAY_RELOAD_COMMAND", "").strip()
        xray_reload_command = xray_reload_cmd_raw if xray_reload_cmd_raw else None
        xray_reload_timeout = _env_int("XRAY_RELOAD_TIMEOUT_SECONDS", 10)

        watcher_raw = os.getenv("XRAY_RELOAD_VIA_WATCHER", "").strip().lower()
        xray_reload_via_watcher = watcher_raw in ("1", "true", "yes")

        xray_api_addr_raw = os.getenv("XRAY_API_ADDR", "").strip()
        xray_api_addr = xray_api_addr_raw if xray_api_addr_raw else None

        return cls(
            app_name=os.getenv("APP_NAME", "SHASHKOFFVPN"),
            environment=os.getenv("APP_ENV", "development"),
            database_url=database_url,
            templates_dir=templates_dir,
            static_dir=static_dir,
            app_base_url=app_base_url,
            app_base_url_configured=bool(app_base_url),
            app_trusted_hosts=app_trusted_hosts,
            vpn_server=vpn_server,
            vpn_port=vpn_port,
            vpn_reality_public_key=vpn_reality_public_key,
            vpn_reality_short_id=vpn_reality_short_id,
            vpn_sni=vpn_sni,
            vpn_transport=vpn_transport,
            vpn_profile_name_prefix=vpn_profile_name_prefix,
            vpn_location_label=vpn_location_label,
            vpn_config_warnings=tuple(vpn_config_warnings),
            vpn_config_incomplete=bool(vpn_config_warnings),
            happ_limited_links_enabled=happ_limited_links_enabled,
            happ_provider_code=os.getenv("HAPP_PROVIDER_CODE", ""),
            happ_auth_key=os.getenv("HAPP_AUTH_KEY", ""),
            happ_api_url=os.getenv("HAPP_API_URL", ""),
            happ_hide_server_settings=os.getenv(
                "HAPP_HIDE_SERVER_SETTINGS", "true"
            ).strip().lower() not in ("0", "false", "no"),
            happ_server_description=os.getenv("HAPP_SERVER_DESCRIPTION", "SHASHKOFF VPN"),
            debug_happ_sub_requests=debug_happ_sub_requests,
            xray_clients_config_path=xray_clients_config_path,
            xray_reload_command=xray_reload_command,
            xray_reload_timeout=xray_reload_timeout,
            xray_reload_via_watcher=xray_reload_via_watcher,
            xray_api_addr=xray_api_addr,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
