#ifndef CONFIG_H
#define CONFIG_H

// 私密配置（WiFi、服务器地址、Token）—— 不提交版本控制
#include "secrets.h"

// ==================== 协议选择 ====================
// 编译时通过 build_flags 定义 USE_HTTPS 切换：
//   -DUSE_HTTPS    → 生产（HTTPS）
//   不定义         → 开发（HTTP）
#ifdef USE_HTTPS
  #define SERVER_PROTO  "https"
#else
  #define SERVER_PROTO  "http"
#endif

// ==================== API 路径 ====================
#define API_HEALTH      "/api/v1/health"
#define API_UPLOAD      "/api/v1/device/events"
// result query path 从上传响应的 result_query_path 字段获取

// ==================== 设备标识 ====================
#define DEVICE_ID           "xiao-door-001"
#define FIRMWARE_VERSION    "1.0.0"

// ==================== 触发按键配置（暂替 PIR） ====================
#define TRIGGER_PIN         D1              // GPIO2, 外接按键（按下接 GND）
#define TRIGGER_DEBOUNCE_MS 3000            // 按键去抖间隔（毫秒）

// ==================== 录音配置 ====================
#define I2S_MIC_PORT        I2S_NUM_0
#define I2S_MIC_SAMPLE_RATE 16000           // 16kHz 采样率
#define I2S_MIC_BITS        16              // 16-bit PCM
#define I2S_MIC_CHANNEL     1               // 单声道
#define RECORD_DURATION_SEC 5               // 固定录音时长（秒）

// XIAO ESP32S3 Sense 板载 PDM 麦克风引脚
#define I2S_MIC_CLK         42              // PDM CLK
#define I2S_MIC_DATA        41              // PDM DATA

// ==================== I2S 音频输出配置（MAX98357A） ====================
#define I2S_SPK_PORT        I2S_NUM_1
#define I2S_SPK_SAMPLE_RATE 16000
#define I2S_SPK_BITS        16
#define I2S_SPK_BCLK        D0              // GPIO1
#define I2S_SPK_LRC         D4              // GPIO4
#define I2S_SPK_DIN         D2              // GPIO3

// ==================== 摄像头引脚定义 (XIAO ESP32S3 Sense) ====================
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK    10
#define CAM_PIN_SIOD    40
#define CAM_PIN_SIOC    39
#define CAM_PIN_D7      48
#define CAM_PIN_D6      11
#define CAM_PIN_D5      12
#define CAM_PIN_D4      14
#define CAM_PIN_D3      16
#define CAM_PIN_D2      18
#define CAM_PIN_D1      17
#define CAM_PIN_D0      15
#define CAM_PIN_VSYNC   38
#define CAM_PIN_HREF    47
#define CAM_PIN_PCLK    13

// ==================== LED 指示灯 ====================
// LED_BUILTIN 已由板级定义（低电平点亮）

// ==================== 轮询配置 ====================
#define POLL_INTERVAL_MS    2000            // 轮询间隔（毫秒）
#define POLL_MAX_ATTEMPTS   15              // 最大轮询次数
#define HEALTH_TIMEOUT_MS   3000            // health check 超时
#define UPLOAD_TIMEOUT_MS   30000           // 上传超时

#endif // CONFIG_H
