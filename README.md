# 🚗 ANPR Gate System

**Project Status** (as of 2026‑07‑05)

- ✅ Config files updated for ESP32‑CAM
- ✅ Camera, sensor, and actuator services rewritten to use HTTP
- ✅ ESP32‑CAM Arduino sketch created and documented
- ✅ Systemd service files updated to use the virtual‑environment Python interpreter
- ✅ Application imports verified (`state_machine` and `main` load without errors)
- 📌 Remaining tasks: run end‑to‑end integration test and enable the services

**Automatic Number Plate Recognition** gate controller for **Raspberry Pi 3B+** with Pi Camera V2, HC-SR04 ultrasonic sensor, servo motor, and relay module.

> Detect vehicle → capture image → detect plate → read characters → decide Allow/Deny → open barrier → log event.

---

## Features

- **Real-time plate detection** using OpenCV contour analysis
- **OCR** via Tesseract with confidence scoring
- **Event-driven state machine** (IDLE → TRIGGERED → CAPTURE → DETECT → OCR → DECIDE → ACTUATE → LOG)
- **Retry logic** with enhanced preprocessing fallback on low-confidence OCR
- **SQLite database** for event logs, vehicle whitelist, and settings
- **Telegram Bot Integration** — real-time notifications with images, remote gate control, and whitelist management via chat
- **Web dashboard** (Flask) — dark-themed, responsive
- **Fail-safe** — barrier stays closed on crash; systemd auto-restart
- **Simulator mode** — runs on any machine (no Pi hardware required for development)

---

## Hardware Requirements

| Component | Model / Spec | Purpose |
|---|---|---|
| Raspberry Pi 3B+ | ARM Cortex-A53, 1 GB RAM | Main processor & orchestrator |
| Pi Camera V2 | Sony IMX219, 8 MP | Frame capture via CSI lane |
| HC-SR04 | 2 cm – 400 cm range | Vehicle proximity detection |
| SG90 / MG996R Servo | SG90 (180°) or MG996R (metal gear) | Physical barrier arm |
| Relay Module | 5V single-channel | External gate motor / solenoid control |
| 5V / 3A Power Supply | ≥ 3 A rating | Stable Pi power (avoid undervoltage) |
| 5V External Supply | Dedicated rail | Servo & relay power (share GND with Pi) |
| Voltage Divider | 1 kΩ + 2 kΩ resistors | Steps HC-SR04 ECHO 5V → 3.3V for GPIO |

---

## Hardware Deep Dive

This section explains **each hardware component** in full detail: its physical role in the gate system, how to wire it, and exactly how the Python driver implements the low-level control that feeds the rest of the software stack.

---

### 1. Raspberry Pi 3B+ — The Main Processor

#### Role
The Pi is the **central brain**. It runs the Python process that continuously polls the sensor, triggers the camera, runs the vision pipeline, makes the access decision, and actuates the barrier — all in a single coherent state machine (`src/state_machine.py`). It also hosts the Flask web dashboard and SQLite database.

Every peripheral connects to the Pi either through:
- **GPIO pins** (sensor, servo, relay) — controlled via `RPi.GPIO`
- **CSI camera port** — dedicated serial lane for the Pi Camera module
- **USB** — optional webcam fallback during development

#### Key specs relevant to this project
| Attribute | Value |
|---|---|
| GPIO voltage | **3.3 V** (⚠ never connect a 5 V signal directly) |
| Max GPIO sink/source current | 16 mA per pin, 50 mA total |
| PWM-capable pins | GPIO 12, 13, 18, 19 (hardware PWM) |
| CSI camera lane | 15-pin ribbon connector |

---

### 2. Pi Camera V2 — Image Capture

#### Role
Mounted above the gate, the Pi Camera V2 captures **still frames** when a vehicle is detected. Multiple frames are captured per trigger event; the sharpest one (highest Laplacian variance) is selected and forwarded to the vision pipeline.

#### Wiring
The Pi Camera connects to the **CSI (Camera Serial Interface) port** — the 15-pin ribbon connector between the USB ports and the HDMI port on the Pi 3B+.

```
Pi Camera V2  →  Pi 3B+ CSI port (15-pin ribbon)

Orientation:
  Blue side of ribbon faces the USB ports (away from HDMI)

Enable in OS:
  sudo raspi-config → Interface Options → Camera → Enable → Reboot
```

> ⚠ **Important:** The ribbon cable is fragile. Insert it fully (it clicks) and ensure the blue stripe faces the correct direction before closing the latch.

## Hardware Requirements (ESP32‑CAM Edition)

