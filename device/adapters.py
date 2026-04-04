"""设备端适配器层 —— trigger / capture / feedback 的抽象接口与 stub 实现。

音频格式对齐参考固件 (ai-desktop-device)：
  - 16kHz 采样率, 16-bit PCM, 单声道
  - WAV 封装 (44 字节标准头)
  - 图片：JPEG (OV2640 VGA 640×480)

真实硬件驱动替换 stub 实现即可，上层流程编排代码无需修改。
"""

import abc
import logging
import math
import os
import struct
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("device.adapters")

# ======================================================================
# 音频格式常量 —— 对齐参考固件 config.h
# ======================================================================

AUDIO_SAMPLE_RATE = 16000   # Hz
AUDIO_CHANNELS = 1          # 单声道
AUDIO_BITS = 16             # 16-bit PCM
AUDIO_BYTES_PER_SAMPLE = AUDIO_BITS // 8


# ======================================================================
# WAV 工具 —— 对齐参考固件 buildWavHeader()
# ======================================================================


def build_wav_header(pcm_size: int) -> bytes:
    """构建 44 字节 WAV 文件头，参数对齐参考固件。

    格式：PCM 16-bit, 16kHz, mono
    """
    byte_rate = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * AUDIO_BYTES_PER_SAMPLE
    block_align = AUDIO_CHANNELS * AUDIO_BYTES_PER_SAMPLE
    data_size = pcm_size
    file_size = 36 + data_size  # 不含前 8 字节 RIFF header

    header = struct.pack(
        "<4sI4s"      # RIFF chunk
        "4sIHHIIHH"   # fmt sub-chunk
        "4sI",         # data sub-chunk header
        b"RIFF", file_size, b"WAVE",
        b"fmt ", 16,                          # fmt chunk size = 16 (PCM)
        1,                                     # AudioFormat = 1 (PCM)
        AUDIO_CHANNELS,                        # NumChannels
        AUDIO_SAMPLE_RATE,                     # SampleRate
        byte_rate,                             # ByteRate
        block_align,                           # BlockAlign
        AUDIO_BITS,                            # BitsPerSample
        b"data", data_size,                    # data chunk
    )
    return header


def build_wav(pcm_data: bytes) -> bytes:
    """PCM 字节 → 完整 WAV 文件字节。"""
    return build_wav_header(len(pcm_data)) + pcm_data


def generate_pcm_silence(duration_sec: float = 1.0) -> bytes:
    """生成指定时长的静音 PCM 数据。"""
    num_samples = int(AUDIO_SAMPLE_RATE * duration_sec)
    return b"\x00\x00" * num_samples


def generate_pcm_tone(freq: float = 800, duration_sec: float = 0.5) -> bytes:
    """生成简单正弦波 PCM 数据（对齐参考固件 generate_beep）。"""
    num_samples = int(AUDIO_SAMPLE_RATE * duration_sec)
    pcm = bytearray()
    for i in range(num_samples):
        t = i / AUDIO_SAMPLE_RATE
        value = int(16000 * math.sin(2 * math.pi * freq * t))
        pcm += struct.pack("<h", max(-32768, min(32767, value)))
    return bytes(pcm)


# ======================================================================
# Trigger
# ======================================================================


class TriggerInfo:
    """一次触发事件的描述。"""

    def __init__(
        self,
        trigger_source: str,
        event_type: str,
        captured_at: Optional[str] = None,
        sensor_payload: Optional[dict] = None,
        vad_payload: Optional[dict] = None,
    ) -> None:
        self.trigger_source = trigger_source
        self.event_type = event_type
        self.captured_at = captured_at or datetime.now(timezone.utc).isoformat()
        self.sensor_payload = sensor_payload
        self.vad_payload = vad_payload


class TriggerSource(abc.ABC):
    """触发源抽象接口。

    真实实现：PIR (GPIO 中断) + VAD (音频能量检测)。
    参考固件使用按钮 (GPIO2) 触发，门口看护器 V1 改为 PIR + VAD。
    """

    @abc.abstractmethod
    def wait_for_trigger(self) -> Optional[TriggerInfo]:
        """阻塞等待触发，返回触发信息。返回 None 表示无效触发 / 去抖过滤。"""


