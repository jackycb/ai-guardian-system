"""TTS 端点 + 设备鉴权测试。"""

import io
import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend import event_store
from backend import analyzer
from backend import auth

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    event_store.clear()
    yield
    event_store.clear()


def _upload(overrides=None, **kwargs):
    data = {
        "device_id": "xiao-door-001",
        "event_type": "motion",
        "trigger_source": "pir",
        "captured_at": "2026-04-04T13:18:00+08:00",
        "firmware_version": "1.0.0",
    }
    if overrides:
        data.update(overrides)
    files = {
        "image": ("frame.jpg", io.BytesIO(b"\xff\xd8fake-jpg"), "image/jpeg"),
        "audio": ("clip.wav", io.BytesIO(b"RIFF fake-wav"), "audio/wav"),
    }
    return client.post(
        "/api/v1/device/events", data=data, files=files, **kwargs
    )


# ---------------------------------------------------------------------------
# TTS 端点测试
# ---------------------------------------------------------------------------


class TestTTSEndpoint:

    def _create_done_event(self):
        """上传并等待分析完成，返回 event_id。"""
        with patch.object(analyzer, "ANALYSIS_DELAY", 0.01):
            resp = _upload()
            event_id = resp.json()["data"]["event_id"]
            time.sleep(0.15)
            return event_id

    def test_tts_returns_pcm_audio(self) -> None:
        event_id = self._create_done_event()
        resp = client.get(f"/api/v1/device/events/{event_id}/tts")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/octet-stream"
        # PCM 应该是偶数字节（16-bit samples）
        assert len(resp.content) > 0
        assert len(resp.content) % 2 == 0

    def test_tts_has_content_length(self) -> None:
        event_id = self._create_done_event()
        resp = client.get(f"/api/v1/device/events/{event_id}/tts")
        assert "content-length" in resp.headers
        assert int(resp.headers["content-length"]) == len(resp.content)

    def test_tts_nonexistent_event(self) -> None:
        resp = client.get("/api/v1/device/events/evt_doesnotexist/tts")
        assert resp.status_code == 404
        assert resp.json()["code"] == 40401

    def test_tts_not_ready_before_done(self) -> None:
        """分析未完成时 TTS 不可用。"""
        with patch.object(analyzer, "ANALYSIS_DELAY", 10.0):
            resp = _upload()
            event_id = resp.json()["data"]["event_id"]
            # 立即请求 TTS
            tts_resp = client.get(f"/api/v1/device/events/{event_id}/tts")
            assert tts_resp.status_code == 404
            assert tts_resp.json()["code"] == 40402

    def test_result_includes_tts_audio_path(self) -> None:
        """分析完成后 result 端点包含 tts_audio_path。"""
        event_id = self._create_done_event()
        resp = client.get(f"/api/v1/device/events/{event_id}/result")
        data = resp.json()["data"]
        assert data["status"] == "done"
        assert "tts_audio_path" in data
        assert event_id in data["tts_audio_path"]


# ---------------------------------------------------------------------------
# 鉴权测试
# ---------------------------------------------------------------------------


class TestDeviceAuth:

    def test_health_public_no_token(self) -> None:
        """Health 端点无需 Token。"""
        with patch.object(auth, "DEVICE_TOKEN", "secret123"):
            resp = client.get("/api/v1/health")
            assert resp.status_code == 200

    def test_upload_rejected_without_token(self) -> None:
        with patch.object(auth, "DEVICE_TOKEN", "secret123"):
            resp = _upload()
            assert resp.status_code == 401
            assert resp.json()["code"] == 40101
            assert resp.json()["message"] == "device authentication failed"

    def test_upload_rejected_wrong_token(self) -> None:
        with patch.object(auth, "DEVICE_TOKEN", "secret123"):
            resp = _upload(headers={"Authorization": "Bearer wrong"})
            assert resp.status_code == 401
            assert resp.json()["code"] == 40101

    def test_upload_ok_with_correct_token(self) -> None:
        with patch.object(auth, "DEVICE_TOKEN", "secret123"):
            resp = _upload(headers={"Authorization": "Bearer secret123"})
            assert resp.status_code == 202

    def test_no_token_configured_allows_all(self) -> None:
        """DEVICE_TOKEN 为空时跳过鉴权（开发模式）。"""
        with patch.object(auth, "DEVICE_TOKEN", ""):
            resp = _upload()
            assert resp.status_code == 202

    def test_result_query_needs_token(self) -> None:
        with patch.object(auth, "DEVICE_TOKEN", "secret123"):
            resp = client.get(
                "/api/v1/device/events/evt_xxx/result",
                headers={"Authorization": "Bearer secret123"},
            )
            # 404 is expected (event doesn't exist), not 401/403
            assert resp.status_code == 404

    def test_tts_needs_token(self) -> None:
        with patch.object(auth, "DEVICE_TOKEN", "secret123"):
            resp = client.get("/api/v1/device/events/evt_xxx/tts")
            assert resp.status_code == 401

    def test_tts_download_with_correct_token(self) -> None:
        """鉴权开启后，正确 token 可下载 TTS 音频。"""
        # 先在无鉴权下创建事件
        with patch.object(auth, "DEVICE_TOKEN", ""):
            with patch.object(analyzer, "ANALYSIS_DELAY", 0.01):
                resp = _upload()
                event_id = resp.json()["data"]["event_id"]
                time.sleep(0.15)

        # 开启鉴权后用正确 token 下载
        with patch.object(auth, "DEVICE_TOKEN", "secret123"):
            resp = client.get(
                f"/api/v1/device/events/{event_id}/tts",
                headers={"Authorization": "Bearer secret123"},
            )
            assert resp.status_code == 200
            assert len(resp.content) > 0


# ---------------------------------------------------------------------------
# captured_at 契约测试
# ---------------------------------------------------------------------------


class TestCapturedAtValidation:

    def test_valid_iso8601_accepted(self) -> None:
        resp = _upload()
        assert resp.status_code == 202

    def test_valid_iso8601_with_z(self) -> None:
        resp = _upload(overrides={"captured_at": "2026-04-04T13:18:00Z"})
        assert resp.status_code == 202

    def test_valid_iso8601_with_fractional(self) -> None:
        resp = _upload(overrides={"captured_at": "2026-04-04T13:18:00.123+00:00"})
        assert resp.status_code == 202

    def test_invalid_captured_at_rejected(self) -> None:
        """非 ISO 8601 格式被拒绝 → 400 + code 40001。"""
        resp = _upload(overrides={"captured_at": "device_uptime_12s"})
        assert resp.status_code == 400
        assert resp.json()["code"] == 40001

    def test_suffix_appended_iso_rejected(self) -> None:
        """追加后缀的伪 ISO 时间被拒绝。"""
        resp = _upload(
            overrides={"captured_at": "2026-04-04T13:18:00+00:00+offset_5s"}
        )
        assert resp.status_code == 400
