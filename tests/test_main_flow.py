"""设备端主链路集成测试 —— 完整 run_once 流程。

5 个核心场景：
1. 正常成功路径：trigger → capture → health → upload → poll(done) → feedback → idle
2. health check 失败 → upload_failed → idle
3. 上传失败 → upload_failed → idle
4. 轮询超时 → result_timeout → idle
5. 分析失败 → complete(skip feedback) → idle
"""

import os
import tempfile
from unittest.mock import patch, MagicMock

import httpx
import pytest

from device.client import GuardianClient
from device.state_machine import State
from device.adapters import (
    TriggerSource,
    TriggerInfo,
    CaptureManager,
    CaptureResult,
    FeedbackPlayer,
    StubTrigger,
    LogFeedback,
)


# ---------------------------------------------------------------------------
# fixtures & helpers
# ---------------------------------------------------------------------------


class FixedCapture(CaptureManager):
    """测试用 capture：使用预建的临时文件。"""

    def __init__(self, img_path: str, aud_path: str) -> None:
        self._img = img_path
        self._aud = aud_path

    def capture(self, trigger: TriggerInfo):
        return CaptureResult(image_path=self._img, audio_path=self._aud)


class FailCapture(CaptureManager):
    def capture(self, trigger: TriggerInfo):
        return None


class RecordingFeedback(FeedbackPlayer):
    """记录是否被调用以及传入的结果。"""

    def __init__(self):
        self.played = False
        self.last_result = None

    def play(self, result):
        self.played = True
        self.last_result = result
        return True


@pytest.fixture()
def sample_files():
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as img:
        img.write(b"\xff\xd8fake-jpeg-data")
        img_path = img.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as aud:
        aud.write(b"RIFF fake-wav-data")
        aud_path = aud.name
    yield img_path, aud_path
    for p in (img_path, aud_path):
        if os.path.exists(p):
            os.unlink(p)


def _health_ok():
    return httpx.Response(200, json={
        "code": 0, "message": "ok",
        "data": {"service": "ai-guardian-backend", "status": "up", "time": "t", "version": "v1"},
    })


def _upload_accepted(event_id="evt_test123"):
    return httpx.Response(202, json={
        "code": 0, "message": "accepted",
        "data": {
            "event_id": event_id, "status": "queued",
            "received_at": "2026-04-04T13:20:00+08:00",
            "result_query_path": f"/api/v1/device/events/{event_id}/result",
        },
    })


def _result_done(event_id="evt_test123"):
    return httpx.Response(200, json={
        "code": 0, "message": "ok",
        "data": {
            "event_id": event_id, "device_id": "xiao-door-001",
            "status": "done", "event_type": "motion", "trigger_source": "pir",
            "risk_level": "low", "summary": "测试摘要",
            "labels": ["motion", "pir"],
            "voice_broadcast_text": "门口检测到motion事件，请留意。",
            "voice_broadcast_level": "info",
            "captured_at": "2026-04-04T13:18:00+08:00",
            "finished_at": "2026-04-04T13:20:01+08:00",
        },
    })


def _result_failed(event_id="evt_test123"):
    return httpx.Response(200, json={
        "code": 0, "message": "ok",
        "data": {
            "event_id": event_id, "device_id": "xiao-door-001",
            "status": "failed", "failure_reason": "model timeout",
        },
    })


def _mock_dispatch(url, **kw):
    """根据 URL 返回对应的 mock response。"""
    if url.endswith("/health"):
        return _health_ok()
    if "/result" in url:
        return _result_done()
    return httpx.Response(404)


# ---------------------------------------------------------------------------
# 1. 正常成功路径
# ---------------------------------------------------------------------------


class TestSuccess:

    def test_full_success(self, sample_files) -> None:
        img, aud = sample_files
        fb = RecordingFeedback()
        gc = GuardianClient(
            "http://localhost:8000",
            capture=FixedCapture(img, aud),
            feedback=fb,
        )

        with patch("device.client.httpx.get", side_effect=_mock_dispatch):
            with patch("device.client.httpx.post", return_value=_upload_accepted()):
                result = gc.run_once()

        assert result is True
        assert gc.sm.state == State.IDLE
        assert fb.played is True
        assert fb.last_result["status"] == "done"

        h = gc.sm.history
        states_visited = [entry.split(" -> ")[1].split(" ")[0] for entry in h]
        assert "triggered" in states_visited
        assert "capturing" in states_visited
        assert "ready_to_upload" in states_visited
        assert "uploading" in states_visited
        assert "waiting_result" in states_visited
        assert "playing_feedback" in states_visited
        assert "complete" in states_visited


