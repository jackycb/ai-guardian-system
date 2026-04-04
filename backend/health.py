"""/api/v1/health 端点实现。"""

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger("backend.health")

router = APIRouter()

_start_time: float = time.monotonic()

SERVICE_NAME = "ai-guardian-backend"
VERSION = "v1"


@router.get("/api/v1/health")
async def health_check() -> JSONResponse:
    uptime = time.monotonic() - _start_time
    now = datetime.now(timezone.utc).isoformat()

    logger.info("health check requested, uptime=%.1fs", uptime)

    return JSONResponse(
        status_code=200,
        content={
            "code": 0,
            "message": "ok",
            "data": {
                "service": SERVICE_NAME,
                "status": "up",
                "time": now,
                "version": VERSION,
                "uptime_seconds": round(uptime, 2),
            },
        },
    )
