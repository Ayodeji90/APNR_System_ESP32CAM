/**
 * ANPR Gate System — ESP32-CAM Edge Firmware
 * ===========================================
 *
 * Hardware:  ESP32-CAM (AI-Thinker) + HC-SR04 + Servo + Relay
 * Platform:  PlatformIO (see platformio.ini — board: esp32cam)
 *            Compatible with Arduino-ESP32 core 2.x and 3.x.
 *
 * Build & flash:
 *   pio run                 # compile
 *   pio run -t upload       # flash (GPIO 0 → GND + press RST first)
 *   pio device monitor      # serial log (remove GPIO 0 jumper + RST)
 *
 * Endpoints exposed on the local network (port 80):
 *   GET  /stream          MJPEG live stream (multipart/x-mixed-replace)
 *   GET  /capture         Single JPEG snapshot
 *   GET  /distance        HC-SR04 distance reading → JSON {"distance_cm": 42.5}
 *   POST /barrier/open    Move servo to open angle + activate relay
 *   POST /barrier/close   Move servo to closed angle + deactivate relay
 *   GET  /status          Device info JSON (uptime, free heap, WiFi RSSI)
 *
 * Security:
 *   Set API_KEY below to a non-empty secret. When set, the barrier
 *   endpoints require either an "X-Api-Key: <key>" header or a
 *   "?key=<key>" query parameter. Leave empty to disable (LAN-only use!).
 *
 * Wiring:
 *   HC-SR04 TRIG → GPIO 12
 *   HC-SR04 ECHO → GPIO 13 (through 1kΩ/2kΩ voltage divider — 5V → 3.3V)
 *   Servo signal → GPIO 14
 *   Relay control → GPIO 15
 *   (ESP32-CAM built-in LED flash → GPIO 4, optional)
 */

#include <Arduino.h>

#include <memory>

#include <WiFi.h>
#include <ESPAsyncWebServer.h>
#include <AsyncTCP.h>
#include <ESPmDNS.h>

#include "esp_camera.h"

// ── Camera pin map — AI-Thinker ESP32-CAM ───────────────────
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// ── Configuration ────────────────────────────────────────────
// WiFi credentials — CHANGE THESE for your network
const char* WIFI_SSID     = "Airtel_W304VA PRO_7E5A_5G";
const char* WIFI_PASSWORD = "07048283747";

// Shared secret for the barrier endpoints. STRONGLY recommended:
// set this and send the same value from the server as an
// "X-Api-Key" header. Empty string = authentication disabled.
const char* API_KEY = "3b01f25b914defb99bbb4aaca51f1f2de7fb7c66e3c47116";

// Static IP (optional — comment out to use DHCP)
// IPAddress local_IP(192, 168, 1, 200);
// IPAddress gateway(192, 168, 1, 1);
// IPAddress subnet(255, 255, 255, 0);

// mDNS hostname (access via http://anpr-gate.local)
const char* MDNS_HOSTNAME = "anpr-gate";

// ── Pin definitions ─────────────────────────────────────────
#define PIN_HCSR04_TRIG   12
#define PIN_HCSR04_ECHO   13
#define PIN_SERVO         14
#define PIN_RELAY         15
#define PIN_LED_FLASH      4   // ESP32-CAM built-in LED (optional)

// ── Servo angles ────────────────────────────────────────────
#define SERVO_OPEN_ANGLE   90
#define SERVO_CLOSED_ANGLE  0

// The camera driver claims LEDC channel 0 / timer 0 for XCLK,
// so the servo must live on a different channel AND timer.
// Channels 2/3 share timer 1 on the ESP32 → channel 2 is safe.
#define SERVO_LEDC_CHANNEL  2

// ── HC-SR04 constants ───────────────────────────────────────
#define SOUND_SPEED_CM_US  0.0343f   // cm per microsecond
#define MAX_DISTANCE_CM    400.0f    // sensor max range
#define DISTANCE_TIMEOUT_US 25000    // ~4.3m round-trip timeout
#define DISTANCE_POLL_INTERVAL_MS 100  // sensor polled from loop()

// ── Camera settings ─────────────────────────────────────────
#define CAMERA_FRAME_SIZE  FRAMESIZE_SVGA  // 800x600
#define CAMERA_JPEG_QUALITY 12             // 0-63, lower = better

// ── Globals ──────────────────────────────────────────────────
AsyncWebServer server(80);
unsigned long bootMillis = 0;
bool cameraOk = false;