| Component | Model / Spec | Purpose |
|---|---|---|
| ESP32‑CAM (AI‑Thinker) | ESP32‑S2 MCU, OV2640 2 MP camera, 4 MiB PSRAM | Edge device handling camera, HC‑SR04, servo, relay; streams MJPEG over HTTP |
| HC‑SR04 Ultrasonic Sensor | 2 cm – 400 cm range | Vehicle proximity detection (queried via ESP32 HTTP endpoint) |
| SG90 / MG996R Servo | 0‑180° hobby servo | Physical barrier arm (controlled via ESP32 HTTP endpoint) |
| Relay Module | 5 V single‑channel | External gate‑motor or solenoid control (via ESP32 HTTP endpoint) |
| 5 V / 2 A Power Supply | ≥ 2 A rating | Powers ESP32‑CAM board and attached peripherals |
| Voltage Divider | 1 kΩ + 2 kΩ resistors | Steps HC‑SR04 ECHO 5 V → 3.3 V for ESP32 GPIO |

---

## Hardware Deep Dive (ESP32‑CAM Edition)

This section explains **each hardware component** in full detail: its physical role in the gate system, how to wire it to the ESP32‑CAM board, and exactly how the Arduino sketch implements the low‑level control that feeds the rest of the software stack.

---

### 1. ESP32‑CAM (AI‑Thinker) — The Edge Processor

#### Role
The ESP32‑CAM is the **edge brain**. It runs a lightweight Arduino sketch that:
- Captures MJPEG video frames from the built‑in OV2640 camera and serves them over HTTP (`/stream` and `/capture`).
- Reads the HC‑SR04 ultrasonic sensor and returns distance in centimetres (`/distance`).
- Drives a hobby servo (barrier arm) and a relay (gate motor) via HTTP POST (`/barrier/open`, `/barrier/close`).
- Exposes a `/status` endpoint with uptime, free heap, Wi‑Fi RSSI, and barrier state.

All heavy computation (OpenCV plate detection, Tesseract OCR, decision logic, database, web UI, Telegram) runs on a cloud/VPS render server that consumes the ESP32’s HTTP API.

#### Key specs relevant to this project
| Attribute | Value |
|---|---|
| MCU | Tensilica Xtensa 32‑bit, 240 MHz |
| Wi‑Fi | 802.11b/g/n, 2.4 GHz |
| Camera | OV2640, up to 2 MP (800×600 used) |
| GPIO voltage | **3.3 V** (⚠ never connect a 5 V signal directly) |
| PWM channels | 16 (hardware PWM via LEDC) |
| Flash | 4 MiB (SPI flash) |
| PSRAM | 4 MiB (optional, not used here) |

---

### 2. OV2640 Camera — Image Capture

#### Role
Mounted on the ESP32‑CAM board, the OV2640 captures **still JPEG frames**. The sketch streams a continuous MJPEG multipart response (`/stream`) and also provides a single‑shot JPEG endpoint (`/capture`).

#### Wiring & Configuration
The camera is soldered directly onto the ESP32‑CAM module – no external wiring required. In the sketch you can adjust:
- `CAMERA_FRAME_SIZE` – e.g. `FRAMESIZE_SVGA` (800×600) for a good trade‑off between detail and bandwidth.
- `CAMERA_JPEG_QUALITY` – lower values increase quality (0‑63 range).

#### Sketch Implementation (excerpt)
```cpp
camera_config_t config;
config.ledc_channel = LEDC_CHANNEL_0;
config.ledc_timer   = LEDC_TIMER_0;
config.pin_d0       = Y2_GPIO_NUM; // … pin mapping continues
config.pixel_format = PIXFORMAT_JPEG;
config.frame_size   = CAMERA_FRAME_SIZE;
config.jpeg_quality = CAMERA_JPEG_QUALITY;
config.fb_count     = 2;
esp_err_t camErr = esp_camera_init(&config);
```

The `/stream` handler builds a multipart response where each part contains a JPEG image. The Python `CameraService` reads this stream, extracts frames, and selects the sharpest one.

---

### 3. HC‑SR04 Ultrasonic Sensor — Vehicle Presence Detection

#### Role
Mounted at bumper height facing the approaching lane, the HC‑SR04 acts as a **proximity tripwire**. The ESP32‑CAM triggers the sensor and measures echo time to calculate distance. The result is exposed via a JSON endpoint (`/distance`).

