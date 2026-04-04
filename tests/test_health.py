"""Health check 相关测试，覆盖 test-plan F-001/F-002/F-003 和 T-D1~T-D4。"""

from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.app import app
from device.client import GuardianClient

# ---------------------------------------------------------------------------
# 后端 /api/v1/health 测试
# ---------------------------------------------------------------------------

client = TestClient(app)


class TestHealthEndpoint:

    def test_health_returns_200(self) -> None:
        """F-001: 健康检查成功，返回 200 且 status=up。"""
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["status"] == "up"

    def test_health_fields_complete(self) -> None:
        """F-002: 响应字段完整。"""
        resp = client.get("/api/v1/health")
        body = resp.json()
        assert "code" in body
        assert "message" in body
        data = body["data"]
        assert data["service"] == "ai-guardian-backend"
        assert "time" in data
        assert "version" in data
        assert data["uptime_seconds"] >= 0

    def test_health_content_type(self) -> None:
        """F-003: Content-Type 为 application/json。"""
        resp = client.get("/api/v1/health")
        assert "application/json" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# 设备端连通性检查测试
# ---------------------------------------------------------------------------


def _make_health_response(status_code: int = 200, status: str = "up") -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json={
            "code": 0,
            "message": "ok",
            "data": {"service": "ai-guardian-backend", "status": status, "time": "t", "version": "v1"},
        },
    )


class TestDeviceHealthCheck:

    def setup_method(self) -> None:
        self.gc = GuardianClient("http://localhost:8000")

    def test_health_ok_returns_true(self) -> None:
        with patch("device.client.httpx.get", return_value=_make_health_response()):
            assert self.gc.check_health() is True

    def test_health_503_returns_false(self) -> None:
        resp = httpx.Response(status_code=503, json={"code": 1, "message": "degraded", "data": None})
        with patch("device.client.httpx.get", return_value=resp):
            assert self.gc.check_health() is False

    def test_health_timeout_returns_false(self) -> None:
        with patch("device.client.httpx.get", side_effect=httpx.TimeoutException("timeout")):
            assert self.gc.check_health() is False

    def test_health_connection_refused_returns_false(self) -> None:
        with patch("device.client.httpx.get", side_effect=httpx.ConnectError("refused")):
            assert self.gc.check_health() is False
