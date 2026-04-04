# AI 看护系统门口看护器 V1 设备端状态机

## 1. 说明

本文定义门口看护器 V1 的设备端状态机，用于指导设备端固件或应用逻辑实现。重点覆盖触发、采集、上传、等待结果、播放反馈与异常恢复。

V1 默认采用：

- PIR 与 VAD 作为主要触发源
- 设备主动上传事件
- 云端异步分析
- 设备轮询结果
- 设备本地语音播报

## 2. 状态机总览

```text
[BOOT]
  |
  v
[INIT] --> [ERROR]
  |
  v
[IDLE] <-----------------------------------------------+
  |                                                     |
  | trigger detected                                    |
  v                                                     |
[TRIGGERED]                                             |
  |                                                     |
  v                                                     |
[CAPTURING] --> [CAPTURE_FAILED] ------------------+    |
  |                                                 |    |
  v                                                 |    |
[READY_TO_UPLOAD]                                   |    |
  |                                                 |    |
  v                                                 |    |
[UPLOADING] --> [UPLOAD_FAILED] ----------------+   |    |
  |                                              |   |    |
  v                                              |   |    |
[WAITING_RESULT] --> [RESULT_TIMEOUT] --------+  |   |    |
  |                                            |  |   |    |
  | result done                                |  |   |    |
  v                                            |  |   |    |
[PLAYING_FEEDBACK] --> [PLAYBACK_FAILED] -----+  |   |    |
  |                                               |   |    |
  v                                               |   |    |
[COMPLETE] ---------------------------------------+---+----+
  |
  v
[IDLE]
```

## 3. 状态列表

| 状态 | 说明 |
| --- | --- |
| `BOOT` | 设备上电后的初始状态 |
| `INIT` | 初始化硬件、网络、配置与运行上下文 |
| `IDLE` | 待机状态，等待 PIR 或 VAD 触发 |
| `TRIGGERED` | 检测到触发事件，进入一次事件处理流程 |
| `CAPTURING` | 正在拍照、录音并收集触发元数据 |
| `CAPTURE_FAILED` | 采集失败，如摄像头或音频链路异常 |
| `READY_TO_UPLOAD` | 本地事件数据已准备完成，等待上传 |
| `UPLOADING` | 正在调用上传接口 |
| `UPLOAD_FAILED` | 上传失败，如网络异常或服务端拒绝 |
| `WAITING_RESULT` | 上传成功后，等待或轮询云端分析结果 |
| `RESULT_TIMEOUT` | 超过等待阈值仍未拿到最终结果 |
| `PLAYING_FEEDBACK` | 正在播放语音播报 |
| `PLAYBACK_FAILED` | 播报失败，如功放链路异常 |
| `COMPLETE` | 单次事件处理完成，准备回到待机 |
| `ERROR` | 初始化级别错误，设备无法正常进入主流程 |

## 4. 状态定义

### 4.1 `BOOT`

- 进入条件：设备上电或复位
- 退出条件：启动主程序后进入 `INIT`

### 4.2 `INIT`

- 职责：
  - 初始化 PIR、摄像头、音频输入、喇叭输出
  - 初始化 WiFi 与配置
  - 可选执行健康检查
- 成功后进入：`IDLE`
- 失败后进入：`ERROR`

### 4.3 `IDLE`

- 职责：
  - 监听 PIR 触发
  - 监听 VAD 触发
  - 清理上一事件的临时上下文
- 进入条件：
  - 初始化完成
  - 单次事件处理完成
  - 某些失败状态完成恢复
- 退出条件：
  - 检测到有效触发，进入 `TRIGGERED`

### 4.4 `TRIGGERED`

- 职责：
  - 锁定本次事件上下文
  - 记录触发时间、触发源
  - 决定是否合并 PIR 与 VAD 为单次事件
- 成功后进入：`CAPTURING`
- 无效触发或去抖过滤后返回：`IDLE`

### 4.5 `CAPTURING`

- 职责：
  - 拍照
  - 录制短音频
  - 记录 `sensor_payload`、`vad_payload`
  - 生成待上传事件数据
- 成功后进入：`READY_TO_UPLOAD`
- 失败后进入：`CAPTURE_FAILED`

### 4.6 `READY_TO_UPLOAD`

- 职责：
  - 检查事件对象完整性
  - 检查必填字段与文件是否存在
  - 准备上传请求
- 成功后进入：`UPLOADING`
- 数据不完整时进入：`CAPTURE_FAILED`

### 4.7 `UPLOADING`

- 职责：
  - 调用上传接口
  - 记录服务端返回的 `event_id`
  - 保存必要的请求追踪信息
- 成功后进入：`WAITING_RESULT`
- 失败后进入：`UPLOAD_FAILED`

### 4.8 `WAITING_RESULT`

- 职责：
  - 按轮询策略调用结果接口
  - 识别 `queued`、`processing`、`done`、`failed`
  - 解析 `voice_broadcast_text`