#### Wiring (to ESP32‑CAM GPIOs)
```
HC‑SR04 Pin  →  ESP32‑CAM GPIO                Notes
───────────────────────────────────────────────────────────────
VCC          →  5 V (Vin)                     Power the sensor
GND          →  GND                           Common ground
TRIG         →  GPIO 12 (Pin 12)               3.3 V output is enough to trigger
ECHO         →  GPIO 13 (Pin 13)               ⚠ MUST use voltage divider!
```

**Voltage divider for ECHO pin (required):**
```
ECHO (5 V) ──┬── 1 kΩ ──── GPIO 13 (3.3 V safe)
              └── 2 kΩ ──── GND
```
The divider yields ≈ 3.33 V, safe for the ESP32’s 3.3 V GPIO.

#### Sketch Implementation (excerpt)
```cpp
float readDistanceCm() {
    // 10 µs trigger pulse
    digitalWrite(PIN_HCSR04_TRIG, LOW);
    delayMicroseconds(2);
    digitalWrite(PIN_HCSR04_TRIG, HIGH);
    delayMicroseconds(10);
    digitalWrite(PIN_HCSR04_TRIG, LOW);

    // Wait for echo HIGH with timeout
    unsigned long timeoutStart = micros();
    while (digitalRead(PIN_HCSR04_ECHO) == LOW) {
        if (micros() - timeoutStart > DISTANCE_TIMEOUT_US) return MAX_DISTANCE_CM;
    }
    unsigned long pulseStart = micros();
    while (digitalRead(PIN_HCSR04_ECHO) == HIGH) {
        if (micros() - pulseStart > DISTANCE_TIMEOUT_US) return MAX_DISTANCE_CM;
    }
    unsigned long pulseDuration = micros() - pulseStart;
    float distance = pulseDuration * 0.0343f / 2.0f; // cm
    return min(distance, MAX_DISTANCE_CM);
}
```
The `/distance` handler simply calls `readDistanceCm()` and returns `{"distance_cm": <value>}`.

---

### 4. Servo + Relay — Physical Barrier Actuation

#### Role
The servo swings the barrier arm; the relay drives an external gate‑motor or solenoid for heavier barriers. Both are controlled via HTTP POST requests to the ESP32‑CAM.

#### Wiring (to ESP32‑CAM GPIOs)
```
Component      →  ESP32‑CAM GPIO   Notes
───────────────────────────────────────────────────────
Servo signal    →  GPIO 14 (Pin 14)   PWM 50 Hz via LEDC channel 0
Relay control  →  GPIO 15 (Pin 15)   HIGH = ON, LOW = OFF
GND (all)      →  GND (Pin 1)        Common ground for ESP32, servo & relay power
5 V rail       →  External 5 V supply (share GND) – powers servo & relay
```
> ⚠ **Never** power the servo or relay directly from the ESP32’s 3.3 V rail. Use a dedicated 5 V supply and connect grounds.

#### Sketch Implementation (excerpt)
```cpp
void setServoAngle(int angle) {
    int pulseUs = map(constrain(angle,0,180), 0,180, 500,2400);
    uint32_t duty = (uint32_t)pulseUs * 65536 / 20000; // LEDC 16‑bit duty
    ledcWrite(0, duty);
    delay(400);
    ledcWrite(0, 0); // stop jitter
}

void setRelay(bool on) {
    digitalWrite(PIN_RELAY, on ? HIGH : LOW);
}

// POST /barrier/open → open angle + relay ON
// POST /barrier/close → closed angle + relay OFF
```

The Python `ActuatorController` simply POSTs to these endpoints; the sketch moves the servo and toggles the relay accordingly.

---

## Migration to ESP32‑CAM (New Architecture)

**TL;DR** – The Raspberry Pi is completely removed. All hardware I/O (camera, HC‑SR04, servo, relay) is now handled by an ESP32‑CAM board that streams MJPEG over HTTP to a cloud/VPS render server. The Python stack (vision, decision, DB, web UI, Telegram) runs **exclusively** on the server.

### Why ESP32‑CAM?
- **Built‑in Wi‑Fi** – no extra networking hardware.
- **Integrated OV2640 camera** – MJPEG streaming support.
- **Sufficient GPIO** for HC‑SR04, a hobby‑servo and a relay.
- **Low power & cost** – ~US$10 board vs. a full Pi.
- **Simplifies deployment** – only a single edge device at the gate; all heavy compute stays in the cloud.

### New System Diagram
```
[ ESP32‑CAM ]  <--Wi‑Fi-->  [ Cloud/VPS Render Server ]
   |  (MJPEG stream, /distance, /barrier/*)   |
   |                                          |
   |  Python services (camera, sensor, actuator)  |
   |  ──> OpenCV, Tesseract, SQLite, Flask, Telegram
   └───────────────────────────────────────────────
```

