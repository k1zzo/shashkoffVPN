"""Reserved tokens that must not be used as user public_token values.

These collide with existing route prefixes or static mounts. The /{token}
catch-all route would shadow them if a user were assigned one of these tokens.
"""

from __future__ import annotations

RESERVED_TOKENS: frozenset[str] = frozenset({
    "admin",
    "api",
    "docs",
    "health",
    "open",
    "openapi.json",
    "redoc",
    "static",
    "sub",
    "u",
})


def is_token_reserved(token: str) -> bool:
    return token.strip().lower() in RESERVED_TOKENS
