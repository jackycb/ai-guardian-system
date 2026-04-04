# AI Guardian System - Door Monitor V1

AI 门口看护器 V1 -- 基于 Seeed XIAO ESP32-S3 Sense + 阿里云 DashScope 的智能门口看护系统。

设备检测到触发事件后自动拍照录音，上传云端进行 AI 分析（多模态视觉理解 + 语音识别），分析结果通过 TTS 语音合成后下发到设备播放。

## 系统架构

```
按键/PIR 触发
    |
    v
[ESP32-S3 Sense] --WiFi/HTTP--> [FastAPI Backend on ECS]
  OV2640 拍照                      |
  PDM 麦克风录音                    v
  MAX98357A 播放 <--- PCM ---  [DashScope AI Pipeline]
                                 Paraformer ASR (语音识别)
                                 Qwen-VL-Max (多模态分析)
                                 CosyVoice TTS (语音合成)
```

## 主链路流程

```
IDLE → 按键触发 → 拍照(JPEG) → 录音(WAV 5s) → Health Check(同步服务端时间)
     → 上传(multipart + Bearer Token) → 轮询结果(ArduinoJson 解析)
     → 下载 TTS PCM → I2S 播放 → IDLE
```

## 项目结构

```
ai-guardian-system-health/
├── backend/                    # FastAPI 后端
│   ├── app.py                  # 入口，挂载中间件 + 路由
│   ├── auth.py                 # Bearer Token 鉴权中间件
│   ├── health.py               # GET /api/v1/health
│   ├── events.py               # POST 上传 / GET 查询 / GET TTS 下载
│   ├── event_store.py          # 内存事件存储
│   ├── analyzer.py             # DashScope AI 管线 (ASR + VL + TTS)
│   └── logging_config.py       # 结构化日志
├── firmware/                   # ESP32-S3 固件 (PlatformIO + Arduino)
│   ├── platformio.ini          # dev(HTTP) + prod(HTTPS) 双环境
│   ├── include/
│   │   ├── config.h            # 硬件引脚、API 路径、轮询参数
│   │   ├── secrets.h           # WiFi/服务器/Token (gitignore)
│   │   └── secrets.h.example   # 模板
│   └── src/main.cpp            # 12 状态机 + 完整主链路
├── device/                     # Python 设备模拟层 (测试/CI)
├── tests/                      # 42 个测试用例
├── docs/                       # 需求/架构/API规范/状态机/运维手册
├── scripts/deploy.sh           # ECS 一键部署
└── pyproject.toml
```

## 快速开始

### 1. 后端 (开发模式)

```bash
pip install fastapi uvicorn httpx python-multipart
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

开发模式下无需 DashScope API Key (自动降级为 stub)，无需设备 Token。

### 2. 后端 (生产模式)

```bash
pip install fastapi uvicorn httpx python-multipart dashscope openai
export DASHSCOPE_API_KEY=sk-xxx
export DEVICE_TOKEN=your-device-token
uvicorn backend.app:app --host 0.0.0.0 --port 5000
```

或使用一键部署脚本：

```bash
export ECS_HOST=your-ecs-ip
export ECS_USER=root
export DASHSCOPE_API_KEY=sk-xxx
bash scripts/deploy.sh
```

### 3. 固件烧录

```bash
cd firmware/include
cp secrets.h.example secrets.h
# 编辑 secrets.h 填入 WiFi SSID/密码、服务器地址、Token

# 编译 + 烧录 (开发环境 HTTP)
python3 -m platformio run -d firmware -e xiao_esp32s3 --target upload

# 编译 + 烧录 (生产环境 HTTPS)
python3 -m platformio run -d firmware -e xiao_esp32s3_prod --target upload

# 串口监控
python3 -m platformio device monitor -d firmware
```

### 4. 运行测试

```bash
pip install fastapi httpx pytest python-multipart
python3 -m pytest tests/ -v
```

## API 端点

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET | `/api/v1/health` | 健康检查 + 服务端时间 | 公开 |
| POST | `/api/v1/device/events` | 上传事件 (multipart) | Bearer Token |
| GET | `/api/v1/device/events/{id}/result` | 查询分析结果 | Bearer Token |
| GET | `/api/v1/device/events/{id}/tts` | 下载 TTS PCM 音频 | Bearer Token |

## 硬件

| 模块 | 型号 | 连接 |
|------|------|------|
| 主控 | Seeed XIAO ESP32-S3 Sense | USB-C |
| 摄像头 | OV2640 (板载) | 内部排线 |
| 麦克风 | PDM 数字麦克风 (板载) | CLK=GPIO42, DATA=GPIO41 |
| 功放 | MAX98357A I2S | BCLK=D0, LRC=D4, DIN=D2 |
| 触发 | 按键 (暂替 PIR) | D1 → GND |

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `DASHSCOPE_API_KEY` | (空) | 阿里云 AI 密钥，空时降级 stub |
| `DEVICE_TOKEN` | (空) | 设备鉴权 Token，空时跳过鉴权 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## DashScope AI 管线

| 步骤 | 模型 | 功能 |
|------|------|------|
| ASR | paraformer-realtime-v2 | WAV 音频 → 中文文本 |
| 多模态分析 | qwen-vl-max | 图片 + 文本 → 风险评估 + 建议 |
| TTS | cosyvoice-v1 (longxiaochun) | 分析文本 → PCM 16kHz/16-bit 语音 |

## 状态机

设备端 12 状态：

```
IDLE → TRIGGERED → CAPTURING → READY_TO_UPLOAD → UPLOADING
     → WAITING_RESULT → PLAYING_FEEDBACK → COMPLETE → IDLE
```

失败路径：`CAPTURE_FAILED` / `UPLOAD_FAILED` / `RESULT_TIMEOUT` / `PLAYBACK_FAILED`

后端事件 4 状态：`queued → processing → done / failed`

## 文档

- [docs/requirements.md](docs/requirements.md) - 需求说明
- [docs/architecture.md](docs/architecture.md) - 架构设计
- [docs/api-spec.md](docs/api-spec.md) - API 规范
- [docs/state-machine.md](docs/state-machine.md) - 状态机定义
- [docs/module-map.md](docs/module-map.md) - 模块地图
- [docs/runbook.md](docs/runbook.md) - 运维手册