# ---------------------------------------------------------------------------
# 2. health check 失败
# ---------------------------------------------------------------------------


class TestHealthFail:

    def test_health_refused(self, sample_files) -> None:
        img, aud = sample_files
        gc = GuardianClient(
            "http://localhost:8000",
            capture=FixedCapture(img, aud),
        )

        with patch("device.client.httpx.get", side_effect=httpx.ConnectError("refused")):
            result = gc.run_once()

        assert result is False
        assert gc.sm.state == State.IDLE
        assert any("upload_failed" in h and "health check failed" in h for h in gc.sm.history)


# ---------------------------------------------------------------------------
# 3. 上传失败
# ---------------------------------------------------------------------------


class TestUploadFail:

    def test_upload_500(self, sample_files) -> None:
        img, aud = sample_files
        gc = GuardianClient(
            "http://localhost:8000",
            capture=FixedCapture(img, aud),
        )

        with patch("device.client.httpx.get", return_value=_health_ok()):
            with patch("device.client.httpx.post", return_value=httpx.Response(500, text="error")):
                result = gc.run_once()

        assert result is False
        assert gc.sm.state == State.IDLE
        assert any("upload_failed" in h and "upload rejected" in h for h in gc.sm.history)


# ---------------------------------------------------------------------------
# 4. 轮询超时
# ---------------------------------------------------------------------------


class TestPollTimeout:

    @patch("device.client.POLL_MAX_ATTEMPTS", 2)
    @patch("device.client.POLL_INTERVAL", 0.01)
    def test_poll_exhausted(self, sample_files) -> None:
        img, aud = sample_files
        gc = GuardianClient(
            "http://localhost:8000",
            capture=FixedCapture(img, aud),
        )

        processing_resp = httpx.Response(200, json={
            "code": 0, "message": "ok",
            "data": {"event_id": "evt_test123", "device_id": "xiao-door-001", "status": "processing"},
        })

        with patch("device.client.httpx.get", side_effect=lambda url, **kw:
                    _health_ok() if url.endswith("/health") else processing_resp):
            with patch("device.client.httpx.post", return_value=_upload_accepted()):
                result = gc.run_once()

        assert result is False
        assert gc.sm.state == State.IDLE
        assert any("result_timeout" in h for h in gc.sm.history)


# ---------------------------------------------------------------------------
# 5. 分析失败 → skip feedback → complete
# ---------------------------------------------------------------------------


class TestAnalysisFailed:

    def test_analysis_failed_skips_feedback(self, sample_files) -> None:
        img, aud = sample_files
        fb = RecordingFeedback()
        gc = GuardianClient(
            "http://localhost:8000",
            capture=FixedCapture(img, aud),
            feedback=fb,
        )

        def _dispatch(url, **kw):
            if url.endswith("/health"):
                return _health_ok()
            if "/result" in url:
                return _result_failed()
            return httpx.Response(404)

        with patch("device.client.httpx.get", side_effect=_dispatch):
            with patch("device.client.httpx.post", return_value=_upload_accepted()):
                result = gc.run_once()

        assert result is False
        assert gc.sm.state == State.IDLE
        # feedback 不应被调用
        assert fb.played is False
        assert any("complete" in h and "analysis failed" in h for h in gc.sm.history)


# ---------------------------------------------------------------------------
# 6. 采集失败
# ---------------------------------------------------------------------------


class TestCaptureFail:

    def test_capture_returns_none(self) -> None:
        gc = GuardianClient(
            "http://localhost:8000",
            capture=FailCapture(),
        )

        result = gc.run_once()

        assert result is False
        assert gc.sm.state == State.IDLE
        assert any("capture_failed" in h for h in gc.sm.history)
