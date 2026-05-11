from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.config import get_settings
from backend.db import init_db, upgrade_db_schema
from backend.routes.devices import router as devices_router
from backend.routes.health import router as health_router
from backend.routes.profile import router as profile_router
from backend.routes.user_page import router as user_page_router
from backend.xray_reconciler import reconciler_loop

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    upgrade_db_schema()
    app.state.templates = Jinja2Templates(directory=str(settings.templates_dir))

    # Background reconciler — converges Xray runtime state to the DB every
    # XRAY_RECONCILER_INTERVAL_SECONDS. The loop is a no-op when
    # XRAY_USE_HANDLER_API=false or XRAY_API_ADDR is unset (logged once).
    stop_event = asyncio.Event()
    reconciler_task = asyncio.create_task(
        reconciler_loop(settings, stop_event),
        name="xray-reconciler",
    )
    app.state.xray_reconciler_stop = stop_event
    app.state.xray_reconciler_task = reconciler_task

    try:
        yield
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(reconciler_task, timeout=5)
        except asyncio.TimeoutError:
            logger.warning("xray_reconciler: shutdown timed out, cancelling")
            reconciler_task.cancel()
            try:
                await reconciler_task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.app_trusted_hosts))
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
app.include_router(health_router)
app.include_router(devices_router)
app.include_router(profile_router)
app.include_router(user_page_router)
