/**
 * ANPR Gate System — ESP32-CAM Edge Firmware (Push Model)
 * ========================================================
 *
 * Architecture: the ESP32 detects vehicles locally (HC-SR04),
 * captures a JPEG, and POSTs it to the cloud server for ANPR
 * processing.  The server responds with open/deny/unknown, and
 * the ESP32 actuates the barrier accordingly.
 *
 * No port forwarding, DuckDNS, or special router config needed —
 * the ESP32 only makes outbound HTTP requests.
 *
 * Hardware:  ESP32-CAM (AI-Thinker) + HC-SR04 + Servo + Relay
 * Platform:  PlatformIO (see platformio.ini — board: esp32cam)
 *
 * Build & flash:
 *   pio run                 # compile
 *   pio run -t upload       # flash (GPIO 0 → GND + press RST first)
 *   pio device monitor      # serial log (remove GPIO 0 jumper + RST)
 *
 * Wiring:
 *   HC-SR04 TRIG → GPIO 12
 *   HC-SR04 ECHO → GPIO 13 (through 1kΩ/2kΩ voltage divider — 5V → 3.3V)
 *   Servo signal → GPIO 14
 *   Relay control → GPIO 15
 *   (ESP32-CAM built-in LED flash → GPIO 4, optional)
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
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
const char* WIFI_SSID     = "Raspberrypi";
const char* WIFI_PASSWORD = "08084835102";

// Server URL — your GCP VM's public IP or domain
// This is where the ESP32 POSTs images and heartbeats.
const char* SERVER_BASE_URL = "http://136.115.40.189:5000";

// API key — must match esp32.api_key in the server's config.yaml
const char* API_KEY = "3b01f25b914defb99bbb4aaca51f1f2de7fb7c66e3c47116";

// ── Detection thresholds ────────────────────────────────────
#define DISTANCE_THRESHOLD_CM  50     // vehicle detected below this
#define CONFIRMATION_READINGS   3     // consecutive readings to confirm
#define DETECTION_COOLDOWN_MS  15000  // min time between detections

// ── Timing ──────────────────────────────────────────────────
#define HEARTBEAT_INTERVAL_MS  30000  // heartbeat every 30 seconds
#define BARRIER_OPEN_DURATION_MS 10000 // auto-close after 10 seconds
#define HTTP_TIMEOUT_MS        15000  // HTTP request timeout
#define WIFI_RECONNECT_DELAY_MS 5000  // delay before WiFi reconnect

// ── Pin definitions ─────────────────────────────────────────
#define PIN_HCSR04_TRIG   12
#define PIN_HCSR04_ECHO   13
#define PIN_SERVO         14
#define PIN_RELAY         15
#define PIN_LED_FLASH      4

// ── Servo angles ────────────────────────────────────────────
#define SERVO_OPEN_ANGLE   90
#define SERVO_CLOSED_ANGLE  0

// The camera driver claims LEDC channel 0 / timer 0 for XCLK,
// so the servo must live on a different channel AND timer.
#define SERVO_LEDC_CHANNEL  2

// ── HC-SR04 constants ───────────────────────────────────────
#define SOUND_SPEED_CM_US  0.0343f
#define MAX_DISTANCE_CM    400.0f
#define DISTANCE_TIMEOUT_US 25000
#define DISTANCE_POLL_INTERVAL_MS 100

// ── Camera settings ─────────────────────────────────────────
#define CAMERA_FRAME_SIZE  FRAMESIZE_SVGA  // 800x600
#define CAMERA_JPEG_QUALITY 12

// ── Globals ──────────────────────────────────────────────────
unsigned long bootMillis = 0;
bool cameraOk = false;
bool barrierOpen = false;
float lastDistanceCm = MAX_DISTANCE_CM;
unsigned long lastHeartbeatMs = 0;
unsigned long lastDetectionMs = 0;
unsigned long barrierOpenedAtMs = 0;

// ── Forward declarations ────────────────────────────────────
float readDistanceCm();
void setServoAngle(int angle);
void setRelay(bool on);
void openBarrier();
void closeBarrier();
bool detectVehicle();
void sendHeartbeat();
void sendDetection(camera_fb_t* fb, float distanceCm);
void processServerCommands(const char* json);
void ensureWiFi();

// ── LEDC compatibility (Arduino-ESP32 core 2.x vs 3.x) ──────
static void servoPwmInit() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttachChannel(PIN_SERVO, 50, 16, SERVO_LEDC_CHANNEL);
#else
    ledcSetup(SERVO_LEDC_CHANNEL, 50, 16);
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
    Serial.println("\n\n=== ANPR ESP32-CAM — Push Model ===");

    // ── GPIO init ───────────────────────────────────────────
    pinMode(PIN_HCSR04_TRIG, OUTPUT);
    pinMode(PIN_HCSR04_ECHO, INPUT);
    digitalWrite(PIN_HCSR04_TRIG, LOW);

    pinMode(PIN_RELAY, OUTPUT);
    digitalWrite(PIN_RELAY, LOW);

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
        config.grab_mode   = CAMERA_GRAB_LATEST;
    } else {
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

    // Servo init
    servoPwmInit();
    setServoAngle(SERVO_CLOSED_ANGLE);

    // ── WiFi ────────────────────────────────────────────────
    WiFi.mode(WIFI_STA);
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
    } else {
        Serial.println("\nWiFi FAILED — will retry in loop");
    }

    bootMillis = millis();
    Serial.printf("Server: %s\n", SERVER_BASE_URL);
    Serial.printf("Detection threshold: %dcm, confirmations: %d\n",
                  DISTANCE_THRESHOLD_CM, CONFIRMATION_READINGS);
    Serial.println("=== Ready — Push Model Active ===\n");
}

// ═══════════════════════════════════════════════════════════════
//  HC-SR04 DISTANCE SENSOR
// ═══════════════════════════════════════════════════════════════

float readDistanceCm() {
    digitalWrite(PIN_HCSR04_TRIG, LOW);
    delayMicroseconds(2);
    digitalWrite(PIN_HCSR04_TRIG, HIGH);
    delayMicroseconds(10);
    digitalWrite(PIN_HCSR04_TRIG, LOW);

    unsigned long timeoutStart = micros();
    while (digitalRead(PIN_HCSR04_ECHO) == LOW) {
        if (micros() - timeoutStart > DISTANCE_TIMEOUT_US) {
            return MAX_DISTANCE_CM;
        }
    }

    unsigned long pulseStart = micros();
    while (digitalRead(PIN_HCSR04_ECHO) == HIGH) {
        if (micros() - pulseStart > DISTANCE_TIMEOUT_US) {
            return MAX_DISTANCE_CM;
        }
    }

    unsigned long pulseDuration = micros() - pulseStart;
    float distance = pulseDuration * SOUND_SPEED_CM_US / 2.0f;
    if (distance > MAX_DISTANCE_CM) distance = MAX_DISTANCE_CM;
    return distance;
}

// ═══════════════════════════════════════════════════════════════
//  SERVO CONTROL (LEDC PWM, 50 Hz)
// ═══════════════════════════════════════════════════════════════

void setServoAngle(int angle) {
    int angleClamped = constrain(angle, 0, 180);
    int pulseUs = map(angleClamped, 0, 180, 500, 2400);
    uint32_t duty = (uint32_t)pulseUs * 65536 / 20000;
    servoPwmWrite(duty);
    delay(400);
    servoPwmWrite(0);
}

// ═══════════════════════════════════════════════════════════════
//  RELAY CONTROL
// ═══════════════════════════════════════════════════════════════

void setRelay(bool on) {
    digitalWrite(PIN_RELAY, on ? HIGH : LOW);
}

// ═══════════════════════════════════════════════════════════════
//  BARRIER CONTROL
// ═══════════════════════════════════════════════════════════════

void openBarrier() {
    if (!barrierOpen) {
        Serial.println("Opening barrier …");
        setServoAngle(SERVO_OPEN_ANGLE);
        setRelay(true);
        barrierOpen = true;
        barrierOpenedAtMs = millis();
        Serial.println("Barrier OPENED");
    }
}

void closeBarrier() {
    if (barrierOpen) {
        Serial.println("Closing barrier …");
        setServoAngle(SERVO_CLOSED_ANGLE);
        setRelay(false);
        barrierOpen = false;
        Serial.println("Barrier CLOSED");
    }
}

// ═══════════════════════════════════════════════════════════════
//  VEHICLE DETECTION (LOCAL)
// ═══════════════════════════════════════════════════════════════

bool detectVehicle() {
    int consecutive = 0;
    for (int i = 0; i < CONFIRMATION_READINGS + 2; i++) {
        float dist = readDistanceCm();
        if (dist < DISTANCE_THRESHOLD_CM) {
            consecutive++;
            if (consecutive >= CONFIRMATION_READINGS) {
                Serial.printf("Vehicle detected at %.1f cm\n", dist);
                return true;
            }
        } else {
            consecutive = 0;
        }
        delay(100);
    }
    return false;
}

// ═══════════════════════════════════════════════════════════════
//  WIFI RECONNECTION
// ═══════════════════════════════════════════════════════════════

void ensureWiFi() {
    if (WiFi.status() == WL_CONNECTED) return;

    Serial.println("WiFi disconnected — reconnecting …");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 20) {
        delay(500);
        Serial.print(".");
        attempts++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\nWiFi reconnected");
        Serial.print("IP: ");
        Serial.println(WiFi.localIP());
    } else {
        Serial.println("\nWiFi reconnect failed — will retry later");
    }
}

// ═══════════════════════════════════════════════════════════════
//  SERVER COMMUNICATION
// ═══════════════════════════════════════════════════════════════

void sendDetection(camera_fb_t* fb, float distanceCm) {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("Cannot send detection — WiFi offline");
        return;
    }

    HTTPClient http;
    String url = String(SERVER_BASE_URL) + "/api/detect";

    http.begin(url);
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.addHeader("X-Api-Key", API_KEY);

    // Build multipart/form-data
    String boundary = "----ESP32Boundary";
    String contentType = "multipart/form-data; boundary=" + boundary;
    http.addHeader("Content-Type", contentType);

    // Build the body
    String bodyStart = "--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"distance_cm\"\r\n\r\n"
        + String(distanceCm, 1) + "\r\n"
        "--" + boundary + "\r\n"
        "Content-Disposition: form-data; name=\"image\"; filename=\"capture.jpg\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n";
    String bodyEnd = "\r\n--" + boundary + "--\r\n";

    size_t totalLen = bodyStart.length() + fb->len + bodyEnd.length();

    // Allocate buffer
    uint8_t* payload = (uint8_t*)ps_malloc(totalLen);
    if (!payload) payload = (uint8_t*)malloc(totalLen);
    if (!payload) {
        Serial.println("Out of memory for HTTP payload");
        http.end();
        return;
    }

    size_t offset = 0;
    memcpy(payload + offset, bodyStart.c_str(), bodyStart.length());
    offset += bodyStart.length();
    memcpy(payload + offset, fb->buf, fb->len);
    offset += fb->len;
    memcpy(payload + offset, bodyEnd.c_str(), bodyEnd.length());

    Serial.printf("Sending detection to server (%d bytes) …\n", totalLen);
    int httpCode = http.POST(payload, totalLen);
    free(payload);

    if (httpCode == 200) {
        String response = http.getString();
        Serial.printf("Server response: %s\n", response.c_str());

        // Parse JSON response
        JsonDocument doc;
        DeserializationError err = deserializeJson(doc, response);
        if (!err) {
            const char* action = doc["action"] | "unknown";
            const char* plate = doc["plate"] | "";
            const char* reason = doc["reason"] | "";

            Serial.printf("Action: %s  Plate: %s  Reason: %s\n", action, plate, reason);

            if (strcmp(action, "open") == 0) {
                openBarrier();
            }
        } else {
            Serial.printf("JSON parse error: %s\n", err.c_str());
        }
    } else {
        Serial.printf("HTTP error: %d\n", httpCode);
        if (httpCode > 0) {
            Serial.println(http.getString());
        }
    }

    http.end();
}


void sendHeartbeat() {
    if (WiFi.status() != WL_CONNECTED) return;

    HTTPClient http;
    String url = String(SERVER_BASE_URL) + "/api/heartbeat";

    http.begin(url);
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Api-Key", API_KEY);

    // Build heartbeat JSON
    JsonDocument doc;
    doc["uptime_sec"] = (millis() - bootMillis) / 1000;
    doc["free_heap"]  = ESP.getFreeHeap();
    doc["wifi_rssi"]  = WiFi.RSSI();
    doc["barrier_open"] = barrierOpen;
    doc["distance_cm"]  = lastDistanceCm;

    String body;
    serializeJson(doc, body);

    int httpCode = http.POST(body);
    if (httpCode == 200) {
        String response = http.getString();

        // Parse response for pending commands
        JsonDocument respDoc;
        DeserializationError err = deserializeJson(respDoc, response);
        if (!err) {
            JsonArray commands = respDoc["commands"].as<JsonArray>();
            for (JsonObject cmd : commands) {
                const char* command = cmd["command"] | "";
                int cmdId = cmd["id"] | 0;
                Serial.printf("Pending command: %s (id=%d)\n", command, cmdId);

                if (strcmp(command, "open") == 0) {
                    openBarrier();
                } else if (strcmp(command, "close") == 0) {
                    closeBarrier();
                }
            }
        }
    } else {
        Serial.printf("Heartbeat failed: HTTP %d\n", httpCode);
    }

    http.end();
}

// ═══════════════════════════════════════════════════════════════
//  MAIN LOOP
// ═══════════════════════════════════════════════════════════════

void loop() {
    static unsigned long lastDistancePollMs = 0;

    // ── Ensure WiFi is connected ────────────────────────────
    ensureWiFi();

    // ── Auto-close barrier after timeout ────────────────────
    if (barrierOpen && (millis() - barrierOpenedAtMs >= BARRIER_OPEN_DURATION_MS)) {
        Serial.println("Auto-close timer fired");
        closeBarrier();
    }

    // ── Poll distance sensor ────────────────────────────────
    if (millis() - lastDistancePollMs >= DISTANCE_POLL_INTERVAL_MS) {
        lastDistancePollMs = millis();
        lastDistanceCm = readDistanceCm();
    }

    // ── Vehicle detection → capture → send to server ────────
    if (!barrierOpen &&
        (millis() - lastDetectionMs >= DETECTION_COOLDOWN_MS) &&
        lastDistanceCm < DISTANCE_THRESHOLD_CM)
    {
        // Confirm with multiple readings
        if (detectVehicle()) {
            Serial.println("▶ Vehicle confirmed — capturing image …");
            lastDetectionMs = millis();

            if (cameraOk) {
                // Flash LED briefly for better capture
                digitalWrite(PIN_LED_FLASH, HIGH);
                delay(100);

                camera_fb_t* fb = esp_camera_fb_get();
                digitalWrite(PIN_LED_FLASH, LOW);

                if (fb) {
                    Serial.printf("Captured %d bytes JPEG\n", fb->len);
                    sendDetection(fb, lastDistanceCm);
                    esp_camera_fb_return(fb);
                } else {
                    Serial.println("Camera capture failed");
                }
            } else {
                Serial.println("Camera not available — skipping detection");
            }
        }
    }

    // ── Periodic heartbeat ──────────────────────────────────
    if (millis() - lastHeartbeatMs >= HEARTBEAT_INTERVAL_MS) {
        lastHeartbeatMs = millis();
        sendHeartbeat();

        // Log status
        Serial.printf("[HEARTBEAT] uptime=%lus  heap=%u  rssi=%d  barrier=%s  dist=%.1fcm\n",
            (millis() - bootMillis) / 1000,
            (unsigned)ESP.getFreeHeap(),
            WiFi.RSSI(),
            barrierOpen ? "open" : "closed",
            lastDistanceCm
        );
    }

    delay(10);
}