// State shared between HTTP handlers (async_tcp task) and loop().
// Handlers only set flags / read cached values — all blocking work
// (servo delay, HC-SR04 busy-wait) happens in loop().
enum BarrierCommand : uint8_t { CMD_NONE = 0, CMD_OPEN, CMD_CLOSE };
volatile uint8_t pendingBarrierCmd = CMD_NONE;
volatile bool barrierOpen = false;
volatile float lastDistanceCm = MAX_DISTANCE_CM;

// ── Forward declarations ────────────────────────────────────
float readDistanceCm();
void setServoAngle(int angle);
void setRelay(bool on);
void setupRoutes();

// ── LEDC compatibility (Arduino-ESP32 core 2.x vs 3.x) ──────
// Core 3.x removed ledcSetup/ledcAttachPin and made ledcWrite
// take the pin instead of the channel.
static void servoPwmInit() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttachChannel(PIN_SERVO, 50, 16, SERVO_LEDC_CHANNEL);
#else
    ledcSetup(SERVO_LEDC_CHANNEL, 50, 16);   // 50 Hz, 16-bit resolution
    ledcAttachPin(PIN_SERVO, SERVO_LEDC_CHANNEL);
#endif
}

static void servoPwmWrite(uint32_t duty) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(PIN_SERVO, duty);
#else
    ledcWrite(SERVO_LEDC_CHANNEL, duty);
#endif
}

// ═══════════════════════════════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════════════════════════════

void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("\n\n=== ANPR ESP32-CAM Edge Firmware ===");

    // ── GPIO init ───────────────────────────────────────────
    pinMode(PIN_HCSR04_TRIG, OUTPUT);
    pinMode(PIN_HCSR04_ECHO, INPUT);
    digitalWrite(PIN_HCSR04_TRIG, LOW);

    pinMode(PIN_RELAY, OUTPUT);
    digitalWrite(PIN_RELAY, LOW);

    // Optional flash LED off
    pinMode(PIN_LED_FLASH, OUTPUT);
    digitalWrite(PIN_LED_FLASH, LOW);

    // ── Camera init (before servo — claims LEDC ch0/timer0) ─
    camera_config_t config = {};
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer   = LEDC_TIMER_0;
    config.pin_d0       = Y2_GPIO_NUM;
    config.pin_d1       = Y3_GPIO_NUM;
    config.pin_d2       = Y4_GPIO_NUM;
    config.pin_d3       = Y5_GPIO_NUM;
    config.pin_d4       = Y6_GPIO_NUM;
    config.pin_d5       = Y7_GPIO_NUM;
    config.pin_d6       = Y8_GPIO_NUM;
    config.pin_d7       = Y9_GPIO_NUM;
    config.pin_xclk     = XCLK_GPIO_NUM;
    config.pin_pclk     = PCLK_GPIO_NUM;
    config.pin_vsync    = VSYNC_GPIO_NUM;
    config.pin_href     = HREF_GPIO_NUM;
    config.pin_sscb_sda = SIOD_GPIO_NUM;
    config.pin_sscb_scl = SIOC_GPIO_NUM;
    config.pin_pwdn     = PWDN_GPIO_NUM;
    config.pin_reset    = RESET_GPIO_NUM;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    config.jpeg_quality = CAMERA_JPEG_QUALITY;

    if (psramFound()) {
        config.frame_size  = CAMERA_FRAME_SIZE;
        config.fb_count    = 2;
        config.fb_location = CAMERA_FB_IN_PSRAM;
        config.grab_mode   = CAMERA_GRAB_LATEST;   // always serve the freshest frame
    } else {
        // No PSRAM: SVGA double-buffering won't fit in DRAM
        config.frame_size  = FRAMESIZE_VGA;
        config.fb_count    = 1;
        config.fb_location = CAMERA_FB_IN_DRAM;
        config.grab_mode   = CAMERA_GRAB_WHEN_EMPTY;
    }

    esp_err_t camErr = esp_camera_init(&config);
    cameraOk = (camErr == ESP_OK);
    if (!cameraOk) {
        Serial.printf("Camera init failed: 0x%x\n", camErr);
    } else {
        Serial.println("Camera initialised OK");
    }

    // Servo attached via LEDC PWM — channel 2 (camera owns channel 0)
    servoPwmInit();
    setServoAngle(SERVO_CLOSED_ANGLE);

    // ── WiFi ────────────────────────────────────────────────
    WiFi.mode(WIFI_STA);
    // Optional static IP
    // WiFi.config(local_IP, gateway, subnet);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    Serial.print("Connecting to WiFi");
    int wifiAttempts = 0;
    while (WiFi.status() != WL_CONNECTED && wifiAttempts < 40) {
        delay(500);
        Serial.print(".");
        wifiAttempts++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\nWiFi connected");
        Serial.print("IP: ");
        Serial.println(WiFi.localIP());
        Serial.print("RSSI: ");
        Serial.println(WiFi.RSSI());

        // mDNS
        if (MDNS.begin(MDNS_HOSTNAME)) {
            Serial.printf("mDNS: http://%s.local\n", MDNS_HOSTNAME);
        }
    } else {
        Serial.println("\nWiFi FAILED — continuing without network");
    }

    if (strlen(API_KEY) == 0) {
        Serial.println("WARNING: API_KEY is empty — barrier endpoints are UNAUTHENTICATED.");
    }

    // ── HTTP routes ─────────────────────────────────────────
    setupRoutes();

    server.begin();
    bootMillis = millis();
    Serial.println("HTTP server started on port 80");
    Serial.println("=== Ready ===\n");
}

