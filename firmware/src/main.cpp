/**
 * AI 门口看护器 V1 - XIAO ESP32-S3 Sense 固件
 *
 * 对齐后端 API (ai-guardian-system-health/backend):
 *   - GET  /api/v1/health              → health check
 *   - POST /api/v1/device/events       → multipart 上传 (image + audio + fields)
 *   - GET  /api/v1/device/events/{id}/result → 轮询分析结果
 *
 * 触发方式：按键（暂替 PIR），按下触发后自动执行：
 *   拍照 → 录音(固定时长) → health check → 上传 → 轮询结果 → 语音播报
 *
 * 硬件：
 *   - Seeed XIAO ESP32-S3 Sense（OV2640 + PDM 麦克风）
 *   - 按键 → D1 (GPIO2)，按下接 GND
 *   - MAX98357A I2S 功放 + 喇叭
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#ifdef USE_HTTPS
#include <WiFiClientSecure.h>
#endif
#include <driver/i2s.h>
#include "esp_camera.h"
#include <ArduinoJson.h>
#include "config.h"

// ==================== 前向声明 ====================
bool initCamera();
bool initMicrophone();
bool initSpeaker();
void connectWiFi();
camera_fb_t* captureImage();
uint8_t* recordAudio(size_t *outSize);
void buildWavHeader(uint8_t *header, size_t dataSize);
bool checkHealth();
String uploadEvent(camera_fb_t *image, uint8_t *audio, size_t audioSize);
String pollResult(const String &resultPath, String &ttsPath);
bool fetchAndPlayTTS(const String &ttsPath);
void playPcmBeep(int freq, int durationMs);
void processEvent();

// ==================== 全局状态 ====================
static bool g_isProcessing = false;
static unsigned long g_lastTrigger = 0;

// 服务端时间同步：health check 时解析为 epoch，millis 偏移回推 ISO 8601
static time_t g_serverEpoch = 0;          // 服务端 UTC epoch 秒
static unsigned long g_serverSyncMs = 0;  // 同步时的 millis()

// ==================== 状态枚举（对齐 device/state_machine.py） ====================
enum DeviceState {
    STATE_IDLE,
    STATE_TRIGGERED,
    STATE_CAPTURING,
    STATE_CAPTURE_FAILED,
    STATE_READY_TO_UPLOAD,
    STATE_UPLOADING,
    STATE_UPLOAD_FAILED,
    STATE_WAITING_RESULT,
    STATE_RESULT_TIMEOUT,
    STATE_PLAYING_FEEDBACK,
    STATE_PLAYBACK_FAILED,
    STATE_COMPLETE,
};

static DeviceState g_state = STATE_IDLE;

static void setState(DeviceState s, const char *reason) {
    g_state = s;
    static const char *names[] = {
        "IDLE", "TRIGGERED", "CAPTURING", "CAPTURE_FAILED",
        "READY_TO_UPLOAD", "UPLOADING", "UPLOAD_FAILED",
        "WAITING_RESULT", "RESULT_TIMEOUT", "PLAYING_FEEDBACK",
        "PLAYBACK_FAILED", "COMPLETE",
    };
    Serial.printf("[STATE] %s — %s\n", names[s], reason);
}

// ==================== 摄像头初始化 ====================
bool initCamera() {
    camera_config_t config;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer   = LEDC_TIMER_0;
    config.pin_d0       = CAM_PIN_D0;
    config.pin_d1       = CAM_PIN_D1;
    config.pin_d2       = CAM_PIN_D2;
    config.pin_d3       = CAM_PIN_D3;
    config.pin_d4       = CAM_PIN_D4;
    config.pin_d5       = CAM_PIN_D5;
    config.pin_d6       = CAM_PIN_D6;
    config.pin_d7       = CAM_PIN_D7;
    config.pin_xclk     = CAM_PIN_XCLK;
    config.pin_pclk     = CAM_PIN_PCLK;
    config.pin_vsync    = CAM_PIN_VSYNC;
    config.pin_href     = CAM_PIN_HREF;
    config.pin_sccb_sda = CAM_PIN_SIOD;
    config.pin_sccb_scl = CAM_PIN_SIOC;
    config.pin_pwdn     = CAM_PIN_PWDN;
    config.pin_reset    = CAM_PIN_RESET;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    config.frame_size   = FRAMESIZE_VGA;      // 640x480
    config.jpeg_quality = 12;
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST;

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("[CAM] init failed: 0x%x\n", err);
        return false;
    }

    sensor_t *s = esp_camera_sensor_get();
    if (s) {
        s->set_brightness(s, 1);
        s->set_saturation(s, 0);
    }

    Serial.println("[CAM] init ok");
    return true;
}

// ==================== I2S PDM 麦克风初始化 ====================
bool initMicrophone() {
    i2s_config_t i2s_mic_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM),
        .sample_rate = I2S_MIC_SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 8,
        .dma_buf_len = 1024,
        .use_apll = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk = 0,
    };

    i2s_pin_config_t mic_pin_config = {
        .bck_io_num   = I2S_PIN_NO_CHANGE,
        .ws_io_num    = I2S_MIC_CLK,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num  = I2S_MIC_DATA,
    };

    esp_err_t err = i2s_driver_install(I2S_MIC_PORT, &i2s_mic_config, 0, NULL);
    if (err != ESP_OK) {
        Serial.printf("[MIC] i2s install failed: 0x%x\n", err);
        return false;
    }

    err = i2s_set_pin(I2S_MIC_PORT, &mic_pin_config);
    if (err != ESP_OK) {
        Serial.printf("[MIC] i2s pin config failed: 0x%x\n", err);
        return false;
    }

    Serial.println("[MIC] PDM init ok");
    return true;
}

// ==================== I2S 扬声器初始化 ====================
bool initSpeaker() {
    i2s_config_t i2s_spk_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
        .sample_rate = I2S_SPK_SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_MSB,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 8,
        .dma_buf_len = 1024,
        .use_apll = false,
        .tx_desc_auto_clear = true,
        .fixed_mclk = 0,
    };

    i2s_pin_config_t spk_pin_config = {
        .bck_io_num   = I2S_SPK_BCLK,
        .ws_io_num    = I2S_SPK_LRC,
        .data_out_num = I2S_SPK_DIN,
        .data_in_num  = I2S_PIN_NO_CHANGE,
    };

    esp_err_t err = i2s_driver_install(I2S_SPK_PORT, &i2s_spk_config, 0, NULL);
    if (err != ESP_OK) {
        Serial.printf("[SPK] i2s install failed: 0x%x\n", err);
        return false;
    }

    err = i2s_set_pin(I2S_SPK_PORT, &spk_pin_config);
    if (err != ESP_OK) {
        Serial.printf("[SPK] i2s pin config failed: 0x%x\n", err);
        return false;
    }

    Serial.println("[SPK] init ok");
    return true;
}

// ==================== WiFi 连接 ====================
void connectWiFi() {
    Serial.printf("[WIFI] connecting to %s ...\n", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    WiFi.setSleep(false);

    int retry = 0;
    while (WiFi.status() != WL_CONNECTED && retry < 30) {
        delay(500);
        Serial.print(".");
        retry++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\n[WIFI] connected, IP: %s\n", WiFi.localIP().toString().c_str());
    } else {
        Serial.println("\n[WIFI] connect failed, will retry...");
    }
}

// ==================== 拍照 ====================
camera_fb_t* captureImage() {
    // 丢弃前 3 帧做 AE 校准（对齐参考固件）
    for (int i = 0; i < 3; i++) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) esp_camera_fb_return(fb);
        delay(100);
    }

    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        Serial.println("[CAM] capture failed");
        return nullptr;
    }
    Serial.printf("[CAM] captured: %dx%d, %d bytes\n", fb->width, fb->height, fb->len);
    return fb;
}

// ==================== 固定时长录音 ====================
uint8_t* recordAudio(size_t *outSize) {
    size_t maxBytes = I2S_MIC_SAMPLE_RATE * RECORD_DURATION_SEC * sizeof(int16_t);

    uint8_t *audioBuffer = (uint8_t *)ps_malloc(maxBytes);
    if (!audioBuffer) {
        Serial.println("[MIC] ps_malloc failed");
        *outSize = 0;
        return nullptr;
    }

    Serial.printf("[MIC] recording %d sec ...\n", RECORD_DURATION_SEC);

    size_t bytesRead = 0;
    size_t totalRead = 0;
    size_t chunkSize = 1024;
    unsigned long startTime = millis();

    while (totalRead < maxBytes) {
        if (millis() - startTime > (unsigned long)RECORD_DURATION_SEC * 1000) {
            break;
        }

        size_t toRead = min(chunkSize, maxBytes - totalRead);
        i2s_read(I2S_MIC_PORT, audioBuffer + totalRead, toRead, &bytesRead, 100 / portTICK_PERIOD_MS);
        totalRead += bytesRead;
    }

    // 16-bit PCM 对齐
    totalRead &= ~1;

    float duration = (float)totalRead / (I2S_MIC_SAMPLE_RATE * sizeof(int16_t));
    Serial.printf("[MIC] recorded: %d bytes (%.1f sec)\n", totalRead, duration);
    *outSize = totalRead;
    return audioBuffer;
}

// ==================== 构建 WAV 头（对齐 adapters.py build_wav_header） ====================
void buildWavHeader(uint8_t *header, size_t dataSize) {
    uint32_t fileSize = 36 + dataSize;
    uint32_t byteRate = I2S_MIC_SAMPLE_RATE * I2S_MIC_CHANNEL * (I2S_MIC_BITS / 8);
    uint16_t blockAlign = I2S_MIC_CHANNEL * (I2S_MIC_BITS / 8);

    memcpy(header, "RIFF", 4);
    memcpy(header + 4, &fileSize, 4);
    memcpy(header + 8, "WAVE", 4);

    memcpy(header + 12, "fmt ", 4);
    uint32_t fmtSize = 16;
    memcpy(header + 16, &fmtSize, 4);
    uint16_t audioFormat = 1; // PCM
    memcpy(header + 20, &audioFormat, 2);
    uint16_t numChannels = I2S_MIC_CHANNEL;
    memcpy(header + 22, &numChannels, 2);
    uint32_t sampleRate = I2S_MIC_SAMPLE_RATE;
    memcpy(header + 24, &sampleRate, 4);
    memcpy(header + 28, &byteRate, 4);
    memcpy(header + 32, &blockAlign, 2);
    uint16_t bitsPerSample = I2S_MIC_BITS;
    memcpy(header + 34, &bitsPerSample, 2);

    memcpy(header + 36, "data", 4);
    memcpy(header + 40, &dataSize, 4);
}

// ==================== ISO 8601 时间工具 ====================

/**
 * 解析 ISO 8601 时间串为 UTC epoch 秒。
 * 支持格式：
 *   2026-04-04T13:20:00Z
 *   2026-04-04T13:20:00+00:00
 *   2026-04-04T13:20:00.123456+08:00
 *   2026-04-04T13:20:00-05:30
 * 小数秒被忽略；时区偏移被正确减去以转为 UTC。
 */
