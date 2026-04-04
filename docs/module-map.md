# Module Map — AI Guardian System

```
ai-guardian-system-health/
├── backend/
│   ├── __init__.py
│   ├── app.py              # FastAPI 入口，挂载中间件 + 路由
│   ├── auth.py             # 设备 Token 鉴权中间件 (Bearer Token)
│   ├── health.py           # GET /api/v1/health
│   ├── events.py           # POST /api/v1/device/events (上传)
│   │                       # GET  /api/v1/device/events/{id}/result (查询)
│   │                       # GET  /api/v1/device/events/{id}/tts (TTS PCM 下载)
│   ├── event_store.py      # 内存事件存储，状态流转 (queued/processing/done/failed)
│   ├── analyzer.py         # DashScope AI 管线 (Paraformer ASR + Qwen-VL + CosyVoice TTS)
│   │                       # DASHSCOPE_API_KEY 未配置时降级为 stub
│   └── logging_config.py   # 结构化 JSON 日志
├── firmware/               # ESP32-S3 真实固件（PlatformIO + Arduino）
│   ├── platformio.ini      # 编译配置，依赖 esp32-camera + ArduinoJson
│   ├── include/
│   │   ├── config.h        # 硬件引脚、API 路径、轮询参数（不含密钥）
│   │   ├── secrets.h       # WiFi/服务器/Token 私密配置（.gitignore）
│   │   └── secrets.h.example
│   └── src/
│       └── main.cpp        # 12 状态机 + 完整主链路（按键触发 → TTS 播放）
├── device/                 # Python 设备模拟层（测试/CI 用）
│   ├── __init__.py
│   ├── client.py           # GuardianClient: run_once 主流程编排
│   ├── state_machine.py    # 12 状态设备端状态机
│   └── adapters.py         # 抽象接口 + stub 实现
├── tests/
│   ├── __init__.py
│   ├── test_health.py      # health 端点 + 设备端连通性 (7 cases)
│   ├── test_events.py      # 真实状态流转 + 上传/查询/TTS + 鉴权 (17+ cases)
│   └── test_main_flow.py   # 设备端完整主链路 (6 cases)
├── docs/
│   ├── requirements.md
│   ├── architecture.md
│   ├── api-spec.md
│   ├── state-machine.md
│   ├── test-plan.md
│   ├── module-map.md       # 本文件
│   └── runbook.md
├── scripts/
│   └── deploy.sh           # ECS 一键部署（systemd）
└── pyproject.toml
```

## 两条设备链路

### 1. firmware/ — 真实硬件链路（生产）

```
按键按下 (D1 → LOW)
  │
  ├─ [TRIGGERED]
  │
  ▼
OV2640 拍照 (JPEG VGA) + PDM 麦克风录音 (WAV 16kHz/16-bit 5 秒)
  │
  ├─ [CAPTURING] → [READY_TO_UPLOAD]
  │
  ▼
HTTP GET /api/v1/health (ArduinoJson 解析，同步服务端时间)
  │
  ├─ [UPLOADING]
  │
  ▼
HTTP POST /api/v1/device/events (multipart, Bearer Token)
  │   → event_store.create_event() (queued)
  │   → analyzer.schedule() (后台线程)
  │        → Paraformer ASR → Qwen-VL 分析 → CosyVoice TTS
  │
  ├─ [WAITING_RESULT]
  │
  ▼
HTTP GET .../result (ArduinoJson 解析 status/tts_audio_path)
  │
  ├─ [PLAYING_FEEDBACK]
  │
  ▼
HTTP GET .../tts → PCM 下载 → I2S MAX98357A 播放
  │
  ├─ [COMPLETE] → [IDLE]
```

失败路径：`CAPTURE_FAILED`、`UPLOAD_FAILED`、`RESULT_TIMEOUT`、`PLAYBACK_FAILED`

### 2. device/ — Python 模拟链路（测试/CI）

```
StubTrigger.wait_for_trigger() → StubCapture.capture() → HTTP 上传 → 轮询 → LogFeedback.play()
```

模拟链路使用 stub 适配器，不依赖硬件，用于自动化测试。

## 适配器替换点

| 接口 | Python stub | firmware 真实实现 |
|------|------------|------------------|
| 触发源 | `StubTrigger` 立即返回 | 按键 D1 (未来 PIR + VAD) |
| 采集器 | `StubCapture` 生成假文件 | OV2640 + PDM 麦克风 |
| 播放器 | `LogFeedback` 日志输出 | MAX98357A I2S 功放 |
| AI 分析 | `analyzer` stub 硬编码 | DashScope ASR + VL + TTS |
| 鉴权 | 无 | Bearer Token (DEVICE_TOKEN) |
| 事件存储 | 内存 dict | 内存 dict (待替换数据库) |
| JSON 解析 | Python 原生 | ArduinoJson v7 |