// ═══════════════════════════════════════════════════════════════
//  AUTH GUARD
// ═══════════════════════════════════════════════════════════════

// Returns true if the request is authorized. Sends a 401 and
// returns false otherwise. No-op (always true) when API_KEY is empty.
static bool checkApiKey(AsyncWebServerRequest* request) {
    if (strlen(API_KEY) == 0) return true;
    if (request->hasHeader("X-Api-Key")
        && request->getHeader("X-Api-Key")->value() == API_KEY) {
        return true;
    }
    if (request->hasParam("key")
        && request->getParam("key")->value() == API_KEY) {
        return true;
    }
    request->send(401, "application/json", "{\"error\": \"unauthorized\"}");
    return false;
}

// ═══════════════════════════════════════════════════════════════
//  MJPEG STREAM STATE
// ═══════════════════════════════════════════════════════════════

// Per-client state for the chunked MJPEG stream. A full SVGA JPEG
// (20-40 KB) never fits in one AsyncWebServer chunk buffer (~1.4-5.7 KB),
// so each frame is sent across many chunk callbacks: the state tracks
// how much of the current part (header + JPEG body) has gone out.
struct MjpegStreamState {
    camera_fb_t* fb = nullptr;
    char header[96];
    size_t headerLen = 0;
    size_t headerSent = 0;
    size_t bodySent = 0;

    ~MjpegStreamState() {
        // Client disconnected mid-frame — return the buffer to the driver
        if (fb) esp_camera_fb_return(fb);
    }
};

// Fill one chunk of the MJPEG stream. Returns bytes written;
// returning 0 ends the stream (camera failure).
static size_t fillStreamChunk(MjpegStreamState* st, uint8_t* buffer, size_t maxLen) {
    // Start a new part if no frame is in flight
    if (st->fb == nullptr) {
        st->fb = esp_camera_fb_get();
        if (st->fb == nullptr) {
            return 0;  // camera failure — terminate the stream
        }
        // Leading CRLF terminates the previous part (harmless preamble
        // before the very first boundary), so no trailer state is needed.
        st->headerLen = snprintf(
            st->header, sizeof(st->header),
            "\r\n--FRAME_BOUNDARY\r\n"
            "Content-Type: image/jpeg\r\n"
            "Content-Length: %u\r\n"
            "\r\n",
            (unsigned)st->fb->len
        );
        st->headerSent = 0;
        st->bodySent = 0;
    }

    size_t written = 0;

    // Send any remaining header bytes
    if (st->headerSent < st->headerLen) {
        size_t n = std::min(maxLen, st->headerLen - st->headerSent);
        memcpy(buffer, st->header + st->headerSent, n);
        st->headerSent += n;
        written = n;
        if (written == maxLen) return written;
    }

    // Send the next slice of the JPEG body
    size_t remaining = st->fb->len - st->bodySent;
    size_t n = std::min(maxLen - written, remaining);
    memcpy(buffer + written, st->fb->buf + st->bodySent, n);
    st->bodySent += n;
    written += n;

    // Frame complete — release it; next callback grabs a fresh one
    if (st->bodySent == st->fb->len) {
        esp_camera_fb_return(st->fb);
        st->fb = nullptr;
    }

    return written;
}

// ═══════════════════════════════════════════════════════════════
//  ROUTES
// ═══════════════════════════════════════════════════════════════