static time_t parseISO8601(const char *iso) {
    struct tm t = {};
    if (sscanf(iso, "%d-%d-%dT%d:%d:%d",
               &t.tm_year, &t.tm_mon, &t.tm_mday,
               &t.tm_hour, &t.tm_min, &t.tm_sec) < 6) {
        return 0;
    }
    t.tm_year -= 1900;
    t.tm_mon -= 1;

    // mktime 在 ESP32 默认 TZ=UTC 下按 UTC 处理
    time_t epoch = mktime(&t);

    // 解析时区偏移：跳过小数秒部分，找 +/-/Z
    const char *p = iso + 19;  // 跳过 "YYYY-MM-DDTHH:MM:SS"
    // 跳过可选小数秒 ".123456"
    if (*p == '.') {
        p++;
        while (*p >= '0' && *p <= '9') p++;
    }

    if (*p == 'Z' || *p == 'z') {
        // 已经是 UTC，无需调整
    } else if (*p == '+' || *p == '-') {
        int sign = (*p == '+') ? 1 : -1;
        int tzH = 0, tzM = 0;
        sscanf(p + 1, "%d:%d", &tzH, &tzM);
        // 将本地时间转为 UTC：减去时区偏移
        epoch -= sign * (tzH * 3600 + tzM * 60);
    }
    // 无时区后缀时假定 UTC

    return epoch;
}

