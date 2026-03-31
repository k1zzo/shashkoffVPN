from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from backend.db import session_scope
from backend.models import User


def seed_data() -> None:
    with session_scope() as session:
        existing_user = session.scalar(
            select(User).where(User.public_token == "demo-token")
        )
        if existing_user is not None:
            return

        demo_user = User(
            username="demo-user",
            public_token="demo-token",
            uuid="demo-uuid-placeholder",
            is_active=True,
            max_devices=5,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(demo_user)
        session.commit()