void setupRoutes() {

    // ── MJPEG Stream ────────────────────────────────────────
    server.on("/stream", HTTP_GET, [](AsyncWebServerRequest* request) {
        if (!cameraOk) {
            request->send(503, "application/json", "{\"error\": \"camera not initialised\"}");
            return;
        }
        // shared_ptr keeps the state alive for the lifetime of the
        // response and its destructor returns any in-flight frame.
        auto state = std::make_shared<MjpegStreamState>();
        AsyncWebServerResponse* response = request->beginChunkedResponse(
            "multipart/x-mixed-replace; boundary=FRAME_BOUNDARY",
            [state](uint8_t* buffer, size_t maxLen, size_t index) -> size_t {
                return fillStreamChunk(state.get(), buffer, maxLen);
            }
        );
        response->addHeader("Access-Control-Allow-Origin", "*");
        request->send(response);
    });

    // ── Single JPEG Capture ─────────────────────────────────
    server.on("/capture", HTTP_GET, [](AsyncWebServerRequest* request) {
        if (!cameraOk) {
            request->send(503, "application/json", "{\"error\": \"camera not initialised\"}");
            return;
        }
        camera_fb_t* fb = esp_camera_fb_get();
        if (!fb) {
            request->send(500, "text/plain", "Camera capture failed");
            return;
        }

        // AsyncWebServer transmits AFTER this handler returns, so the
        // framebuffer must be copied — returning it here and letting the
        // response reference fb->buf would be a use-after-free.
        size_t len = fb->len;
        uint8_t* copy = (uint8_t*)ps_malloc(len);
        if (copy == nullptr) copy = (uint8_t*)malloc(len);
        if (copy == nullptr) {
            esp_camera_fb_return(fb);
            request->send(500, "text/plain", "Out of memory");
            return;
        }
        memcpy(copy, fb->buf, len);
        esp_camera_fb_return(fb);

        AsyncWebServerResponse* response = request->beginResponse(
            "image/jpeg", len,
            [copy, len](uint8_t* buffer, size_t maxLen, size_t index) -> size_t {
                size_t n = std::min(maxLen, len - index);
                memcpy(buffer, copy + index, n);
                return n;
            }
        );
        response->addHeader("Access-Control-Allow-Origin", "*");
        request->onDisconnect([copy]() { free(copy); });
        request->send(response);
    });

    // ── Distance (HC-SR04) ──────────────────────────────────
    // Returns the cached reading polled from loop() — the busy-wait
    // measurement must not run on the async_tcp task.
    server.on("/distance", HTTP_GET, [](AsyncWebServerRequest* request) {
        char json[64];
        snprintf(json, sizeof(json), "{\"distance_cm\": %.1f}", (float)lastDistanceCm);
        AsyncWebServerResponse* response = request->beginResponse(
            200, "application/json", json
        );
        response->addHeader("Access-Control-Allow-Origin", "*");
        request->send(response);
    });

    // ── Barrier Open ────────────────────────────────────────
    // Handlers only queue the command; the servo move (with its
    // 400 ms settle delay) runs in loop().
    server.on("/barrier/open", HTTP_POST, [](AsyncWebServerRequest* request) {
        if (!checkApiKey(request)) return;
        pendingBarrierCmd = CMD_OPEN;
        Serial.println("Barrier OPEN queued");
        request->send(200, "application/json", "{\"status\": \"open\"}");
    });

    // ── Barrier Close ───────────────────────────────────────
    server.on("/barrier/close", HTTP_POST, [](AsyncWebServerRequest* request) {
        if (!checkApiKey(request)) return;
        pendingBarrierCmd = CMD_CLOSE;
        Serial.println("Barrier CLOSE queued");
        request->send(200, "application/json", "{\"status\": \"closed\"}");
    });

    // ── Device Status ───────────────────────────────────────
    server.on("/status", HTTP_GET, [](AsyncWebServerRequest* request) {
        unsigned long uptime = (millis() - bootMillis) / 1000;
        char json[256];
        snprintf(json, sizeof(json),
            "{"
            "\"uptime_sec\": %lu,"
            "\"free_heap\": %u,"
            "\"wifi_rssi\": %d,"
            "\"wifi_ip\": \"%s\","
            "\"barrier_open\": %s,"
            "\"camera_ok\": %s"
            "}",
            uptime,
            (unsigned)ESP.getFreeHeap(),
            WiFi.RSSI(),
            WiFi.localIP().toString().c_str(),
            barrierOpen ? "true" : "false",
            cameraOk ? "true" : "false"
        );
        AsyncWebServerResponse* response = request->beginResponse(
            200, "application/json", json
        );
        response->addHeader("Access-Control-Allow-Origin", "*");
        request->send(response);
    });

    // ── 404 catch-all ───────────────────────────────────────
    server.onNotFound([](AsyncWebServerRequest* request) {
        request->send(404, "application/json", "{\"error\": \"not found\"}");
    });
}

