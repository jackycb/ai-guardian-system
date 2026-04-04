"""FastAPI 应用入口。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.logging_config import setup_logging
from backend.auth import DeviceTokenMiddleware
from backend.health import router as health_router
from backend.events import router as events_router

setup_logging()
logger = logging.getLogger("backend.app")


@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info("ai-guardian-backend started")
    yield
    logger.info("ai-guardian-backend shutting down")


app = FastAPI(title="AI Guardian Backend", version="0.1.0", lifespan=lifespan)
app.add_middleware(DeviceTokenMiddleware)
app.include_router(health_router)
app.include_router(events_router)
