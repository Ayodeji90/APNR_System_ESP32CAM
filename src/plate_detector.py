"""
ANPR System — Plate Detection (OpenCV)

Detects license plate regions in a frame and returns the cropped,
deskewed plate image plus a confidence score.

Two detection strategies are tried, best result wins:
  1. Bright-region segmentation (primary) — license plates are bright,
     high-contrast rectangles; robust for dark/low-light frames where the
     plate is the dominant lit object.
  2. Edge + 4-vertex contour (fallback) — classic approach, works when the
     plate has clean straight edges against a busy background.
"""

import logging
from typing import Optional, Tuple

import cv2
import numpy as np

from src.config import AppConfig
from src.preprocessing import ImagePreprocessor

logger = logging.getLogger(__name__)


class PlateDetector:
    """Detects license plate regions using OpenCV."""

    def __init__(self, cfg: AppConfig):
        self.preprocessor = ImagePreprocessor(cfg)
        self.aspect_min = cfg.detection.plate_aspect_min
        self.aspect_max = cfg.detection.plate_aspect_max
        self.min_area = cfg.detection.min_plate_area
        self.target_width = cfg.detection.preprocessing_width

    def detect(
        self, frame: np.ndarray
    ) -> Tuple[Optional[np.ndarray], float]:
        """
        Detect a license plate in the given frame.

        Returns:
            (plate_crop, confidence)
            - plate_crop: cropped & perspective-corrected plate image, or None
            - confidence: 0.0–1.0
        """
        resized = self.preprocessor.resize(frame, self.target_width)
        frame_area = resized.shape[0] * resized.shape[1]

        # Strategy 1: bright-region segmentation (primary)
        crop_b, conf_b = self._detect_bright(resized, frame_area)
        # Strategy 2: edge + 4-vertex contour (fallback)
        crop_e, conf_e = self._detect_edges(resized, frame_area)

        if conf_b >= conf_e and crop_b is not None:
            best_plate, best_confidence = crop_b, conf_b
        elif crop_e is not None:
            best_plate, best_confidence = crop_e, conf_e
        else:
            best_plate, best_confidence = crop_b, conf_b

        if best_plate is not None:
            logger.info("Plate detected — confidence=%.2f", best_confidence)
        else:
            logger.info("No plate detected in frame.")

        return best_plate, best_confidence

    # ── Strategy 1: bright-region segmentation ──────────────
    def _detect_bright(
        self, resized: np.ndarray, frame_area: int
    ) -> Tuple[Optional[np.ndarray], float]:
        """Find the plate as the dominant bright rectangular region."""
        gray = self.preprocessor.clahe(resized)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # Fill gaps so the plate becomes one solid blob
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (17, 17))
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(
            th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:10]

        best_crop: Optional[np.ndarray] = None
        best_conf = 0.0
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < self.min_area:
                continue

            # minAreaRect handles rotation/skew directly
            rect = cv2.minAreaRect(contour)
            (w, h) = rect[1]
            if min(w, h) == 0:
                continue
            aspect = max(w, h) / min(w, h)
            if not (self.aspect_min <= aspect <= self.aspect_max):
                continue

            # Pad the rect ~14% so edge characters aren't clipped
            # (the first/last plate character often sits right at the border).
            (cx, cy), (rw, rh), ang = rect
            padded = ((cx, cy), (rw * 1.14, rh * 1.14), ang)
            box = cv2.boxPoints(padded)
            crop = self._four_point_transform(resized, box)
            if crop is None:
                continue

            # Confidence from how much of the frame the plate fills.
            # A real plate fills a large fraction; junk contours don't.
            fill = area / frame_area
            conf = float(np.clip(0.3 + fill * 1.5, 0.0, 1.0))
            if conf > best_conf:
                best_conf = conf
                best_crop = crop
                logger.debug(
                    "Bright candidate: area=%d aspect=%.2f fill=%.2f conf=%.2f",
                    area, aspect, fill, conf,
                )

        return best_crop, best_conf

    # ── Strategy 2: edge + 4-vertex contour ─────────────────
    def _detect_edges(
        self, resized: np.ndarray, frame_area: int
    ) -> Tuple[Optional[np.ndarray], float]:
        """Classic edge-based detection: 4-vertex rectangular contour."""
        gray = self.preprocessor.clahe(resized)
        gray = self.preprocessor.bilateral_filter(gray)
        edges = self.preprocessor.canny_edges(gray)

        contours, _ = cv2.findContours(
            edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
        )
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:30]

        best_crop: Optional[np.ndarray] = None
        best_conf = 0.0
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < self.min_area:
                continue

            peri = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
            if len(approx) != 4:
                continue

            x, y, w, h = cv2.boundingRect(approx)
            if h == 0:
                continue
            aspect = w / h
            if not (self.aspect_min <= aspect <= self.aspect_max):
                continue

            crop = self._four_point_transform(resized, approx)
            if crop is None:
                continue

            fill = area / frame_area
            conf = float(np.clip(0.3 + fill * 1.5, 0.0, 1.0))
            if conf > best_conf:
                best_conf = conf
                best_crop = crop

        return best_crop, best_conf

    # ── Perspective transform ───────────────────────────────
    @staticmethod
    def _four_point_transform(
        image: np.ndarray, pts: np.ndarray
    ) -> Optional[np.ndarray]:
        """
        Apply a perspective transform to extract a rectangular region
        defined by 4 points (any order).
        """
        try:
            pts = pts.reshape(4, 2).astype(np.float32)

            # Order points: top-left, top-right, bottom-right, bottom-left
            rect = np.zeros((4, 2), dtype=np.float32)
            s = pts.sum(axis=1)
            rect[0] = pts[np.argmin(s)]
            rect[2] = pts[np.argmax(s)]
            diff = np.diff(pts, axis=1)
            rect[1] = pts[np.argmin(diff)]
            rect[3] = pts[np.argmax(diff)]

            width_a = np.linalg.norm(rect[2] - rect[3])
            width_b = np.linalg.norm(rect[1] - rect[0])
            max_width = max(int(width_a), int(width_b))

            height_a = np.linalg.norm(rect[1] - rect[2])
            height_b = np.linalg.norm(rect[0] - rect[3])
            max_height = max(int(height_a), int(height_b))

            if max_width < 10 or max_height < 10:
                return None

            dst = np.array(
                [[0, 0], [max_width - 1, 0],
                 [max_width - 1, max_height - 1], [0, max_height - 1]],
                dtype=np.float32,
            )

            M = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(image, M, (max_width, max_height))
            return warped
        except Exception as e:
            logger.debug("Perspective transform failed: %s", e)
            return None
