# AI 看护系统门口看护器 V1 API 规范

## 1. 说明

本文定义门口看护器 V1 的最小必要接口，覆盖以下能力：

- 设备健康检查
- 设备上传事件
- 设备查询事件分析结果
- 设备根据分析结果执行语音播报

V1 默认采用“PIR 或 VAD 触发 -> 设备拍照/录音 -> 设备上传事件 -> 云端异步分析 -> 设备轮询结果 -> 设备语音播报”的交互模式。若后续引入消息推送、MQTT 或回调机制，需另行扩展，当前标记为“待确认”。

## 2. 通用约定

### 2.1 基础信息

- 协议：HTTPS
- 数据格式：`application/json`
- 文件上传：`multipart/form-data`
- 版本前缀：`/api/v1`
- 字符编码：UTF-8
- 时间格式：ISO 8601，例如 `2026-04-04T13:20:00+08:00`

### 2.2 鉴权

V1 设备请求需在 Header 中携带：

```
Authorization: Bearer <device_token>
```

鉴权失败统一返回 `401` + `code: 40101`（无论 Token 缺失或错误）。

当服务端未配置 `DEVICE_TOKEN` 环境变量时，鉴权自动跳过（开发模式）。
`/api/v1/health` 为公开端点，无需鉴权。

### 2.3 通用响应结构

成功响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

失败响应：

```json
{
  "code": 40001,
  "message": "invalid request",
  "data": null
}
```

### 2.4 状态码约定

- `200 OK`：请求成功
- `202 Accepted`：请求已接收，异步处理中
- `400 Bad Request`：请求参数错误
- `401 Unauthorized`：鉴权失败
- `404 Not Found`：资源不存在
- `413 Payload Too Large`：上传内容超限
- `415 Unsupported Media Type`：文件类型不支持
- `429 Too Many Requests`：请求过于频繁
- `500 Internal Server Error`：服务内部错误

### 2.5 通用字段定义

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `request_id` | string | 请求追踪 ID，由服务端生成或透传 |
| `device_id` | string | 设备唯一标识 |
| `event_id` | string | 事件唯一标识，由服务端生成 |
| `trigger_source` | string | 触发源，如 `pir`、`vad`、`pir+vad` |
| `captured_at` | string | 事件采集时间 |
| `received_at` | string | 服务端接收时间 |
| `status` | string | 处理状态 |

## 3. 健康检查接口

### 3.1 接口定义

- 方法：`GET`
- 路径：`/api/v1/health`

### 3.2 用途

- 设备启动后检查云端是否可达
- 运维或测试环境验证服务是否在线

### 3.3 请求头

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `Authorization` | 否 | 若环境要求鉴权则必填，待确认 |
| `X-Device-Id` | 否 | 设备 ID，可用于链路追踪 |

### 3.4 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "service": "ai-guardian-backend",
    "status": "up",
    "time": "2026-04-04T13:20:00+08:00",
    "version": "v1"
  }
}
```

## 4. 设备上传事件接口

### 4.1 接口定义

- 方法：`POST`
- 路径：`/api/v1/device/events`
- Content-Type：`multipart/form-data`

### 4.2 用途

设备向云端上传一次门口事件，包括元数据与附件。事件通常由 PIR 红外感应、语音 VAD，或二者组合触发。服务端接收后创建事件记录，并异步触发分析流程。

### 4.3 表单字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `device_id` | text | 是 | 设备唯一标识 |
| `event_type` | text | 是 | 事件类型，如 `motion`、`voice_detected`、`visitor_detected`、`sensor_trigger` |
| `trigger_source` | text | 是 | 触发来源，如 `pir`、`vad`、`pir+vad` |
| `captured_at` | text | 是 | 设备采集时间，ISO 8601 |
| `firmware_version` | text | 是 | 固件版本 |
| `sequence_id` | text | 否 | 设备本地事件序号，建议用于幂等，待确认 |
| `sensor_payload` | text | 否 | JSON 字符串，包含传感器数据，如 PIR 状态 |
| `vad_payload` | text | 否 | JSON 字符串，包含 VAD 触发信息，如能量值、持续时长 |
| `image` | file | 是 | 主图像文件，V1 默认至少 1 张 |
| `audio` | file | 是 | 触发后录制的短音频片段 |

### 4.4 文件要求

| 字段 | 支持类型 | 大小限制 |
| --- | --- | --- |
| `image` | `image/jpeg`、`image/png` | 待确认 |
| `audio` | `audio/wav`、`audio/mpeg` | 待确认 |

文件尺寸、录音时长、采样率、图像分辨率上限：待确认。

### 4.5 请求示例

```bash
curl -X POST "https://example.com/api/v1/device/events" \
  -H "Authorization: Bearer <device_token>" \
  -F "device_id=xiao-door-001" \
  -F "event_type=visitor_detected" \
  -F "trigger_source=pir+vad" \
  -F "captured_at=2026-04-04T13:18:00+08:00" \
  -F "firmware_version=1.0.0" \
  -F "sequence_id=1024" \
  -F 'sensor_payload={"pir":true}' \
  -F 'vad_payload={"vad_triggered":true,"duration_ms":1800}' \
  -F "image=@/path/to/frame.jpg" \
  -F "audio=@/path/to/audio.wav"