class StubTrigger(TriggerSource):
    """Stub 触发源 —— 立即返回一个预设的触发事件。"""

    def __init__(
        self,
        trigger_source: str = "pir",
        event_type: str = "motion",
    ) -> None:
        self._trigger_source = trigger_source
        self._event_type = event_type

    def wait_for_trigger(self) -> Optional[TriggerInfo]:
        info = TriggerInfo(
            trigger_source=self._trigger_source,
            event_type=self._event_type,
            sensor_payload={"pir": True},
        )
        logger.info(
            "[TRIGGER stub] source=%s, type=%s",
            info.trigger_source,
            info.event_type,
        )
        return info


# ======================================================================
# Capture
# ======================================================================


class CaptureResult:
    """一次采集的输出。

    Attributes:
        image_path: JPEG 图片文件路径
        audio_path: WAV 音频文件路径 (PCM 16kHz/16-bit/mono, 44 字节标准头)
    """

    def __init__(self, image_path: str, audio_path: str) -> None:
        self.image_path = image_path
        self.audio_path = audio_path


class CaptureManager(abc.ABC):
    """采集管理器抽象接口。

    真实实现：OV2640 拍照 (丢弃前 3 帧做 AE 校准) + PDM 麦克风录音。
    """

    @abc.abstractmethod
    def capture(self, trigger: TriggerInfo) -> Optional[CaptureResult]:
        """执行拍照 + 录音，返回文件路径。失败返回 None。"""


class StubCapture(CaptureManager):
    """Stub 采集器 —— 生成格式正确的 JPEG + WAV 临时文件。

    WAV 格式对齐参考固件：PCM 16kHz, 16-bit, mono, 标准 44 字节头。
    """

    def __init__(self, audio_duration_sec: float = 1.0) -> None:
        self._audio_duration = audio_duration_sec

    def capture(self, trigger: TriggerInfo) -> Optional[CaptureResult]:
        try:
            # JPEG: 最小合法 JPEG (SOI + EOI)
            img_fd, img_path = tempfile.mkstemp(suffix=".jpg", prefix="cap_")
            os.write(img_fd, b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9")
            os.close(img_fd)

            # WAV: 真实格式，PCM 16kHz/16-bit/mono
            pcm_data = generate_pcm_tone(freq=440, duration_sec=self._audio_duration)
            wav_data = build_wav(pcm_data)
            aud_fd, aud_path = tempfile.mkstemp(suffix=".wav", prefix="cap_")
            os.write(aud_fd, wav_data)
            os.close(aud_fd)

            logger.info(
                "[CAPTURE stub] image=%s (%d bytes), audio=%s (%d bytes, %.1fs)",
                img_path, os.path.getsize(img_path),
                aud_path, len(wav_data), self._audio_duration,
            )
            return CaptureResult(image_path=img_path, audio_path=aud_path)
        except OSError as exc:
            logger.error("[CAPTURE stub] failed: %s", exc)
            return None


# ======================================================================
# Feedback
# ======================================================================


class FeedbackPlayer(abc.ABC):
    """反馈播放器抽象接口。

    真实实现：将 voice_broadcast_text 做本地 TTS 或从后端获取 PCM，
    通过 I2S + MAX98357A + 喇叭播放。
    参考固件参数：I2S_NUM_1, BCLK=GPIO1, LRC=GPIO5, DIN=GPIO3, 16kHz/16-bit/mono。
    """

    @abc.abstractmethod
    def play(self, result: Dict[str, Any]) -> bool:
        """播放分析结果反馈。返回 True 表示播放成功。"""


class LogFeedback(FeedbackPlayer):
    """Stub 播放器 —— 日志输出播报文本。"""

    def play(self, result: Dict[str, Any]) -> bool:
        status = result.get("status", "unknown")
        if status == "done":
            text = result.get("voice_broadcast_text", "")
            level = result.get("voice_broadcast_level", "info")
            logger.info(
                "[FEEDBACK] broadcast_level=%s, text=%s", level, text
            )
            return True
        elif status == "failed":
            reason = result.get("failure_reason", "unknown")
            logger.warning("[FEEDBACK] analysis failed, reason=%s", reason)
            return True
        else:
            logger.warning("[FEEDBACK] unexpected status=%s", status)
            return False
