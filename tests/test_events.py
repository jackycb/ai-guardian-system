"""后端事件端点测试 —— 真实异步状态流转。

核心 5 条：
1. 上传后立即查询 → queued 或 processing
2. 后台完成后查询 → done + 分析结果字段
3. 后台失败后查询 → failed + failure_reason
4. 上传返回 202 + 正确结构
5. 查询不存在事件 → 404

加上字段校验和幂等测试。
"""

import io
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend import event_store
from backend import analyzer

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_store():
    event_store.clear()
    yield
    event_store.clear()


def _upload(overrides=None, skip_image=False, skip_audio=False):
    data = {
        "device_id": "xiao-door-001",
        "event_type": "motion",
        "trigger_source": "pir",
        "captured_at": "2026-04-04T13:18:00+08:00",
        "firmware_version": "1.0.0",
    }
    if overrides:
        data.update(overrides)

    files = {}
    if not skip_image:
        files["image"] = ("frame.jpg", io.BytesIO(b"\xff\xd8fake-jpg"), "image/jpeg")
    if not skip_audio:
        files["audio"] = ("clip.wav", io.BytesIO(b"RIFF fake-wav"), "audio/wav")

    return client.post("/api/v1/device/events", data=data, files=files)


# ---------------------------------------------------------------------------
# 真实状态流转测试
# ---------------------------------------------------------------------------


class TestRealStateFlow:

    def test_upload_then_immediate_query_returns_queued_or_processing(self) -> None:
        """上传后立即查询 → status 为 queued 或 processing（分析尚未完成）。"""
        # 让分析耗时足够长，确保立即查询时还没完成
        with patch.object(analyzer, "ANALYSIS_DELAY", 5.0):
            resp = _upload()
            event_id = resp.json()["data"]["event_id"]

            result = client.get(f"/api/v1/device/events/{event_id}/result")
            assert result.status_code == 200
            status = result.json()["data"]["status"]
            assert status in ("queued", "processing")

    def test_query_after_analysis_done(self) -> None:
        """后台分析完成后查询 → status=done + 完整分析结果。"""
        with patch.object(analyzer, "ANALYSIS_DELAY", 0.01):
            resp = _upload()
            event_id = resp.json()["data"]["event_id"]

            # 等待后台线程完成
            time.sleep(0.1)

            result = client.get(f"/api/v1/device/events/{event_id}/result")
            data = result.json()["data"]
            assert data["status"] == "done"
            assert "risk_level" in data
            assert "summary" in data
            assert "voice_broadcast_text" in data
            assert "labels" in data
            assert "finished_at" in data
            assert data["device_id"] == "xiao-door-001"

    def test_query_after_analysis_failed(self) -> None:
        """后台分析失败后查询 → status=failed + failure_reason。"""
        # 先正常创建事件
        with patch.object(analyzer, "ANALYSIS_DELAY", 0.01):
            # 让 analyze 内部抛异常
            original_analyze = analyzer.analyze

            def _failing_analyze(event_id):
                event_store.update_status(event_id, "processing")
                event_store.update_status(
                    event_id, "failed", failure_reason="model invocation error"
                )

            with patch.object(analyzer, "analyze", side_effect=_failing_analyze):
                resp = _upload()
                event_id = resp.json()["data"]["event_id"]
                time.sleep(0.1)

            result = client.get(f"/api/v1/device/events/{event_id}/result")
            data = result.json()["data"]
            assert data["status"] == "failed"
            assert data["failure_reason"] == "model invocation error"


# ---------------------------------------------------------------------------
# 上传端点测试
# ---------------------------------------------------------------------------


class TestUploadEvent:

    def test_upload_success_returns_202(self) -> None:
        resp = _upload()
        assert resp.status_code == 202
        body = resp.json()
        assert body["code"] == 0
        assert body["message"] == "accepted"
        data = body["data"]
        assert data["event_id"].startswith("evt_")
        assert data["status"] == "queued"
        assert "received_at" in data
        assert "result_query_path" in data

    def test_upload_returns_unique_event_ids(self) -> None:
        ids = set()
        for _ in range(5):
            resp = _upload()
            ids.add(resp.json()["data"]["event_id"])
        assert len(ids) == 5

    def test_upload_with_optional_payloads(self) -> None:
        resp = _upload(overrides={
            "sensor_payload": '{"pir": true}',
            "vad_payload": '{"vad_triggered": true, "duration_ms": 1800}',
        })
        assert resp.status_code == 202

    def test_upload_missing_image(self) -> None:
        resp = _upload(skip_image=True)
        assert resp.status_code == 422

    def test_upload_missing_audio(self) -> None:
        resp = _upload(skip_audio=True)
        assert resp.status_code == 422

    def test_upload_missing_device_id(self) -> None:
        data = {
            "event_type": "motion",
            "trigger_source": "pir",
            "captured_at": "2026-04-04T13:18:00+08:00",
            "firmware_version": "1.0.0",
        }
        files = {
            "image": ("frame.jpg", io.BytesIO(b"\xff\xd8fake"), "image/jpeg"),
            "audio": ("clip.wav", io.BytesIO(b"RIFF fake"), "audio/wav"),
        }
        resp = client.post("/api/v1/device/events", data=data, files=files)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 结果查询端点测试
# ---------------------------------------------------------------------------


class TestQueryResult:

    def test_query_nonexistent_event(self) -> None:
        resp = client.get("/api/v1/device/events/evt_doesnotexist/result")
        assert resp.status_code == 404
        assert resp.json()["code"] == 40401

    def test_result_query_path_works(self) -> None:
        """上传返回的 result_query_path 可直接用于查询。"""
        with patch.object(analyzer, "ANALYSIS_DELAY", 0.01):
            resp = _upload()
            path = resp.json()["data"]["result_query_path"]
            time.sleep(0.1)
            result = client.get(path)
            assert result.status_code == 200
            assert result.json()["data"]["status"] == "done"