/**
 * 将 epoch + 偏移秒格式化为 ISO 8601 UTC 字符串。
 */
static String formatISO8601(time_t epoch) {
    struct tm t;
    gmtime_r(&epoch, &t);
    char buf[32];
    snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d+00:00",
             t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
             t.tm_hour, t.tm_min, t.tm_sec);
    return String(buf);
}

// ==================== URL 构建 + TLS ====================
static String buildURL(const String &path) {
    return String(SERVER_PROTO) + "://" + SERVER_HOST + ":" + String(SERVER_PORT) + path;
}

#ifdef USE_HTTPS
static WiFiClientSecure g_tlsClient;

static void initTLS() {
    // 跳过证书校验（自签名 / 开发阶段）。
    // 生产正式部署时应替换为 setCACert(root_ca_pem) 固定根证书。
    g_tlsClient.setInsecure();
    Serial.println("[TLS] initialized (insecure mode)");
}
#endif

/**
 * 对 HTTPClient 调用 begin()：HTTPS 时使用 WiFiClientSecure，HTTP 时直接 URL。
 */
static void httpBegin(HTTPClient &http, const String &url) {
#ifdef USE_HTTPS
    http.begin(g_tlsClient, url);
#else
    httpBegin(http, url);
#endif
}

// ==================== Health Check ====================
bool checkHealth() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[HEALTH] WiFi not connected");
        return false;
    }

    HTTPClient http;
    String url = buildURL(API_HEALTH);
    httpBegin(http, url);
    http.setTimeout(HEALTH_TIMEOUT_MS);

    int code = http.GET();
    if (code == 200) {
        String body = http.getString();
        http.end();

        JsonDocument doc;
        if (deserializeJson(doc, body)) {
            Serial.println("[HEALTH] JSON parse error");
            return false;
        }
        const char *status = doc["data"]["status"] | "";
        bool ok = strcmp(status, "up") == 0;

        // 解析并缓存服务端时间用于 captured_at
        const char *serverTime = doc["data"]["time"] | "";
        if (strlen(serverTime) > 0) {
            time_t epoch = parseISO8601(serverTime);
            if (epoch > 0) {
                g_serverEpoch = epoch;
                g_serverSyncMs = millis();
            }
        }

        Serial.printf("[HEALTH] status=%s, ok=%d, time=%s\n", status, ok, serverTime);
        return ok;
    }

    Serial.printf("[HEALTH] failed, code=%d\n", code);
    http.end();
    return false;
}