### What changed in the codebase?
| Component | Old (Pi) | New (ESP32‑CAM) |
|---|---|---|
| Camera | `picamera2` → local CSI | MJPEG HTTP stream (`src/camera.py` rewritten) |
| Sensor | Direct GPIO trigger/echo | HTTP GET `/distance` (`src/sensor.py` rewritten) |
| Actuator | PWM + GPIO relay | HTTP POST `/barrier/open` & `/barrier/close` (`src/actuator.py` rewritten) |
| Config | `config.yaml` Pi‑specific fields | New `esp32:` section with URLs & timeouts |
| Requirements | No `requests` | Added `requests` |
| Firmware | None | New Arduino sketch `esp32cam/esp32cam.ino` |


## Getting Started (ESP32‑CAM Edition)

1. **Flash the ESP32‑CAM firmware** – see `esp32cam/esp32cam.ino`.
2. **Configure Wi‑Fi** in the sketch (SSID / password).
3. **Update `config.yaml`** – set `esp32.base_url` to the ESP32’s IP (or mDNS name) and adjust paths if you changed them.
4. **Deploy the Python stack** on a cloud/VPS (Ubuntu 22.04+ recommended). Install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
5. **Run the service**:
   ```bash
   python -m src.main
   ```
   The system will now pull frames from the ESP32, run the vision pipeline, and control the barrier via HTTP.

---

## Development & Testing (No ESP32 hardware?)

- **Simulator mode** – All hardware classes (`CameraService`, `UltrasonicSensor`, `ActuatorController`) fall back to a *simulated* implementation when the ESP32 endpoint is unreachable. You can manually set distances or barrier state via the class methods (`set_simulator_distance`, etc.) for unit‑tests.
- **Unit tests** – The `tests/` directory already contains mocks for the hardware services; they continue to work unchanged.

---

## License

MIT – see `LICENSE` file.

│   │                   [via 1kΩ/2kΩ voltage divider — 5V to 3.3V]
│   ├── GPIO 18  OUT  → Servo Signal     (50 Hz PWM, duty cycle = angle)
│   └── GPIO 25  OUT  → Relay IN        (HIGH/LOW to open/close gate)
│
└── CSI Port (Camera Serial Interface 2 — ribbon cable)
    └── Pi Camera V2  →  Dedicated MIPI CSI-2 lane  →  Pi's VideoCore GPU
                         (raw Bayer data → ISP → RGB/YUV → picamera2 array)
```

### Decision Flow (End-to-End)

```
Vehicle enters lane
        ↓
[HC-SR04] Distance < threshold for N readings?
        ↓ YES
[Camera] Capture best_frame (sharpest of capture_count frames)
        ↓
[PlateDetector] Find 4-vertex contour with plate aspect ratio
                → perspective-correct crop
        ↓
[OcrEngine] Run Tesseract on crop → raw text + confidence
            If confidence < threshold → retry with enhanced preprocessing
            If still failing → retry full capture (up to max_retries)
        ↓
[DecisionEngine] Is plate in whitelist DB?
                 Is confidence sufficient?
                 ↓ ALLOW              ↓ DENY / UNKNOWN
[ActuatorController]   Servo → 90°    Servo stays at 0°
                       Relay → ON     Relay stays OFF
                       Auto-close timer armed
        ↓
[Database] Log event: plate, decision, confidence, image path, timestamp
        ↓
Return to IDLE
```

> ⚠ **Important:** Use a voltage divider (two resistors) on the HC-SR04 ECHO pin to step 5V down to 3.3V. This protects the Pi's GPIO.

---

---

## Telegram Bot Integration

The ANPR system includes a built-in Telegram bot that provides real-time notifications and allows you to control the gate remotely.

### 1. Create a Telegram Bot
1.  Open Telegram and search for **@BotFather**.
2.  Send the command `/newbot`.
3.  Follow the instructions to name your bot and give it a username.
4.  BotFather will give you an **API Token**. Copy this; you'll need it for your configuration.
5.  Search for your bot by its username and click **Start**.

### 2. Get Your Chat ID
The system only accepts commands from authorized Chat IDs.
1.  Start a conversation with your bot.
2.  Visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in your browser.
3.  Look for the `"chat":{"id":123456789...}` section in the JSON response. That number is your Chat ID.

### 3. Configuration
Edit your `config.yaml` to enable the bot:

```yaml
telegram:
  enabled: true
  bot_token: "PASTE_YOUR_TOKEN_HERE"
  allowed_chat_ids: [123456789]  # Add your Chat ID here
  notify_on_allow: true
  notify_on_deny: true
  notify_on_unknown: true
  send_image: true
