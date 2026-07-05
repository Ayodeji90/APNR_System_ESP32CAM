"""
ANPR System — Configuration Loader

Loads config.yaml and exposes an AppConfig dataclass with sensible defaults.
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Optional

import yaml


# ── Defaults ────────────────────────────────────────────────
_DEFAULTS = {
    "esp32": {
        "base_url": "",
        "api_key": "",
        "stream_path": "/stream",
        "capture_path": "/capture",
        "sensor_path": "/distance",
        "barrier_open_path": "/barrier/open",
        "barrier_close_path": "/barrier/close",
        "status_path": "/status",
        "stream_timeout_sec": 10,
        "request_timeout_sec": 5,
    },
    "camera": {
        "resolution_width": 640,
        "resolution_height": 480,
        "capture_count": 5,
        "warmup_seconds": 0,
    },
    "sensor": {
        "distance_threshold_cm": 50,
        "confirmation_readings": 3,
        "reading_interval_sec": 0.1,
    },
    "actuator": {
        "servo_open_angle": 90,
        "servo_closed_angle": 0,
        "open_duration_sec": 10,
        "use_servo": True,
        "use_relay": False,
    },
    "detection": {
        "min_detection_confidence": 0.5,
        "min_ocr_confidence": 60,
        "max_retries": 3,
        "preprocessing_width": 800,
        "plate_aspect_min": 2.0,
        "plate_aspect_max": 6.0,
        "min_plate_area": 1000,
    },
    "ocr": {
        "engine": "tesseract",
        "tesseract_psm": 7,
        "char_whitelist": "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    },
    "paths": {
        "database": "data/db/anpr.db",
        "events_dir": "data/events",
        "event_retention_days": 30,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 5000,
        "debug": False,
        "secret_key": "",
        "dashboard_username": "",
        "dashboard_password": "",
    },
    "logging": {
        "level": "INFO",
        "file": "data/anpr.log",
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "allowed_chat_ids": [],
        "notify_on_allow": True,
        "notify_on_deny": True,
        "notify_on_unknown": True,
        "send_image": True,
    },
}


# ── Nested Config Dataclasses ───────────────────────────────
@dataclass
class Esp32Config:
    """ESP32-CAM edge device connection settings."""
    base_url: str = ""
    api_key: str = ""
    stream_path: str = "/stream"
    capture_path: str = "/capture"
    sensor_path: str = "/distance"
    barrier_open_path: str = "/barrier/open"
    barrier_close_path: str = "/barrier/close"
    status_path: str = "/status"
    stream_timeout_sec: int = 10
    request_timeout_sec: int = 5

    @property
    def auth_headers(self) -> dict:
        """HTTP headers for authenticating against the ESP32 (empty if no key set)."""
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    @property
    def stream_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.stream_path}"

    @property
    def capture_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.capture_path}"

    @property
    def sensor_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.sensor_path}"

    @property
    def barrier_open_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.barrier_open_path}"

    @property
    def barrier_close_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.barrier_close_path}"

    @property
    def status_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.status_path}"


@dataclass
class CameraConfig:
    """Camera capture settings (frames pulled from ESP32 MJPEG stream)."""
    resolution_width: int = 640
    resolution_height: int = 480
    capture_count: int = 5
    warmup_seconds: int = 0  # Not needed for MJPEG stream


@dataclass
class SensorConfig:
    """Ultrasonic sensor settings (readings fetched from ESP32 over HTTP)."""
    distance_threshold_cm: int = 50
    confirmation_readings: int = 3
    reading_interval_sec: float = 0.1


@dataclass
class ActuatorConfig:
    """Barrier actuator settings (commands sent to ESP32 over HTTP)."""
    servo_open_angle: int = 90
    servo_closed_angle: int = 0
    open_duration_sec: int = 10
    use_servo: bool = True
    use_relay: bool = False


@dataclass
class DetectionConfig:
    min_detection_confidence: float = 0.5
    min_ocr_confidence: float = 60
    max_retries: int = 3
    preprocessing_width: int = 800
    plate_aspect_min: float = 2.0
    plate_aspect_max: float = 6.0
    min_plate_area: int = 1000


@dataclass
class OcrConfig:
    engine: str = "tesseract"
    tesseract_psm: int = 7
    char_whitelist: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


@dataclass
class PathsConfig:
    database: str = "data/db/anpr.db"
    events_dir: str = "data/events"
    event_retention_days: int = 30


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False
    secret_key: str = ""
    dashboard_username: str = ""
    dashboard_password: str = ""


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "data/anpr.log"


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    allowed_chat_ids: list = field(default_factory=list)
    notify_on_allow: bool = True
    notify_on_deny: bool = True
    notify_on_unknown: bool = True
    send_image: bool = True


@dataclass
class AppConfig:
    """Top-level application configuration."""
    esp32: Esp32Config = field(default_factory=Esp32Config)
    camera: CameraConfig = field(default_factory=CameraConfig)
    sensor: SensorConfig = field(default_factory=SensorConfig)
    actuator: ActuatorConfig = field(default_factory=ActuatorConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    # Absolute base directory (set at load time)
    base_dir: str = ""


# ── Deep merge helper ──────────────────────────────────────
def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ── Loader ──────────────────────────────────────────────────
def load_config(config_path: Optional[str] = None) -> AppConfig:
    """
    Load configuration from a YAML file, merge with defaults,
    and return an AppConfig instance.

    If *config_path* is None, looks for ``config.yaml`` next to the
    project root (parent of ``src/``).
    """
    if config_path is None:
        # Default: project root / config.yaml
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base_dir, "config.yaml")
    else:
        base_dir = os.path.dirname(os.path.abspath(config_path))

    raw: dict = {}
    if os.path.isfile(config_path):
        with open(config_path, "r") as fh:
            raw = yaml.safe_load(fh) or {}
    else:
        logging.warning("Config file not found at %s — using defaults.", config_path)

    merged = _deep_merge(_DEFAULTS, raw)

    cfg = AppConfig(
        esp32=Esp32Config(**merged["esp32"]),
        camera=CameraConfig(**merged["camera"]),
        sensor=SensorConfig(**merged["sensor"]),
        actuator=ActuatorConfig(**merged["actuator"]),
        detection=DetectionConfig(**merged["detection"]),
        ocr=OcrConfig(**merged["ocr"]),
        paths=PathsConfig(**merged["paths"]),
        web=WebConfig(**merged["web"]),
        logging=LoggingConfig(**merged["logging"]),
        telegram=TelegramConfig(**merged["telegram"]),
        base_dir=base_dir,
    )

    return cfg


# ── Convenience: resolve paths relative to base_dir ────────
def resolve_path(cfg: AppConfig, relative_path: str) -> str:
    """Return an absolute path by joining *relative_path* with cfg.base_dir."""
    if os.path.isabs(relative_path):
        return relative_path
    return os.path.join(cfg.base_dir, relative_path)


# ── Setup logging based on config ──────────────────────────
def setup_logging(cfg: AppConfig) -> None:
    """Configure the root logger from AppConfig."""
    log_path = resolve_path(cfg, cfg.logging.file)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    numeric_level = getattr(logging, cfg.logging.level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(),
        ],
    )
