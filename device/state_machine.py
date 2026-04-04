"""设备端状态机 —— 对齐 docs/state-machine.md 主链路。

主路径：
    IDLE → TRIGGERED → CAPTURING → READY_TO_UPLOAD → UPLOADING
         → WAITING_RESULT → PLAYING_FEEDBACK → COMPLETE → IDLE

失败路径：
    CAPTURING       → CAPTURE_FAILED   → IDLE
    READY_TO_UPLOAD → UPLOAD_FAILED    → IDLE  (health check 失败)
    UPLOADING       → UPLOAD_FAILED    → IDLE
    WAITING_RESULT  → RESULT_TIMEOUT   → IDLE
    PLAYING_FEEDBACK→ PLAYBACK_FAILED  → COMPLETE → IDLE
"""

import enum
import logging
from typing import Callable, Dict, List

logger = logging.getLogger("device.state_machine")


class State(enum.Enum):
    IDLE = "idle"
    TRIGGERED = "triggered"
    CAPTURING = "capturing"
    CAPTURE_FAILED = "capture_failed"
    READY_TO_UPLOAD = "ready_to_upload"
    UPLOADING = "uploading"
    UPLOAD_FAILED = "upload_failed"
    WAITING_RESULT = "waiting_result"
    RESULT_TIMEOUT = "result_timeout"
    PLAYING_FEEDBACK = "playing_feedback"
    PLAYBACK_FAILED = "playback_failed"
    COMPLETE = "complete"


class TransitionError(Exception):
    pass


_TRANSITIONS: Dict[State, List[State]] = {
    State.IDLE: [State.TRIGGERED],
    State.TRIGGERED: [State.CAPTURING, State.IDLE],
    State.CAPTURING: [State.READY_TO_UPLOAD, State.CAPTURE_FAILED],
    State.CAPTURE_FAILED: [State.IDLE],
    State.READY_TO_UPLOAD: [State.UPLOADING, State.UPLOAD_FAILED],
    State.UPLOADING: [State.WAITING_RESULT, State.UPLOAD_FAILED],
    State.UPLOAD_FAILED: [State.IDLE],
    State.WAITING_RESULT: [State.PLAYING_FEEDBACK, State.RESULT_TIMEOUT, State.COMPLETE],
    State.RESULT_TIMEOUT: [State.IDLE],
    State.PLAYING_FEEDBACK: [State.COMPLETE, State.PLAYBACK_FAILED],
    State.PLAYBACK_FAILED: [State.COMPLETE],
    State.COMPLETE: [State.IDLE],
}


class DeviceStateMachine:
    """设备端状态机，管理单次事件处理流程中的状态流转。"""

    def __init__(self) -> None:
        self._state = State.IDLE
        self._history: List[str] = []
        self._listeners: List[Callable[[State, State], None]] = []

    @property
    def state(self) -> State:
        return self._state

    @property
    def history(self) -> List[str]:
        return list(self._history)

    def on_transition(self, listener: Callable[[State, State], None]) -> None:
        self._listeners.append(listener)

    def transition_to(self, target: State, reason: str = "") -> None:
        allowed = _TRANSITIONS.get(self._state, [])
        if target not in allowed:
            raise TransitionError(
                f"illegal transition {self._state.value} -> {target.value}, "
                f"allowed: {[s.value for s in allowed]}"
            )

        old = self._state
        self._state = target
        entry = f"{old.value} -> {target.value}"
        if reason:
            entry += f" ({reason})"
        self._history.append(entry)

        logger.info("state: %s", entry)

        for fn in self._listeners:
            fn(old, target)

    def reset(self) -> None:
        self._state = State.IDLE
        self._history.clear()