```

### 4. Bot Commands
| Command | Description |
|---|---|
| `/start` | Show help and command list |
| `/status` | Current system state, barrier status, and uptime |
| `/last_event` | Details of the most recent plate detection |
| `/snapshot` | Capture a live frame from the camera and send it |
| `/open_gate` | Manually raise the barrier |
| `/close_gate` | Manually lower the barrier |
| `/add_plate <ABC123>` | Add a plate to the whitelist |
| `/remove_plate <ABC123>` | Remove a plate from the whitelist |
| `/list_plates` | List all whitelisted vehicles |

---

## Installation

### 1. Prerequisites (Raspberry Pi)

```bash
sudo apt update && sudo apt install -y \
    python3-pip python3-venv \
    tesseract-ocr \
    libopencv-dev

# Enable camera
sudo raspi-config  # → Interface Options → Camera → Enable
```

### 2. Clone & Install

```bash
cd ~
git clone <your-repo-url> ANPR_System
cd ANPR_System

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Pi-specific packages (only on Raspberry Pi)
pip install RPi.GPIO picamera2 gpiozero
```

### 3. Configure

Edit `config.yaml` to set your GPIO pins, thresholds, and preferences.

---

## Usage

### Run the ANPR system

```bash
cd ~/ANPR_System
source venv/bin/activate
python -m src.main
```

### Run the web dashboard

```bash
python -m web.app
# Open http://<pi-ip>:5000 in your browser
```

### Run tests

```bash
python -m pytest tests/ -v
```

---

## Systemd Services (Auto-start on Boot)

```bash
# Copy service files
sudo cp systemd/anpr_core.service /etc/systemd/system/
sudo cp systemd/anpr_web.service /etc/systemd/system/

# Edit paths in the service files if needed
sudo nano /etc/systemd/system/anpr_core.service

# Enable & start
sudo systemctl daemon-reload
sudo systemctl enable anpr_core anpr_web
sudo systemctl start anpr_core anpr_web

# Check status
sudo systemctl status anpr_core
```

---

## Project Structure

```
ANPR_System/
├── config.yaml              # Runtime configuration
├── requirements.txt         # Python dependencies
├── systemd/                 # systemd service files
│   ├── anpr_core.service
│   └── anpr_web.service
├── data/                    # Created at runtime
│   ├── db/anpr.db
│   └── events/YYYY-MM-DD/
├── src/
│   ├── config.py            # YAML config loader
│   ├── database.py          # SQLite schema + CRUD
│   ├── sensor.py            # HC-SR04 driver
│   ├── actuator.py          # Servo + Relay control
│   ├── camera.py            # Pi Camera capture
│   ├── preprocessing.py     # Image preprocessing
│   ├── plate_detector.py    # OpenCV plate detection
│   ├── ocr_engine.py        # Tesseract OCR
│   ├── decision_engine.py   # Allow/Deny logic
│   ├── state_machine.py     # Workflow orchestrator
│   └── main.py              # Entry point
├── web/
│   ├── app.py               # Flask dashboard
│   ├── templates/           # HTML pages
│   └── static/style.css     # Dark theme CSS
└── tests/                   # pytest suite
```

---

## Configuration Reference

All settings live in `config.yaml`:

| Section | Key | Default | Description |
|---|---|---|---|
| `sensor` | `distance_threshold_cm` | 50 | Trigger capture below this distance |
| `sensor` | `confirmation_readings` | 3 | Consecutive readings to confirm |
| `detection` | `min_detection_confidence` | 0.5 | Minimum plate detection score |
| `detection` | `min_ocr_confidence` | 60 | Minimum OCR confidence (0–100) |
| `detection` | `max_retries` | 3 | Retry capture on failure |
| `actuator` | `open_duration_sec` | 10 | Auto-close barrier after N seconds |
| `actuator` | `servo_open_angle` | 90 | Barrier open position |
| `ocr` | `engine` | tesseract | OCR engine (tesseract) |

---

## Tech Stack

- **Python 3** — core language
- **OpenCV** — image processing + plate detection
- **pytesseract** — OCR engine
- **Flask** — web dashboard
- **SQLite** — local database
- **RPi.GPIO** — hardware control (Pi only)

---

## License

MIT
 # APNR_System_ESP32CAM