// ==================== 上传事件（multipart/form-data） ====================
// 对齐 POST /api/v1/device/events
// 返回 result_query_path（如 "/api/v1/device/events/evt_xxx/result"），失败返回空串
String uploadEvent(camera_fb_t *image, uint8_t *audio, size_t audioSize) {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[UPLOAD] WiFi not connected");
        return "";
    }

    HTTPClient http;
    String url = buildURL(API_UPLOAD);
    httpBegin(http, url);
    http.setTimeout(UPLOAD_TIMEOUT_MS);

    String boundary = "----ESP32Boundary" + String(millis());
    http.addHeader("Content-Type", "multipart/form-data; boundary=" + boundary);
    http.addHeader("Authorization", "Bearer " DEVICE_TOKEN);

    // WAV header
    uint8_t wavHeader[44];
    buildWavHeader(wavHeader, audioSize);

    // 构建各 multipart part
    // form fields
    String fieldDeviceId = "--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"device_id\"\r\n\r\n"
        DEVICE_ID "\r\n";
    String fieldEventType = "--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"event_type\"\r\n\r\n"
        "visitor_detected\r\n";
    String fieldTriggerSource = "--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"trigger_source\"\r\n\r\n"
        "pir\r\n";

    // captured_at: 基于 health check 同步的服务端 epoch + millis 偏移，生成 ISO 8601
    String capturedAt;
    if (g_serverEpoch > 0) {
        unsigned long elapsedSec = (millis() - g_serverSyncMs) / 1000;
        capturedAt = formatISO8601(g_serverEpoch + (time_t)elapsedSec);
    } else {
        // 未同步时用 received_at 作为 fallback（服务端在 create_event 中自动生成）
        capturedAt = "1970-01-01T00:00:00+00:00";
    }
    String fieldCapturedAt = "--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"captured_at\"\r\n\r\n"
        + capturedAt + "\r\n";

    String fieldFwVer = "--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"firmware_version\"\r\n\r\n"
        FIRMWARE_VERSION "\r\n";

    String fieldSensor = "--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"sensor_payload\"\r\n\r\n"
        "{\"pir\":true}\r\n";

    // file parts
    String partImage = "--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"image\"; filename=\"photo.jpg\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n";
    String partAudio = "\r\n--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"audio\"; filename=\"record.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n";
    String partEnd = "\r\n--" + boundary + "--\r\n";

    // 计算总大小
    size_t totalSize = fieldDeviceId.length()
        + fieldEventType.length()
        + fieldTriggerSource.length()
        + fieldCapturedAt.length()
        + fieldFwVer.length()
        + fieldSensor.length()
        + partImage.length() + image->len
        + partAudio.length() + 44 + audioSize
        + partEnd.length();

    Serial.printf("[UPLOAD] building request: %d bytes\n", totalSize);
    uint8_t *body = (uint8_t *)ps_malloc(totalSize);
    if (!body) {
        Serial.println("[UPLOAD] ps_malloc failed");
        http.end();
        return "";
    }

    // 拼接请求体
    size_t offset = 0;
    #define APPEND_STR(s) do { memcpy(body + offset, (s).c_str(), (s).length()); offset += (s).length(); } while(0)
    #define APPEND_BUF(buf, len) do { memcpy(body + offset, buf, len); offset += (len); } while(0)

    APPEND_STR(fieldDeviceId);
    APPEND_STR(fieldEventType);
    APPEND_STR(fieldTriggerSource);
    APPEND_STR(fieldCapturedAt);
    APPEND_STR(fieldFwVer);
    APPEND_STR(fieldSensor);
    APPEND_STR(partImage);
    APPEND_BUF(image->buf, image->len);
    APPEND_STR(partAudio);
    APPEND_BUF(wavHeader, 44);
    APPEND_BUF(audio, audioSize);
    APPEND_STR(partEnd);

    #undef APPEND_STR
    #undef APPEND_BUF

    Serial.println("[UPLOAD] sending...");
    int httpCode = http.sendRequest("POST", body, totalSize);
    free(body);

    Serial.printf("[UPLOAD] response code: %d\n", httpCode);

    if (httpCode != 202) {
        Serial.printf("[UPLOAD] failed: %d\n", httpCode);
        http.end();
        return "";
    }

    // 解析响应 JSON
    String respBody = http.getString();
    http.end();

    JsonDocument doc;
    if (deserializeJson(doc, respBody)) {
        Serial.println("[UPLOAD] JSON parse error");
        return "";
    }

    const char *resultPath = doc["data"]["result_query_path"] | "";
    if (strlen(resultPath) > 0) {
        Serial.printf("[UPLOAD] result_query_path: %s\n", resultPath);
        return String(resultPath);
    }

    // fallback: 用 event_id 构造路径
    const char *eventId = doc["data"]["event_id"] | "";
    if (strlen(eventId) > 0) {
        String path = "/api/v1/device/events/" + String(eventId) + "/result";
        Serial.printf("[UPLOAD] constructed path: %s\n", path.c_str());
        return path;
    }

    Serial.println("[UPLOAD] no result_query_path or event_id in response");
    return "";
}

