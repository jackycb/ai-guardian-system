"""事件分析器 —— 驱动 queued → processing → done/failed 状态流转。

AI 管线对齐参考固件 (ai-desktop-device/server/app.py)：
  1. Paraformer ASR：WAV 音频 → 文本
  2. Qwen-VL 多模态分析：图片 + 文本 → AI 分析结果
  3. CosyVoice TTS：分析结果 → 播报文本（本系统只保存文本，不合成音频）

当 DASHSCOPE_API_KEY 未配置时自动降级为 stub 模式（硬编码结果）。
"""

import base64
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from backend import event_store

logger = logging.getLogger("backend.analyzer")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DASHSCOPE_API_KEY: str = os.environ.get("DASHSCOPE_API_KEY", "")

# AI 模型（对齐参考固件）
QWEN_VL_MODEL = "qwen-vl-max"
ASR_MODEL = "paraformer-realtime-v2"
TTS_MODEL = "cosyvoice-v1"
TTS_VOICE = "longxiaochun"

# 模拟分析耗时（秒），仅 stub 模式使用，可通过测试覆盖
ANALYSIS_DELAY: float = 0.5


# ---------------------------------------------------------------------------
# ASR：Paraformer 语音识别
# ---------------------------------------------------------------------------

def transcribe_audio(audio_bytes: bytes) -> str:
    """WAV 音频 → 文本。对齐参考固件 transcribe_audio()。

    优先使用 DashScope Recognition API，失败则降级 OpenAI 兼容接口。
    """
    if len(audio_bytes) <= 44:
        return ""

    logger.info("[ASR] start, audio_size=%d bytes", len(audio_bytes))

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        temp_path = f.name

    try:
        from dashscope.audio.asr import Recognition

        recognition = Recognition(
            model=ASR_MODEL,
            format="wav",
            sample_rate=16000,
            callback=None,
        )
        result = recognition.call(temp_path)

        if result.status_code == 200:
            sentences = result.get_sentence()
            if sentences:
                text = "".join([s.get("text", "") for s in sentences])
            else:
                text = (
                    result.output.get("text", "")
                    if hasattr(result, "output")
                    else ""
                )
            logger.info("[ASR] result: %s", text)
            return text if text else ""
        else:
            logger.error(
                "[ASR] failed: %s - %s", result.status_code, result.message
            )
            return _transcribe_audio_openai(temp_path)

    except Exception as exc:
        logger.error("[ASR] exception: %s, trying OpenAI compat", exc)
        return _transcribe_audio_openai(temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _transcribe_audio_openai(file_path: str) -> str:
    """备用：OpenAI 兼容的 whisper 接口（对齐参考固件）。"""
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=DASHSCOPE_API_KEY,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        with open(file_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="paraformer-v2",
                file=audio_file,
                language="zh",
            )
        text = transcript.text
        logger.info("[ASR-OpenAI] result: %s", text)
        return text if text else ""
    except Exception as exc:
        logger.error("[ASR-OpenAI] failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# 多模态分析：Qwen-VL
# ---------------------------------------------------------------------------

def analyze_with_ai(image_bytes: bytes, user_text: str, event_type: str) -> str:
    """图片 + 文本 → AI 分析回复。对齐参考固件 analyze_with_ai()。

    针对门口看护场景定制 prompt（不同于桌面助手的通用 prompt）。
    """
    logger.info(
        "[AI] start, image_size=%d bytes, text=%s",
        len(image_bytes),
        user_text[:50] if user_text else "(empty)",
    )

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    if user_text and user_text.strip():
        prompt = (
            f"门口摄像头画面中检测到{event_type}事件。"
            f"现场有声音，语音识别结果为：「{user_text}」\n"
            "请根据画面和声音综合分析：1)来人身份推测 2)风险等级(low/medium/high) "
            "3)建议行动。回复简洁，控制在100字以内，适合语音播报。"
        )
    else:
        prompt = (
            f"门口摄像头画面中检测到{event_type}事件。"
            "请分析画面内容：1)来人身份推测 2)风险等级(low/medium/high) "
            "3)建议行动。回复简洁，控制在100字以内，适合语音播报。"
        )

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=DASHSCOPE_API_KEY,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        response = client.chat.completions.create(
            model=QWEN_VL_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个门口智能看护助手，能看到门口摄像头的画面。"
                        "请用简洁、友好的中文回答。回答会被语音合成播放，"
                        "所以不要使用特殊符号、markdown格式或表情。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}",
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                },
            ],
            max_tokens=300,
        )

        reply = response.choices[0].message.content
        logger.info("[AI] reply: %s", reply)
        return reply

    except Exception as exc:
        logger.error("[AI] failed: %s", exc)
        return f"门口检测到{event_type}事件，AI 分析暂不可用，请留意。"


# ---------------------------------------------------------------------------
# 风险等级提取
# ---------------------------------------------------------------------------

def _extract_risk_level(ai_reply: str) -> str:
    """从 AI 回复中提取风险等级关键词。"""
    text_lower = ai_reply.lower()
    if "high" in text_lower or "高风险" in ai_reply:
        return "high"
    if "medium" in text_lower or "中风险" in ai_reply:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# TTS：CosyVoice 语音合成
# ---------------------------------------------------------------------------

