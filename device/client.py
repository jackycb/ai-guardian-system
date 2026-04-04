"""设备端主流程编排：trigger → capture → health check → upload → poll → feedback。

网络层（health / upload / poll）为真实 HTTP 调用。
trigger / capture / feedback 通过适配器注入，底层可替换。
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from device.state_machine import DeviceStateMachine, State
from device.adapters import (
    TriggerSource,
    CaptureManager,
    FeedbackPlayer,
    StubTrigger,
    StubCapture,
    LogFeedback,
    TriggerInfo,
    CaptureResult,
)

logger = logging.getLogger("device.client")

HEALTH_TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT", "3.0"))
UPLOAD_TIMEOUT = float(os.environ.get("UPLOAD_TIMEOUT", "30.0"))
POLL_TIMEOUT = float(os.environ.get("POLL_TIMEOUT", "5.0"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2.0"))
POLL_MAX_ATTEMPTS = int(os.environ.get("POLL_MAX_ATTEMPTS", "10"))


class GuardianClient:
    """设备端主控，驱动状态机并编排完整事件处理流程。"""

    def __init__(
        self,
        base_url: str,
        device_id: str = "xiao-door-001",
        firmware_version: str = "1.0.0",
        trigger: Optional[TriggerSource] = None,
        capture: Optional[CaptureManager] = None,
        feedback: Optional[FeedbackPlayer] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.firmware_version = firmware_version
        self.health_url = f"{self.base_url}/api/v1/health"
        self.upload_url = f"{self.base_url}/api/v1/device/events"
        self.sm = DeviceStateMachine()

        # 适配器：默认用 stub，可注入真实实现
        self.trigger = trigger or StubTrigger()
        self.capture = capture or StubCapture()
        self.feedback = feedback or LogFeedback()

    # ------------------------------------------------------------------
    # network: health check
    # ------------------------------------------------------------------

    def check_health(self) -> bool:
        try:
            resp = httpx.get(self.health_url, timeout=HEALTH_TIMEOUT)
            if resp.status_code == 200:
                body = resp.json()
                status = body.get("data", {}).get("status")
                if status == "up":
                    logger.info("health check passed, url=%s", self.health_url)
                    return True
                logger.warning("health check non-up status=%s", status)
                return False
            logger.warning("health check status_code=%d", resp.status_code)
            return False
        except httpx.TimeoutException:
            logger.error("health check timeout, url=%s", self.health_url)
            return False
        except httpx.ConnectError:
            logger.error("health check connection refused, url=%s", self.health_url)
            return False
        except httpx.HTTPError as exc:
            logger.error("health check error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # network: upload
    # ------------------------------------------------------------------

    def upload_event(
        self,
        trigger_info: TriggerInfo,
        capture_result: CaptureResult,
    ) -> Optional[str]:
        """上传事件，返回 event_id 或 None。"""
        fields = {
            "device_id": self.device_id,
            "event_type": trigger_info.event_type,
            "trigger_source": trigger_info.trigger_source,
            "captured_at": trigger_info.captured_at,
            "firmware_version": self.firmware_version,
        }
        if trigger_info.sensor_payload is not None:
            fields["sensor_payload"] = json.dumps(trigger_info.sensor_payload)
        if trigger_info.vad_payload is not None:
            fields["vad_payload"] = json.dumps(trigger_info.vad_payload)

        try:
            with open(capture_result.image_path, "rb") as img, \
                 open(capture_result.audio_path, "rb") as aud:
                files = {
                    "image": (os.path.basename(capture_result.image_path), img, "image/jpeg"),
                    "audio": (os.path.basename(capture_result.audio_path), aud, "audio/wav"),
                }
                resp = httpx.post(
                    self.upload_url, data=fields, files=files, timeout=UPLOAD_TIMEOUT,
                )

            if resp.status_code == 202:
                event_id = resp.json().get("data", {}).get("event_id")
                logger.info("upload ok, event_id=%s", event_id)
                return event_id

            logger.warning("upload failed, status_code=%d, body=%s", resp.status_code, resp.text)
            return None
        except (httpx.HTTPError, OSError) as exc:
            logger.error("upload error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # network: poll result
    # ------------------------------------------------------------------

    def poll_result(self, event_id: str) -> Optional[Dict[str, Any]]:
        """轮询结果，直到 done/failed 或超出重试上限。"""
        result_url = f"{self.base_url}/api/v1/device/events/{event_id}/result"

        for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
            try:
                resp = httpx.get(result_url, timeout=POLL_TIMEOUT)
                if resp.status_code != 200:
                    logger.warning("poll status_code=%d, attempt=%d", resp.status_code, attempt)
                    time.sleep(POLL_INTERVAL)
                    continue

                data = resp.json().get("data", {})
                status = data.get("status")

                if status in ("done", "failed"):
                    logger.info("poll result %s, event_id=%s, attempt=%d", status, event_id, attempt)
                    return data

                logger.info("poll status=%s, event_id=%s, attempt=%d", status, event_id, attempt)

            except httpx.HTTPError as exc:
                logger.error("poll error: %s, attempt=%d", exc, attempt)

            time.sleep(POLL_INTERVAL)

        logger.error("poll timeout after %d attempts, event_id=%s", POLL_MAX_ATTEMPTS, event_id)
        return None

    # ------------------------------------------------------------------
    # main flow: run_once
    # ------------------------------------------------------------------

    def run_once(self) -> bool:
        """执行一次完整的事件处理闭环。

        状态流转：
            IDLE → TRIGGERED → CAPTURING → READY_TO_UPLOAD → UPLOADING
                 → WAITING_RESULT → PLAYING_FEEDBACK → COMPLETE → IDLE

        Returns:
            True = 完整成功，False = 流程中断（已恢复到 IDLE）。
        """
        try:
            # 1. IDLE → TRIGGERED
            self.sm.transition_to(State.TRIGGERED, "trigger detected")
            trigger_info = self.trigger.wait_for_trigger()
            if trigger_info is None:
                self.sm.transition_to(State.IDLE, "invalid trigger")
                return False

            # 2. TRIGGERED → CAPTURING
            self.sm.transition_to(State.CAPTURING, f"source={trigger_info.trigger_source}")
            capture_result = self.capture.capture(trigger_info)
            if capture_result is None:
                self.sm.transition_to(State.CAPTURE_FAILED, "capture error")
                self.sm.transition_to(State.IDLE, "give up")
                return False

            # 3. CAPTURING → READY_TO_UPLOAD (health check)
            self.sm.transition_to(State.READY_TO_UPLOAD, "capture done")
            if not self.check_health():
                self.sm.transition_to(State.UPLOAD_FAILED, "health check failed")
                self.sm.transition_to(State.IDLE, "give up")
                return False

            # 4. READY_TO_UPLOAD → UPLOADING
            self.sm.transition_to(State.UPLOADING, "health ok")
            event_id = self.upload_event(trigger_info, capture_result)
            if event_id is None:
                self.sm.transition_to(State.UPLOAD_FAILED, "upload rejected")
                self.sm.transition_to(State.IDLE, "give up")
                return False

            # 5. UPLOADING → WAITING_RESULT
            self.sm.transition_to(State.WAITING_RESULT, f"event_id={event_id}")
            result = self.poll_result(event_id)
            if result is None:
                self.sm.transition_to(State.RESULT_TIMEOUT, "poll exhausted")
                self.sm.transition_to(State.IDLE, "give up")
                return False

            # 如果分析失败，跳过播放直接完成
            if result.get("status") == "failed":
                self.sm.transition_to(State.COMPLETE, "analysis failed, skip feedback")
                self.sm.transition_to(State.IDLE, "cycle complete")
                return False

            # 6. WAITING_RESULT → PLAYING_FEEDBACK
            self.sm.transition_to(State.PLAYING_FEEDBACK, f"status={result.get('status')}")
            playback_ok = self.feedback.play(result)
            if not playback_ok:
                self.sm.transition_to(State.PLAYBACK_FAILED, "playback error")
                self.sm.transition_to(State.COMPLETE, "skip playback")
                self.sm.transition_to(State.IDLE, "cycle complete")
                return False

            # 7. PLAYING_FEEDBACK → COMPLETE → IDLE
            self.sm.transition_to(State.COMPLETE, "feedback done")
            self.sm.transition_to(State.IDLE, "cycle complete")
            return True

        except Exception as exc:
            logger.error("run_once unexpected error: %s", exc)
            self.sm.reset()
            return False

        finally:
            # 清理临时采集文件
            if "capture_result" in dir() and capture_result is not None:
                for path in (capture_result.image_path, capture_result.audio_path):
                    try:
                        if os.path.exists(path):
                            os.unlink(path)
                    except OSError:
                        pass
