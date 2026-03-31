from __future__ import annotations

from fastapi import Request

from backend.config import Settings


def build_app_url(*, path: str, request: Request, settings: Settings) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"

    if settings.app_base_url_configured:
        return f"{settings.app_base_url}{normalized_path}"

    request_base = str(request.base_url).rstrip("/")
    return f"{request_base}{normalized_path}"