// ═══════════════════════════════════════════════════════════════
//  HC-SR04 DISTANCE SENSOR
// ═══════════════════════════════════════════════════════════════

float readDistanceCm() {
    // Send 10µs trigger pulse
    digitalWrite(PIN_HCSR04_TRIG, LOW);
    delayMicroseconds(2);
    digitalWrite(PIN_HCSR04_TRIG, HIGH);
    delayMicroseconds(10);
    digitalWrite(PIN_HCSR04_TRIG, LOW);

    // Measure echo pulse duration with timeout
    unsigned long pulseStart = micros();
    unsigned long timeoutStart = pulseStart;

    // Wait for echo HIGH
    while (digitalRead(PIN_HCSR04_ECHO) == LOW) {
        if (micros() - timeoutStart > DISTANCE_TIMEOUT_US) {
            return MAX_DISTANCE_CM;  // no echo — object too far or sensor fault
        }
    }

    pulseStart = micros();

    // Wait for echo LOW
    while (digitalRead(PIN_HCSR04_ECHO) == HIGH) {
        if (micros() - pulseStart > DISTANCE_TIMEOUT_US) {
            return MAX_DISTANCE_CM;
        }
    }

    unsigned long pulseDuration = micros() - pulseStart;
    float distance = pulseDuration * SOUND_SPEED_CM_US / 2.0f;

    if (distance > MAX_DISTANCE_CM) {
        distance = MAX_DISTANCE_CM;
    }

    return distance;
}

// ═══════════════════════════════════════════════════════════════
//  SERVO CONTROL (LEDC PWM, 50 Hz)
// ═══════════════════════════════════════════════════════════════

void setServoAngle(int angle) {
    // SG90/MG996R: 0° ≈ 500µs pulse, 180° ≈ 2400µs pulse
    // 50 Hz → period = 20000µs
    // duty = (500 + angle * (1900/180)) / 20000 * 65536
    int angleClamped = constrain(angle, 0, 180);
    int pulseUs = map(angleClamped, 0, 180, 500, 2400);
    uint32_t duty = (uint32_t)pulseUs * 65536 / 20000;
    servoPwmWrite(duty);
    delay(400);  // allow servo to reach position
    servoPwmWrite(0);  // stop PWM to prevent jitter
}

// ═══════════════════════════════════════════════════════════════
//  RELAY CONTROL
// ═══════════════════════════════════════════════════════════════

void setRelay(bool on) {
    digitalWrite(PIN_RELAY, on ? HIGH : LOW);
}

// ═══════════════════════════════════════════════════════════════
//  MAIN LOOP
// ═══════════════════════════════════════════════════════════════
// All blocking work lives here: HC-SR04 polling, servo moves
// queued by the HTTP handlers, and the periodic heartbeat.

void loop() {
    static unsigned long lastDistancePollMs = 0;
    static unsigned long lastHeartbeatMs = 0;

    // ── Execute queued barrier commands ─────────────────────
    uint8_t cmd = pendingBarrierCmd;
    if (cmd != CMD_NONE) {
        pendingBarrierCmd = CMD_NONE;
        if (cmd == CMD_OPEN) {
            setServoAngle(SERVO_OPEN_ANGLE);
            setRelay(true);
            barrierOpen = true;
            Serial.println("Barrier OPENED");
        } else if (cmd == CMD_CLOSE) {
            setServoAngle(SERVO_CLOSED_ANGLE);
            setRelay(false);
            barrierOpen = false;
            Serial.println("Barrier CLOSED");
        }
    }

    // ── Poll HC-SR04 into the cached reading ────────────────
    if (millis() - lastDistancePollMs >= DISTANCE_POLL_INTERVAL_MS) {
        lastDistancePollMs = millis();
        lastDistanceCm = readDistanceCm();
    }

    // ── 1-minute heartbeat ──────────────────────────────────
    if (millis() - lastHeartbeatMs >= 60000) {
        lastHeartbeatMs = millis();
        if (WiFi.status() == WL_CONNECTED) {
            Serial.printf("[HEARTBEAT] uptime=%lus  heap=%u  rssi=%d  barrier=%s  dist=%.1fcm\n",
                (millis() - bootMillis) / 1000,
                (unsigned)ESP.getFreeHeap(),
                WiFi.RSSI(),
                barrierOpen ? "open" : "closed",
                (float)lastDistanceCm
            );
        }
    }

    delay(10);
}