```

### 4.6 成功响应示例

返回 `202 Accepted`：

```json
{
  "code": 0,
  "message": "accepted",
  "data": {
    "request_id": "req_202604041320001",
    "event_id": "evt_20260404131800001",
    "status": "queued",
    "received_at": "2026-04-04T13:20:00+08:00",
    "result_query_path": "/api/v1/device/events/evt_20260404131800001/result"
  }
}
```

### 4.7 失败示例

缺少必填字段：

```json
{
  "code": 40001,
  "message": "missing required field: audio",
  "data": null
}
```

鉴权失败：

```json
{
  "code": 40101,
  "message": "device authentication failed",
  "data": null
}
```

## 5. 结果返回接口

### 5.1 接口定义

- 方法：`GET`
- 路径：`/api/v1/device/events/{event_id}/result`

### 5.2 用途

设备或上层系统根据 `event_id` 查询事件分析状态与结果。

### 5.3 路径参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `event_id` | string | 是 | 上传事件后返回的唯一事件 ID |

### 5.4 查询参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `device_id` | string | 否 | 建议传入，用于校验设备归属 |

### 5.5 状态定义

| 状态 | 说明 |
| --- | --- |
| `queued` | 已接收，等待分析 |
| `processing` | 分析中 |
| `done` | 分析完成 |
| `failed` | 分析失败 |
| `expired` | 结果已过期，待确认是否需要 |

### 5.6 成功响应示例

分析完成：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "event_id": "evt_20260404131800001",
    "device_id": "xiao-door-001",
    "status": "done",
    "event_type": "visitor_detected",
    "trigger_source": "pir+vad",
    "risk_level": "medium",
    "summary": "检测到门口有人停留并伴随语音活动，请留意。",
    "labels": ["person", "doorway", "voice_detected"],
    "voice_broadcast_text": "门口检测到有人停留，请及时查看。",
    "voice_broadcast_level": "warning",
    "captured_at": "2026-04-04T13:18:00+08:00",
    "finished_at": "2026-04-04T13:20:08+08:00"
  }
}
```

仍在处理中：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "event_id": "evt_20260404131800001",
    "device_id": "xiao-door-001",
    "status": "processing"
  }
}
```

### 5.7 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `event_type` | string | 原始事件类型 |
| `trigger_source` | string | 触发来源 |
| `risk_level` | string | 风险等级，建议值为 `low`、`medium`、`high`，最终分级待确认 |
| `summary` | string | 云端分析摘要 |
| `labels` | array<string> | 事件标签 |
| `voice_broadcast_text` | string | 建议设备播报的语音文本 |
| `voice_broadcast_level` | string | 播报级别，如 `info`、`warning`、`alert`，待确认 |
| `finished_at` | string | 分析完成时间 |
| `failure_reason` | string | 分析失败原因，仅失败时返回 |

### 5.8 失败响应示例

事件不存在：

```json
{
  "code": 40401,
  "message": "event not found",
  "data": null
}
```

分析失败：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "event_id": "evt_20260404131800001",
    "status": "failed",
    "failure_reason": "analysis timeout"
  }
}
```

## 6. TTS 音频下载接口

### 6.1 接口定义

- 方法：`GET`
- 路径：`/api/v1/device/events/{event_id}/tts`

### 6.2 用途

设备在轮询结果得到 `status: done` 后，若响应中包含 `tts_audio_path` 字段，可请求该路径下载云端合成的语音播报音频（PCM 格式），通过 I2S 功放直接播放。

### 6.3 路径参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `event_id` | string | 是 | 事件 ID |

### 6.4 请求头

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `Authorization` | 是（生产） | `Bearer <device_token>` |

### 6.5 成功响应

返回 `200 OK`，Body 为原始 PCM 音频流：

- Content-Type: `application/octet-stream`
- Content-Length: 音频字节数
- 格式：PCM 16kHz / 16-bit / mono（小端序）

设备收到后可直接通过 I2S 写入 MAX98357A 播放，无需解码。

### 6.6 结果查询响应中的关联字段

当分析完成且 TTS 音频已生成时，`/api/v1/device/events/{event_id}/result` 的 `data` 中会包含：

```json
{
  "tts_audio_path": "/api/v1/device/events/evt_xxx/tts"
}
```

设备应检查此字段是否存在，存在时请求下载并播放。

### 6.7 失败响应

事件不存在：

```json
{
  "code": 40401,
  "message": "event not found",
  "data": null
}
```

TTS 尚未生成（分析未完成或 TTS 合成失败）：

```json
{
  "code": 40402,
  "message": "tts audio not ready",
  "data": null
}
```

## 7. V1 处理流程

1. 设备调用 `/api/v1/health` 验证云端可用（同时同步服务端时间）。
2. PIR、VAD、按键或组合触发设备进入采集流程。
3. 设备完成拍照和短音频录制，并调用 `/api/v1/device/events` 上传。
4. 服务端返回 `event_id` 与初始状态 `queued`。
5. 云端异步分析事件（ASR → 多模态 AI → TTS 合成）。
6. 设备轮询 `/api/v1/device/events/{event_id}/result` 获取最终状态与结果。
7. 若结果中包含 `tts_audio_path`，设备请求 `/api/v1/device/events/{event_id}/tts` 下载 PCM 音频。
8. 设备通过 I2S 功放播放 TTS 音频；若 TTS 不可用则播放本地提示音作为 fallback。

## 8. 待确认项

- 是否需要单独的设备注册接口
- 健康检查接口是否要求鉴权
- 上传接口是否支持一次上传多张图片
- 设备幂等键是否使用 `sequence_id`、`request_id` 或哈希值
- VAD 触发元数据的最终字段定义
- `audio` 是否始终必填，还是只在 VAD 命中时必填
- 结果接口是否需要支持服务端主动回调或 MQTT 推送
- `voice_broadcast_text` 是最终播报文本，还是仅作为模板变量
- 语音播报由设备侧 TTS 完成还是由云端生成音频后下发
- 风险等级与事件分类的最终枚举定义
- 文件大小限制、存储路径规范与结果过期策略