// ==================== 轮询分析结果 ====================
// 对齐 GET /api/v1/device/events/{id}/result
// 返回 voice_broadcast_text，ttsPath 中存放 TTS 音频下载路径
String pollResult(const String &resultPath, String &ttsPath) {
    String url = buildURL(resultPath);
    ttsPath = "";

    for (int attempt = 1; attempt <= POLL_MAX_ATTEMPTS; attempt++) {
        Serial.printf("[POLL] attempt %d/%d\n", attempt, POLL_MAX_ATTEMPTS);

        HTTPClient http;
        httpBegin(http, url);
        http.setTimeout(HEALTH_TIMEOUT_MS);
        http.addHeader("Authorization", "Bearer " DEVICE_TOKEN);

        int code = http.GET();
        if (code == 200) {
            String body = http.getString();
            http.end();

            JsonDocument doc;
            if (deserializeJson(doc, body)) {
                Serial.println("[POLL] JSON parse error");
                delay(POLL_INTERVAL_MS);
                continue;
            }

            const char *status = doc["data"]["status"] | "";

            if (strcmp(status, "done") == 0) {
                const char *tts = doc["data"]["tts_audio_path"] | "";
                if (strlen(tts) > 0) {
                    ttsPath = String(tts);
                    Serial.printf("[POLL] tts_audio_path: %s\n", tts);
                }

                const char *text = doc["data"]["voice_broadcast_text"] | "";
                if (strlen(text) > 0) {
                    Serial.printf("[POLL] done, text: %s\n", text);
                    return String(text);
                }
                Serial.println("[POLL] done but no broadcast text");
                return "done";
            }

            if (strcmp(status, "failed") == 0) {
                Serial.println("[POLL] analysis failed");
                return "";
            }

            Serial.printf("[POLL] status=%s, waiting...\n", status);
        } else {
            Serial.printf("[POLL] http error: %d\n", code);
            http.end();
        }

        delay(POLL_INTERVAL_MS);
    }

    Serial.println("[POLL] timeout");
    return "";
}

