"""最小设备鉴权 —— Bearer Token 校验。

环境变量 DEVICE_TOKEN 控制预共享密钥。
未配置时跳过鉴权（dev 模式）。
"""

import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = logging.getLogger("backend.auth")

DEVICE_TOKEN: str = os.environ.get("DEVICE_TOKEN", "")

# 不需要鉴权的路径前缀
_PUBLIC_PATHS = ("/api/v1/health", "/docs", "/openapi.json")


class DeviceTokenMiddleware(BaseHTTPMiddleware):
    """校验 Authorization: Bearer <token>。

    DEVICE_TOKEN 为空时自动跳过（开发模式）。
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ):
        # 公开路径跳过
        if any(request.url.path.startswith(p) for p in _PUBLIC_PATHS):
            return await call_next(request)

        # dev 模式：未配置 token 则跳过
        if not DEVICE_TOKEN:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != DEVICE_TOKEN:
            logger.warning("auth failed, path=%s", request.url.path)
            return JSONResponse(
                status_code=401,
                content={"code": 40101, "message": "device authentication failed", "data": None},
            )

        return await call_next(request)