def text_to_speech_pcm(text: str) -> bytes:
    """文本 → PCM 16kHz/16-bit/mono。对齐参考固件 text_to_speech_pcm()。

    失败时返回 800Hz 提示音作为 fallback。
    """
    logger.info("[TTS] start, text=%s", text[:50])

    try:
        from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat

        synthesizer = SpeechSynthesizer(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            format=AudioFormat.PCM_16000HZ_MONO_16BIT,
        )
        audio = synthesizer.call(text)

        if audio is not None and len(audio) > 0:
            logger.info("[TTS] done, %d bytes", len(audio))
            return bytes(audio)
        else:
            logger.error("[TTS] returned empty data")
            return _generate_fallback_beep()

    except Exception as exc:
        logger.error("[TTS] failed: %s", exc)
        return _generate_fallback_beep()


def _generate_fallback_beep() -> bytes:
    """TTS 失败时的备用提示音（800Hz, 500ms）。对齐参考固件 generate_beep()。"""
    import math
    import struct
    num_samples = 8000  # 500ms at 16kHz
    pcm = bytearray()
    for i in range(num_samples):
        t = i / 16000.0
        value = int(16000 * math.sin(2 * math.pi * 800 * t))
        pcm += struct.pack("<h", max(-32768, min(32767, value)))
    return bytes(pcm)


# ---------------------------------------------------------------------------
# 主分析函数
# ---------------------------------------------------------------------------

def analyze(event_id: str) -> None:
    """对单个事件执行分析，更新状态为 processing → done/failed。

    此函数设计为在后台线程中调用，不应在请求处理线程中同步执行。

    当 DASHSCOPE_API_KEY 配置时走真实 AI 管线；
    否则降级为 stub（固定延迟 + 硬编码结果）。
    """
    event = event_store.get_event(event_id)
    if event is None:
        logger.error("analyze: event not found, event_id=%s", event_id)
        return

    # queued → processing
    event_store.update_status(event_id, "processing")

    use_ai = bool(DASHSCOPE_API_KEY)

    if not use_ai:
        # ----- stub 模式 -----
        _analyze_stub(event_id, event)
        return

    # ----- DashScope AI 管线 -----
    try:
        image_bytes = event.get("image_bytes", b"") or b""
        audio_bytes = event.get("audio_bytes", b"") or b""
        event_type = event.get("event_type", "unknown")
        trigger_source = event.get("trigger_source", "unknown")

        # 1) ASR
        user_text = ""
        if audio_bytes and len(audio_bytes) > 44:
            user_text = transcribe_audio(audio_bytes)

        # 2) Qwen-VL 多模态分析
        ai_reply = analyze_with_ai(image_bytes, user_text, event_type)
        risk_level = _extract_risk_level(ai_reply)

        # 3) CosyVoice TTS：生成播报 PCM 音频
        tts_pcm = text_to_speech_pcm(ai_reply)

        broadcast_level = (
            "warning" if risk_level in ("medium", "high") else "info"
        )

        finished_at = datetime.now(timezone.utc).isoformat()

        event_store.update_status(
            event_id,
            "done",
            risk_level=risk_level,
            summary=ai_reply,
            labels=[event_type, trigger_source],
            voice_broadcast_text=ai_reply,
            voice_broadcast_level=broadcast_level,
            tts_audio=tts_pcm,
            finished_at=finished_at,
        )
        logger.info("analyze done (AI), event_id=%s, risk=%s", event_id, risk_level)

    except Exception as exc:
        logger.error("analyze failed, event_id=%s, error=%s", event_id, exc)
        event_store.update_status(
            event_id,
            "failed",
            failure_reason=str(exc),
        )
    finally:
        # 释放内存中的二进制数据
        _release_binary(event_id)


def _analyze_stub(event_id: str, event: dict) -> None:
    """Stub 分析 —— DASHSCOPE_API_KEY 未配置时使用。"""
    time.sleep(ANALYSIS_DELAY)

    try:
        finished_at = datetime.now(timezone.utc).isoformat()
        event_type = event.get("event_type", "unknown")
        trigger_source = event.get("trigger_source", "unknown")

        broadcast_text = f"门口检测到{event_type}事件，目前判断为低风险，请留意。"
        event_store.update_status(
            event_id,
            "done",
            risk_level="low",
            summary=f"检测到门口事件（{event_type}），来源：{trigger_source}，暂无异常。",
            labels=[event_type, trigger_source],
            voice_broadcast_text=broadcast_text,
            voice_broadcast_level="info",
            tts_audio=_generate_fallback_beep(),
            finished_at=finished_at,
        )
        logger.info("analyze done (stub), event_id=%s", event_id)

    except Exception as exc:
        logger.error("analyze failed, event_id=%s, error=%s", event_id, exc)
        event_store.update_status(
            event_id,
            "failed",
            failure_reason=str(exc),
        )
    finally:
        _release_binary(event_id)


def _release_binary(event_id: str) -> None:
    """分析完成后释放内存中的上传二进制数据（保留 tts_audio 供设备下载）。"""
    event = event_store.get_event(event_id)
    if event is not None:
        event.pop("image_bytes", None)
        event.pop("audio_bytes", None)


def schedule(event_id: str) -> threading.Thread:
    """在后台线程中启动分析任务。返回线程对象（便于测试 join）。"""
    t = threading.Thread(target=analyze, args=(event_id,), daemon=True)
    t.start()
    logger.info("analysis scheduled, event_id=%s, thread=%s", event_id, t.name)
    return t