// ==================== 从后端拉取 TTS PCM 音频并播放 ====================
// 对齐参考固件 uploadAndGetResponse() 的播放逻辑
bool fetchAndPlayTTS(const String &ttsPath) {
    if (ttsPath.length() == 0) return false;

    String url = buildURL(ttsPath);
    Serial.printf("[TTS] fetching %s\n", url.c_str());

    HTTPClient http;
    httpBegin(http, url);
    http.setTimeout(30000);
    http.addHeader("Authorization", "Bearer " DEVICE_TOKEN);

    int code = http.GET();
    if (code != 200) {
        Serial.printf("[TTS] fetch failed, code=%d\n", code);
        http.end();
        return false;
    }

    int len = http.getSize();
    Serial.printf("[TTS] response: %d bytes\n", len);

    WiFiClient *stream = http.getStreamPtr();
    if (!stream) {
        http.end();
        return false;
    }

    // 读取全部 PCM 到 PSRAM（对齐参考固件）
    size_t allocSize = len > 0 ? (size_t)len : 512000;
    uint8_t *pcmBuf = (uint8_t *)ps_malloc(allocSize);
    if (!pcmBuf) {
        Serial.println("[TTS] ps_malloc failed");
        http.end();
        return false;
    }

    size_t totalRead = 0;
    unsigned long lastData = millis();
    while ((stream->available() || stream->connected()) && (millis() - lastData < 10000)) {
        int avail = stream->available();
        if (avail > 0) {
            int rd = stream->readBytes(pcmBuf + totalRead, avail);
            totalRead += rd;
            lastData = millis();
        } else {
            delay(5);
        }
        if (len > 0 && totalRead >= (size_t)len) break;
    }
    http.end();

    totalRead &= ~1;  // 16-bit PCM 对齐
    Serial.printf("[TTS] read %d bytes, playing...\n", totalRead);

    // 重装 I2S 扬声器驱动（录音后需要）
    i2s_driver_uninstall(I2S_SPK_PORT);
    delay(10);
    initSpeaker();

    // 预热静音
    uint8_t warmup[1024] = {0};
    for (int i = 0; i < 8; i++) {
        size_t bw;
        i2s_write(I2S_SPK_PORT, warmup, sizeof(warmup), &bw, portMAX_DELAY);
    }
    delay(50);

    // 播放 PCM（对齐参考固件的 1024 字节分块）
    size_t played = 0;
    while (played < totalRead) {
        size_t chunk = min((size_t)1024, totalRead - played);
        chunk &= ~1;
        size_t bw = 0;
        i2s_write(I2S_SPK_PORT, pcmBuf + played, chunk, &bw, portMAX_DELAY);
        played += bw;
    }

    // 尾部静音 flush DMA（对齐参考固件 8 × 512）
    uint8_t silence[512] = {0};
    for (int i = 0; i < 8; i++) {
        size_t bw;
        i2s_write(I2S_SPK_PORT, silence, sizeof(silence), &bw, portMAX_DELAY);
    }

    free(pcmBuf);
    Serial.printf("[TTS] playback done: %d bytes\n", played);
    return true;
}

