"""设备事件上传与结果查询端点。"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

from backend import event_store
from backend import analyzer

# ISO 8601 基本校验：YYYY-MM-DDTHH:MM:SS 后可选小数秒和时区
_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(\.\d+)?"               # 可选小数秒
    r"([+-]\d{2}:\d{2}|Z)?$"  # 可选时区
)

logger = logging.getLogger("backend.events")

router = APIRouter(prefix="/api/v1/device")


@router.post("/events")
async def upload_event(
    device_id: str = Form(...),
    event_type: str = Form(...),
    trigger_source: str = Form(...),
    captured_at: str = Form(...),
    firmware_version: str = Form(...),
    image: UploadFile = File(...),
    audio: UploadFile = File(...),
    sensor_payload: Optional[str] = Form(None),
    vad_payload: Optional[str] = Form(None),
) -> JSONResponse:
    """接收设备上传事件，创建记录（queued），启动后台分析。"""
    if not _ISO8601_RE.match(captured_at):
        return JSONResponse(
            status_code=400,
            content={
                "code": 40001,
                "message": "invalid captured_at: must be ISO 8601",
                "data": None,
            },
        )

    image_bytes = await image.read()
    audio_bytes = await audio.read()

    logger.info(
        "event upload received, device_id=%s, event_type=%s, "
        "image=%s (%d bytes), audio=%s (%d bytes)",
        device_id,
        event_type,
        image.filename,
        len(image_bytes),
        audio.filename,
        len(audio_bytes),
    )

    event = event_store.create_event(
        device_id=device_id,
        event_type=event_type,
        trigger_source=trigger_source,
        captured_at=captured_at,
        firmware_version=firmware_version,
        image_filename=image.filename or "unknown",
        image_size=len(image_bytes),
        audio_filename=audio.filename or "unknown",
        audio_size=len(audio_bytes),
        image_bytes=image_bytes,
        audio_bytes=audio_bytes,
        sensor_payload=sensor_payload,
        vad_payload=vad_payload,
    )

    # 启动后台分析（真异步：queued → processing → done/failed）
    analyzer.schedule(event["event_id"])

    return JSONResponse(
        status_code=202,
        content={
            "code": 0,
            "message": "accepted",
            "data": {
                "request_id": event["event_id"],
                "event_id": event["event_id"],
                "status": "queued",
                "received_at": event["received_at"],
                "result_query_path": f"/api/v1/device/events/{event['event_id']}/result",
            },
        },
    )


@router.get("/events/{event_id}/result")
async def query_result(event_id: str) -> JSONResponse:
    """按 event_id 查询分析状态与结果。返回真实当前状态。"""
    event = event_store.get_event(event_id)

    if event is None:
        logger.warning("result query for unknown event_id=%s", event_id)
        return JSONResponse(
            status_code=404,
            content={
                "code": 40401,
                "message": "event not found",
                "data": None,
            },
        )

    logger.info(
        "result query, event_id=%s, status=%s",
        event_id,
        event["status"],
    )

    data = {
        "event_id": event["event_id"],
        "device_id": event["device_id"],
        "status": event["status"],
    }

    if event["status"] == "done":
        data.update({
            "event_type": event["event_type"],
            "trigger_source": event["trigger_source"],
            "risk_level": event["risk_level"],
            "summary": event["summary"],
            "labels": event["labels"],
            "voice_broadcast_text": event["voice_broadcast_text"],
            "voice_broadcast_level": event["voice_broadcast_level"],
            "captured_at": event["captured_at"],
            "finished_at": event["finished_at"],
        })
    elif event["status"] == "failed":
        data["failure_reason"] = event.get("failure_reason", "unknown")

    # done 时标注有 TTS 音频可下载
    if event["status"] == "done" and event.get("tts_audio"):
        data["tts_audio_path"] = f"/api/v1/device/events/{event_id}/tts"

    return JSONResponse(
        status_code=200,
        content={
            "code": 0,
            "message": "ok",
            "data": data,
        },
    )


@router.get("/events/{event_id}/tts")
async def get_tts_audio(event_id: str) -> Response:
    """下载 TTS 播报音频（PCM 16kHz/16-bit/mono）。

    对齐参考固件返回 application/octet-stream PCM 流。
    设备下载后直接通过 I2S 播放。
    """
    event = event_store.get_event(event_id)

    if event is None:
        return JSONResponse(
            status_code=404,
            content={"code": 40401, "message": "event not found", "data": None},
        )

    tts_audio = event.get("tts_audio")
    if not tts_audio:
        return JSONResponse(
            status_code=404,
            content={"code": 40402, "message": "tts audio not ready", "data": None},
        )

    logger.info(
        "tts audio download, event_id=%s, size=%d bytes",
        event_id,
        len(tts_audio),
    )

    return Response(
        content=tts_audio,
        media_type="application/octet-stream",
        headers={"Content-Length": str(len(tts_audio))},
    )
