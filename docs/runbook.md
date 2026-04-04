# Runbook — AI Guardian System

## 1. 启动后端服务

### 开发模式（无鉴权，stub AI）

```bash
pip install fastapi uvicorn httpx python-multipart
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

### 生产模式（鉴权 + DashScope AI）

```bash
pip install fastapi uvicorn httpx python-multipart dashscope openai
export DASHSCOPE_API_KEY=sk-xxx
export DEVICE_TOKEN=guardian-dev-token
uvicorn backend.app:app --host 0.0.0.0 --port 5000
```

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `DASHSCOPE_API_KEY` | (空) | 阿里云 AI 密钥，空时降级为 stub |
| `DEVICE_TOKEN` | (空) | 设备鉴权 Token，空时跳过鉴权 |

### 一键部署到 ECS

```bash
export ECS_HOST=8.130.183.132
export ECS_USER=root
export DASHSCOPE_API_KEY=sk-xxx
bash scripts/deploy.sh
```

## 2. 固件烧录（firmware/ — 真实硬件）

### 环境

- PlatformIO CLI
- Seeed XIAO ESP32-S3 Sense + MAX98357A

### 配置

```bash
cd firmware/include
cp secrets.h.example secrets.h
# 编辑 secrets.h 填入 WiFi、服务器、Token
```

### 编译烧录

```bash
python3 -m platformio run -d firmware                # 编译
python3 -m platformio run -d firmware --target upload # 烧录
python3 -m platformio device monitor -d firmware      # 串口监控
```

## 3. 验证 Health Check

```bash
curl -s http://localhost:8000/api/v1/health | python3 -m json.tool
```

## 4. 手动上传事件

```bash
# 开发模式（无鉴权）
curl -X POST http://localhost:8000/api/v1/device/events \
  -F "device_id=xiao-door-001" \
  -F "event_type=visitor_detected" \
  -F "trigger_source=button" \
  -F "captured_at=2026-04-04T13:18:00+08:00" \
  -F "firmware_version=1.0.0" \
  -F "image=@frame.jpg" \
  -F "audio=@audio.wav" \
  | python3 -m json.tool

# 生产模式（带 Token）
curl -X POST http://localhost:5000/api/v1/device/events \
  -H "Authorization: Bearer guardian-dev-token" \
  -F "device_id=xiao-door-001" \
  -F "event_type=visitor_detected" \
  -F "trigger_source=button" \
  -F "captured_at=2026-04-04T13:18:00+08:00" \
  -F "firmware_version=1.0.0" \
  -F "image=@frame.jpg" \
  -F "audio=@audio.wav" \
  | python3 -m json.tool
```

返回 202 + `event_id`。事件初始状态为 `queued`，后台线程自动推进到 `done`。

## 5. 查询分析结果

```bash
# 上传后立即查 → queued 或 processing
curl -s http://localhost:8000/api/v1/device/events/<event_id>/result

# 约 0.5 秒后再查 → done + 分析结果
curl -s http://localhost:8000/api/v1/device/events/<event_id>/result | python3 -m json.tool
```

## 6. 下载 TTS 音频

```bash
curl -s http://localhost:8000/api/v1/device/events/<event_id>/tts -o tts.pcm
# PCM 格式：16kHz / 16-bit / mono
```

## 7. 运行测试

```bash
pip install fastapi httpx pytest python-multipart
python3 -m pytest tests/ -v
```

## 8. 故障排查

### 8.1 设备端 "health check timeout / connection refused"

1. 确认后端在运行
2. 检查网络连通性和端口
3. 调整 `HEALTH_TIMEOUT_MS`（firmware config.h）

### 8.2 上传返回 401 / 403

- 检查 firmware `secrets.h` 的 `DEVICE_TOKEN` 是否与后端 `DEVICE_TOKEN` 环境变量一致
- 开发模式下后端未设置 `DEVICE_TOKEN` 时自动跳过鉴权

### 8.3 上传返回 422

- 检查必填字段和文件是否齐全
- 确认已安装 `python-multipart`

### 8.4 查询结果一直是 queued / processing

- 检查后端日志中是否有 `analysis scheduled` 和 `analyze done`
- 后台线程可能未启动或抛出异常
- stub 模式默认延迟 0.5 秒

### 8.5 TTS 播放无声音

- 确认 I2S 接线：BCLK→D0, LRC→D4, DIN→D2
- 串口日志应显示 `[TTS] playback done: N bytes`
- 如果 N 很小（<1000），可能 TTS 合成失败，检查后端日志

### 8.6 设备端 "poll timeout"

- 后端分析是否完成（查日志）
- AI 模式下分析耗时较长（5-15 秒），轮询默认最多 30 秒

## 9. 状态机

设备端 12 个状态，主路径：

```
IDLE → TRIGGERED → CAPTURING → READY_TO_UPLOAD → UPLOADING
     → WAITING_RESULT → PLAYING_FEEDBACK → COMPLETE → IDLE
```

失败路径：`CAPTURE_FAILED`、`UPLOAD_FAILED`、`RESULT_TIMEOUT`、`PLAYBACK_FAILED`

后端事件 4 个状态：`queued → processing → done / failed`

## 10. 当前实现状态

| 组件 | 状态 | 说明 |
|------|------|------|
| AI 分析 | 已实现 | DashScope Paraformer + Qwen-VL + CosyVoice |
| 触发源 | 按键暂替 | 未来替换为 PIR + VAD |
| 采集器 | 已实现 | OV2640 JPEG + PDM WAV |
| 播放器 | 已实现 | MAX98357A I2S，TTS PCM 播放 |
| JSON 解析 | 已实现 | ArduinoJson v7 |
| 鉴权 | 已实现 | Bearer Token (可选) |
| 文件存储 | 不持久化 | 分析后释放二进制 |
| 事件存储 | 内存 dict | 待替换数据库 |
