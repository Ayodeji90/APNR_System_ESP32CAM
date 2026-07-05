"""
ANPR System — Camera Capture Service (ESP32-CAM Edition)

Reads frames from an ESP32-CAM MJPEG stream over HTTP.
Selects the sharpest frame using Laplacian variance.
"""

import time
import logging
import threading
from typing import Optional, List

import cv2
import numpy as np
import requests

from src.config import AppConfig

logger = logging.getLogger(__name__)


class CameraService:
    """Captures frames from an ESP32-CAM MJPEG stream over HTTP."""

    def __init__(self, cfg: AppConfig):
        self.width = cfg.camera.resolution_width
        self.height = cfg.camera.resolution_height
        self.capture_count = cfg.camera.capture_count
        self.warmup = cfg.camera.warmup_seconds

        self._stream_url = cfg.esp32.stream_url
        self._capture_url = cfg.esp32.capture_url
        self._stream_timeout = cfg.esp32.stream_timeout_sec
        self._request_timeout = cfg.esp32.request_timeout_sec
        self._auth_headers = cfg.esp32.auth_headers

        self._stream_response: Optional[requests.Response] = None
        self._stream_lock = threading.Lock()
        self._bytes_buffer = b""
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_ready = threading.Event()

        # Open persistent MJPEG stream connection
        self._connect_stream()

    # ── Stream connection ───────────────────────────────────
    def _connect_stream(self) -> None:
        """Open a persistent HTTP connection to the ESP32 MJPEG stream."""
        if not self._stream_url:
            logger.error("ESP32 stream URL not configured — camera will return blank frames.")
            return

        try:
            self._stream_response = requests.get(
                self._stream_url,
                stream=True,
                timeout=(self._request_timeout, self._stream_timeout),
                headers=self._auth_headers,
            )
            self._stream_response.raise_for_status()
            logger.info("Connected to ESP32 MJPEG stream: %s", self._stream_url)

            # Start background reader thread
            self._reader_thread = threading.Thread(
                target=self._stream_reader, daemon=True
            )
            self._reader_thread.start()
        except requests.RequestException as e:
            logger.error("Failed to connect to ESP32 stream at %s: %s", self._stream_url, e)
            self._stream_response = None

    def _stream_reader(self) -> None:
        """Background thread: continuously reads MJPEG frames from the stream."""
        if self._stream_response is None:
            return

        boundary = None
        try:
            for chunk in self._stream_response.iter_content(chunk_size=4096):
                if chunk:
                    self._bytes_buffer += chunk

                    # Detect MJPEG boundary from Content-Type header
                    if boundary is None:
                        ct = self._stream_response.headers.get("Content-Type", "")
                        if "boundary=" in ct:
                            boundary = ct.split("boundary=")[-1].strip().encode()
                            logger.debug("MJPEG boundary: %s", boundary)
                        else:
                            # Try to infer from first chunk
                            a = self._bytes_buffer.find(b"\r\n\r\n")
                            if a != -1:
                                boundary = self._bytes_buffer[:a].split(b"\r\n")[0]
                                logger.debug("Inferred boundary: %s", boundary)

                    if boundary:
                        self._extract_frames(boundary)
        except requests.RequestException as e:
            logger.warning("MJPEG stream read error: %s", e)
        except Exception as e:
            logger.warning("MJPEG reader exception: %s", e)
        finally:
            logger.info("MJPEG stream reader stopped.")

    def _extract_frames(self, boundary: bytes) -> None:
        """Extract complete JPEG frames from the byte buffer using MJPEG boundary."""
        while True:
            start = self._bytes_buffer.find(boundary)
            if start == -1:
                break

            end = self._bytes_buffer.find(boundary, start + len(boundary))
            if end == -1:
                break  # incomplete frame, wait for more data

            part = self._bytes_buffer[start:end]

            # Find JPEG data (after the double CRLF that ends headers)
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                # This boundary segment has no body — skip it
                self._bytes_buffer = self._bytes_buffer[end:]
                continue

            jpeg_data = part[header_end + 4:]

            if len(jpeg_data) > 100:  # minimum plausible JPEG size
                try:
                    frame = cv2.imdecode(
                        np.frombuffer(jpeg_data, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                    if frame is not None:
                        with self._stream_lock:
                            self._latest_frame = frame
                        self._frame_ready.set()
                except Exception as e:
                    logger.debug("Frame decode error: %s", e)

            # Remove processed segment from buffer
            self._bytes_buffer = self._bytes_buffer[end:]

    # ── Capture a single frame ──────────────────────────────
    def capture_frame(self) -> np.ndarray:
        """Return a single BGR frame from the MJPEG stream."""
        # Try to get the latest frame from the stream
        self._frame_ready.wait(timeout=2.0)
        self._frame_ready.clear()

        with self._stream_lock:
            if self._latest_frame is not None:
                return self._latest_frame.copy()

        # Fallback: try single-capture endpoint
        if self._capture_url:
            try:
                resp = requests.get(
                    self._capture_url,
                    timeout=self._request_timeout,
                    headers=self._auth_headers,
                )
                resp.raise_for_status()
                frame = cv2.imdecode(
                    np.frombuffer(resp.content, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if frame is not None:
                    return frame
            except requests.RequestException as e:
                logger.warning("Single capture fallback failed: %s", e)

        # Last resort — blank frame
        logger.warning("Returning blank frame (no stream data)")
        return np.zeros((self.height, self.width, 3), dtype=np.uint8)

    # ── Sharpness metric ────────────────────────────────────
    @staticmethod
    def _laplacian_variance(frame: np.ndarray) -> float:
        """Higher value = sharper image."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.Laplacian(gray, cv2.CV_64F).var()

    # ── Capture best frame ──────────────────────────────────
    def capture_best_frame(self) -> np.ndarray:
        """
        Capture *capture_count* frames and return the sharpest one
        (highest Laplacian variance).
        """
        best_frame: Optional[np.ndarray] = None
        best_score: float = -1.0

        for i in range(self.capture_count):
            frame = self.capture_frame()
            score = self._laplacian_variance(frame)
            logger.debug("Frame %d sharpness: %.1f", i, score)
            if score > best_score:
                best_score = score
                best_frame = frame
            time.sleep(0.1)  # small delay between captures

        logger.info("Best frame sharpness: %.1f", best_score)
        return best_frame if best_frame is not None else self.capture_frame()

    # ── Cleanup ─────────────────────────────────────────────
    def cleanup(self) -> None:
        if self._stream_response:
            self._stream_response.close()
            logger.info("MJPEG stream connection closed.")
