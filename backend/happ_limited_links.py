"""Happ Limited Links integration (optional, experimental).

PURPOSE
=======
Happ supports "Limited Links" — a mechanism where the provider registers a
subscription URL with Happ-proxy infrastructure, receives an install_code, and
appends it to the subscription URL as ?InstallID=<code>.  When Happ imports
that URL it contacts happ-proxy.com and enforces the device cap server-side,
independent of our SQLite device model.

This module is an OPTIONAL layer.  When HAPP_LIMITED_LINKS_ENABLED is not set
(or is false/0) the entire module is a no-op: callers fall back to the plain
subscription URL and nothing changes.

WHAT THIS SOLVES
================
- Gives Happ a server-enforced install cap (if the Happ-proxy infrastructure
  works as documented).
- Provides a real test of whether Happ blocks device N+1 at import time.

WHAT THIS DOES NOT SOLVE
=========================
- Our SQLite device list is still not synchronised with Happ imports.
- Removing a device in the cabinet still does NOT free a Happ-proxy slot.
- last_seen_at in the cabinet still reflects API registration only.
- Whether Happ-proxy actually enforces the cap is UNVERIFIED until a real
  2-device test (limit=1) is run.

ASSUMPTIONS (not confirmed from official Happ documentation)
============================================================
- API endpoint:  POST {HAPP_API_URL}/api/install/create
  ASSUMPTION — replace with the confirmed endpoint if/when documented.

- Request body shape:
    {"provider_code": "...", "auth_key": "...", "url": "...", "limit": N}
  ASSUMPTION — field names may differ.

- Response shape:  {"install_code": "..."}
  ASSUMPTION — key name may differ.

- install_code lifetime and idempotency are UNKNOWN.
  A new code is requested on every user page render that has the feature
  enabled.  Whether old codes expire or accumulate on Happ-proxy's side is
  unknown.  Add server-side caching (e.g. per-user DB field) once API
  behaviour is confirmed.

FAILURE MODES
=============
All failures (timeout, non-200, unexpected response shape, network error) are
logged at WARNING level and return None.  Callers must fall back to the plain
subscription URL — never crash the page.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx

from backend.config import Settings

logger = logging.getLogger(__name__)

# ASSUMPTION: this is the correct Happ Limited Links API path.
_CREATE_PATH = "/api/install/create"
_REQUEST_TIMEOUT_SECONDS = 5.0


def is_enabled(settings: Settings) -> bool:
    """Return True only when all required Limited Links config fields are present."""
    return (
        settings.happ_limited_links_enabled
        and bool(settings.happ_provider_code)
        and bool(settings.happ_auth_key)
    )


def get_limited_install_code(
    *,
    subscription_url: str,
    max_devices: int,
    settings: Settings,
) -> str | None:
    """Request an install_code from the Happ Limited Links API.

    Returns the install_code string on success, or None on any failure.
    Never raises — failures are logged and the caller falls back gracefully.

    API shape is ASSUMED (see module docstring).
    """
    if not is_enabled(settings):
        return None

    api_base = settings.happ_api_url or "https://happ-proxy.com"
    url = urljoin(api_base, _CREATE_PATH)

    try:
        resp = httpx.post(
            url,
            json={
                # ASSUMPTION: these field names match the real API.
                "provider_code": settings.happ_provider_code,
                "auth_key": settings.happ_auth_key,
                "url": subscription_url,
                "limit": max_devices,
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        logger.warning(
            "happ_limited_links: API timed out after %.1fs (url=%s)",
            _REQUEST_TIMEOUT_SECONDS,
            subscription_url,
        )
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "happ_limited_links: API returned HTTP %s — %s",
            exc.response.status_code,
            exc.response.text[:300],
        )
        return None
    except Exception as exc:  # noqa: BLE001  — intentional catch-all
        logger.warning("happ_limited_links: unexpected error — %s", exc)
        return None

    try:
        data = resp.json()
    except Exception:
        logger.warning(
            "happ_limited_links: response is not JSON — %r", resp.text[:200]
        )
        return None

    # ASSUMPTION: the install_code lives at data["install_code"].
    install_code = data.get("install_code")
    if not install_code or not isinstance(install_code, str):
        logger.warning(
            "happ_limited_links: unexpected response shape — %r", data
        )
        return None

    return install_code


def build_limited_subscription_url(
    *,
    subscription_url: str,
    install_code: str,
) -> str:
    """Return subscription_url with ?InstallID={install_code} appended.

    Preserves any existing query parameters.  The InstallID parameter is
    what Happ reads to contact happ-proxy.com and enforce the install cap.
    """
    parsed = urlparse(subscription_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["InstallID"] = [install_code]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))