// ==================== 提示音播放（TTS 失败时的 fallback） ====================
void playPcmBeep(int freq, int durationMs) {
    // 录音后 I2S_NUM_1 可能状态异常，重装驱动确保干净状态
    i2s_driver_uninstall(I2S_SPK_PORT);
    delay(10);
    initSpeaker();

    // 预热静音：让 MAX98357A 功放唤醒并锁定 I2S 时钟
    uint8_t warmup[1024] = {0};
    for (int i = 0; i < 8; i++) {
        size_t bw;
        i2s_write(I2S_SPK_PORT, warmup, sizeof(warmup), &bw, portMAX_DELAY);
    }
    delay(50);

    int numSamples = I2S_SPK_SAMPLE_RATE * durationMs / 1000;
    size_t bufSize = numSamples * sizeof(int16_t);
    int16_t *buf = (int16_t *)ps_malloc(bufSize);
    if (!buf) {
        Serial.println("[SPK] ps_malloc failed for beep");
        return;
    }

    // 满振幅正弦波（30000 / 32768 ≈ 91%）
    for (int i = 0; i < numSamples; i++) {
        float t = (float)i / I2S_SPK_SAMPLE_RATE;
        buf[i] = (int16_t)(30000.0f * sinf(2.0f * M_PI * freq * t));
    }

    size_t written = 0;
    size_t played = 0;
    while (played < bufSize) {
        size_t chunk = min((size_t)1024, bufSize - played);
        chunk &= ~1;
        i2s_write(I2S_SPK_PORT, (uint8_t *)buf + played, chunk, &written, portMAX_DELAY);
        played += written;
    }

    // 尾部静音确保 DMA 缓冲区全部播放完毕
    uint8_t silence[512] = {0};
    for (int i = 0; i < 8; i++) {
        size_t bw;
        i2s_write(I2S_SPK_PORT, silence, sizeof(silence), &bw, portMAX_DELAY);
    }

    free(buf);
    Serial.printf("[SPK] beep %dHz %dms done\n", freq, durationMs);
}