- 分析完成后进入：`PLAYING_FEEDBACK`
- 分析失败时进入：`COMPLETE` 或异常分支，具体策略待确认
- 超时后进入：`RESULT_TIMEOUT`

### 4.9 `PLAYING_FEEDBACK`

- 职责：
  - 驱动本地播报模块
  - 播放云端返回的建议播报文本或等效反馈内容
- 成功后进入：`COMPLETE`
- 失败后进入：`PLAYBACK_FAILED`

### 4.10 `COMPLETE`

- 职责：
  - 回收临时资源
  - 写入本地日志或统计信息
  - 清理当前事件上下文
- 退出条件：回到 `IDLE`

### 4.11 `ERROR`

- 职责：
  - 标记初始化失败
  - 记录错误原因
  - 进入等待人工恢复、重启或自动重试

## 5. 状态转换条件

| 当前状态 | 条件 | 下一个状态 |
| --- | --- | --- |
| `BOOT` | 主程序启动 | `INIT` |
| `INIT` | 硬件和网络初始化成功 | `IDLE` |
| `INIT` | 关键模块初始化失败 | `ERROR` |
| `IDLE` | PIR 或 VAD 有效触发 | `TRIGGERED` |
| `TRIGGERED` | 触发有效且通过去抖 | `CAPTURING` |
| `TRIGGERED` | 判定为误触发 | `IDLE` |
| `CAPTURING` | 图片和音频采集完成 | `READY_TO_UPLOAD` |
| `CAPTURING` | 摄像头或音频采集失败 | `CAPTURE_FAILED` |
| `READY_TO_UPLOAD` | 数据完整 | `UPLOADING` |
| `READY_TO_UPLOAD` | 数据缺失 | `CAPTURE_FAILED` |
| `UPLOADING` | 上传成功并获得 `event_id` | `WAITING_RESULT` |
| `UPLOADING` | 上传失败 | `UPLOAD_FAILED` |
| `WAITING_RESULT` | 结果状态为 `done` | `PLAYING_FEEDBACK` |
| `WAITING_RESULT` | 结果状态为 `failed` | `COMPLETE` |
| `WAITING_RESULT` | 达到超时阈值 | `RESULT_TIMEOUT` |
| `PLAYING_FEEDBACK` | 播报成功 | `COMPLETE` |
| `PLAYING_FEEDBACK` | 播报失败 | `PLAYBACK_FAILED` |
| `CAPTURE_FAILED` | 放弃本次事件 | `IDLE` |
| `UPLOAD_FAILED` | 重试成功 | `WAITING_RESULT` |
| `UPLOAD_FAILED` | 达到重试上限或放弃 | `IDLE` |
| `RESULT_TIMEOUT` | 超时后放弃等待 | `IDLE` |
| `PLAYBACK_FAILED` | 跳过播报或失败记录完成 | `COMPLETE` |
| `COMPLETE` | 清理完成 | `IDLE` |

## 6. 异常流转

### 6.1 采集异常

- 摄像头不可用
- 麦克风链路异常
- 本地缓存不足

建议流转：

- `CAPTURING -> CAPTURE_FAILED -> IDLE`

### 6.2 上传异常

- WiFi 断开
- 上传超时
- 服务端返回 `4xx` 或 `5xx`

建议流转：

- `UPLOADING -> UPLOAD_FAILED`
- 若允许重试：`UPLOAD_FAILED -> UPLOADING`
- 若放弃：`UPLOAD_FAILED -> IDLE`

### 6.3 等待结果异常

- 长时间处于 `queued` 或 `processing`
- 查询接口失败
- 云端任务失败

建议流转：

- `WAITING_RESULT -> RESULT_TIMEOUT -> IDLE`
- 或 `WAITING_RESULT -> COMPLETE`，当结果明确失败时

### 6.4 播报异常

- MAX98357A 初始化失败
- 喇叭输出异常
- 播报文本为空

建议流转：

- `PLAYING_FEEDBACK -> PLAYBACK_FAILED -> COMPLETE`

## 7. 与关键链路相关的状态

### 7.1 与上传相关

- `READY_TO_UPLOAD`
- `UPLOADING`
- `UPLOAD_FAILED`

### 7.2 与等待结果相关

- `WAITING_RESULT`
- `RESULT_TIMEOUT`

### 7.3 与播放反馈相关

- `PLAYING_FEEDBACK`
- `PLAYBACK_FAILED`

## 8. V1 实现建议

### 8.1 状态机实现方式

- 使用单线程事件循环或显式状态枚举
- 每次状态切换都记录日志
- 每个状态尽量只做一类职责

### 8.2 最小可实现原则

- 先实现线性主路径
- 异常状态先做到可记录、可返回待机
- 复杂并发、事件合并和后台任务优化可以后续增强

## 9. 待确认项

- PIR 与 VAD 是否允许合并为同一事件
- VAD 触发是否必须附带音频文件
- `WAITING_RESULT` 的轮询间隔与最大等待时长
- 云端返回失败结果时是否仍需要本地播报
- 播报失败后的重试次数与降级策略
- 是否需要本地离线缓存和补传状态
