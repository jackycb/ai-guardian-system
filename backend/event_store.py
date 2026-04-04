"""内存事件存储。

事件创建时状态为 queued，由 analyzer 驱动后续状态流转。
存储后端后续可替换为数据库，但状态语义不变。
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backend.event_store")

_events: Dict[str, Dict[str, Any]] = {}


def create_event(
    device_id: str,
    event_type: str,
    trigger_source: str,
    captured_at: str,
    firmware_version: str,
    image_filename: str,
    image_size: int,
    audio_filename: str,
    audio_size: int,
    image_bytes: Optional[bytes] = None,
    audio_bytes: Optional[bytes] = None,
    sensor_payload: Optional[str] = None,
    vad_payload: Optional[str] = None,
) -> Dict[str, Any]:
    """创建事件，初始状态为 queued。

    image_bytes / audio_bytes 供 analyzer 做 AI 分析使用，
    分析完成后由 analyzer 清理以释放内存。
    """
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    received_at = datetime.now(timezone.utc).isoformat()

    event = {
        "event_id": event_id,
        "device_id": device_id,
        "event_type": event_type,
        "trigger_source": trigger_source,
        "captured_at": captured_at,
        "firmware_version": firmware_version,
        "received_at": received_at,
        "image_filename": image_filename,
        "image_size": image_size,
        "audio_filename": audio_filename,
        "audio_size": audio_size,
        "image_bytes": image_bytes,
        "audio_bytes": audio_bytes,
        "sensor_payload": sensor_payload,
        "vad_payload": vad_payload,
        "status": "queued",
    }

    _events[event_id] = event

    logger.info(
        "event created, event_id=%s, device_id=%s, type=%s, status=queued",
        event_id,
        device_id,
        event_type,
    )
    return event


def update_status(event_id: str, status: str, **extra: Any) -> bool:
    """更新事件状态及附加字段。返回 False 表示 event_id 不存在。"""
    event = _events.get(event_id)
    if event is None:
        return False
    old = event["status"]
    event["status"] = status
    event.update(extra)
    logger.info(
        "event status updated, event_id=%s, %s -> %s",
        event_id,
        old,
        status,
    )
    return True


def get_event(event_id: str) -> Optional[Dict[str, Any]]:
    """按 event_id 查询事件。"""
    return _events.get(event_id)


def list_by_status(status: str) -> List[Dict[str, Any]]:
    """按状态列出事件（分析调度用）。"""
    return [e for e in _events.values() if e["status"] == status]


def clear() -> None:
    """清空所有事件（测试用）。"""
    _events.clear()