// ==================== 主处理流程（对齐 device/client.py run_once） ====================
void processEvent() {
    if (g_isProcessing) return;
    g_isProcessing = true;

    Serial.println("\n========== EVENT START ==========");
    digitalWrite(LED_BUILTIN, LOW);  // LED 亮

    // 1. TRIGGERED
    setState(STATE_TRIGGERED, "Button pressed");

    // 2. CAPTURING — 拍照
    setState(STATE_CAPTURING, "start capture");
    camera_fb_t *image = captureImage();
    if (!image) {
        setState(STATE_CAPTURE_FAILED, "camera error");
        setState(STATE_IDLE, "give up");
        digitalWrite(LED_BUILTIN, HIGH);
        g_isProcessing = false;
        return;
    }

    // 3. CAPTURING — 录音
    size_t audioSize = 0;
    uint8_t *audioData = recordAudio(&audioSize);
    if (!audioData || audioSize == 0) {
        setState(STATE_CAPTURE_FAILED, "mic error");
        esp_camera_fb_return(image);
        setState(STATE_IDLE, "give up");
        digitalWrite(LED_BUILTIN, HIGH);
        g_isProcessing = false;
        return;
    }

    // 4. READY_TO_UPLOAD — health check
    setState(STATE_READY_TO_UPLOAD, "capture done");
    if (!checkHealth()) {
        setState(STATE_UPLOAD_FAILED, "health check failed");
        esp_camera_fb_return(image);
        free(audioData);
        setState(STATE_IDLE, "give up");
        digitalWrite(LED_BUILTIN, HIGH);
        g_isProcessing = false;
        return;
    }

    // 5. UPLOADING
    setState(STATE_UPLOADING, "health ok");
    String resultPath = uploadEvent(image, audioData, audioSize);

    // 释放采集资源
    esp_camera_fb_return(image);
    free(audioData);

    if (resultPath.length() == 0) {
        setState(STATE_UPLOAD_FAILED, "upload rejected");
        setState(STATE_IDLE, "give up");
        digitalWrite(LED_BUILTIN, HIGH);
        g_isProcessing = false;
        return;
    }

    // 6. WAITING_RESULT — 轮询
    setState(STATE_WAITING_RESULT, "polling...");
    String ttsAudioPath;
    String broadcastText = pollResult(resultPath, ttsAudioPath);

    if (broadcastText.length() == 0) {
        setState(STATE_RESULT_TIMEOUT, "poll exhausted or failed");
        playPcmBeep(400, 500);  // 低沉提示音 = 失败
        setState(STATE_IDLE, "give up");
        digitalWrite(LED_BUILTIN, HIGH);
        g_isProcessing = false;
        return;
    }

    // 7. PLAYING_FEEDBACK — 拉取 TTS 音频并播放
    setState(STATE_PLAYING_FEEDBACK, broadcastText.c_str());
    bool ttsOk = fetchAndPlayTTS(ttsAudioPath);
    if (!ttsOk) {
        setState(STATE_PLAYBACK_FAILED, "TTS fetch/play failed");
        Serial.println("[SPK] TTS failed, playing beep fallback");
        playPcmBeep(800, 500);
    }

    // 8. COMPLETE → IDLE
    setState(STATE_COMPLETE, ttsOk ? "feedback done" : "fallback beep done");
    setState(STATE_IDLE, "cycle complete");

    Serial.println("========== EVENT DONE ==========\n");
    digitalWrite(LED_BUILTIN, HIGH);  // LED 灭
    g_isProcessing = false;
}

// ==================== Arduino Setup ====================
void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("\n============================");
    Serial.println("  AI Door Guardian V1");
    Serial.println("============================\n");

    // LED
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, HIGH);

    // 触发按键（按下接 GND，内部上拉）
    pinMode(TRIGGER_PIN, INPUT_PULLUP);

    // 初始化外设
    if (!initCamera()) {
        Serial.println("[FATAL] camera init failed!");
    }
    if (!initMicrophone()) {
        Serial.println("[FATAL] mic init failed!");
    }
    if (!initSpeaker()) {
        Serial.println("[FATAL] speaker init failed!");
    }

    // 连接 WiFi
    connectWiFi();

#ifdef USE_HTTPS
    initTLS();
#endif

    // 启动提示音
    playPcmBeep(1000, 200);

    Serial.println("\n[READY] Door Guardian active, press button to trigger...\n");
}

// ==================== Arduino Loop ====================
void loop() {
    // 按键触发检测（按下 = LOW，内部上拉）
    if (digitalRead(TRIGGER_PIN) == LOW) {
        unsigned long now = millis();
        if (now - g_lastTrigger > TRIGGER_DEBOUNCE_MS) {
            g_lastTrigger = now;
            processEvent();
        }
    }

    // WiFi 断线重连
    static unsigned long lastWiFiCheck = 0;
    if (millis() - lastWiFiCheck > 10000) {
        lastWiFiCheck = millis();
        if (WiFi.status() != WL_CONNECTED) {
            Serial.println("[WIFI] disconnected, reconnecting...");
            connectWiFi();
        }
    }

    delay(50);
}
