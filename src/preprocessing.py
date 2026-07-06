"""
ANPR System — Image Preprocessing Pipeline

Provides a chain of OpenCV operations to enhance plate images
before OCR. Designed to be lightweight for Raspberry Pi 3B+.
"""

import logging
from typing import Optional

import cv2
import numpy as np

from src.config import AppConfig

logger = logging.getLogger(__name__)


class ImagePreprocessor:
    """Image preprocessing pipeline for plate recognition."""

    def __init__(self, cfg: AppConfig):
        self.target_width = cfg.detection.preprocessing_width

    # ── Individual steps ────────────────────────────────────
    @staticmethod
    def resize(image: np.ndarray, width: int) -> np.ndarray:
        """Resize image to fixed width, preserving aspect ratio."""
        h, w = image.shape[:2]
        if w == 0:
            return image
        ratio = width / w
        new_dim = (width, int(h * ratio))
        return cv2.resize(image, new_dim, interpolation=cv2.INTER_AREA)

    @staticmethod
    def to_grayscale(image: np.ndarray) -> np.ndarray:
        """Convert BGR to grayscale (no-op if already gray)."""
        if len(image.shape) == 2:
            return image
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    @staticmethod
    def bilateral_filter(image: np.ndarray) -> np.ndarray:
        """Apply bilateral filter — edge-preserving smoothing."""
        return cv2.bilateralFilter(image, d=11, sigmaColor=17, sigmaSpace=17)

    @staticmethod
    def gaussian_blur(image: np.ndarray, ksize: int = 5) -> np.ndarray:
        """Apply Gaussian blur."""
        return cv2.GaussianBlur(image, (ksize, ksize), 0)

    @staticmethod
    def adaptive_threshold(image: np.ndarray) -> np.ndarray:
        """Apply adaptive Gaussian thresholding."""
        return cv2.adaptiveThreshold(
            image, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=11,
            C=2,
        )

    @staticmethod
    def otsu_threshold(image: np.ndarray) -> np.ndarray:
        """Apply Otsu's binarization."""
        _, binary = cv2.threshold(
            image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        return binary

    @staticmethod
    def morphology_open(image: np.ndarray, ksize: int = 3) -> np.ndarray:
        """Morphological opening — remove small noise."""
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (ksize, ksize)
        )
        return cv2.morphologyEx(image, cv2.MORPH_OPEN, kernel)

    @staticmethod
    def morphology_close(image: np.ndarray, ksize: int = 3) -> np.ndarray:
        """Morphological closing — fill small holes."""
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (ksize, ksize)
        )
        return cv2.morphologyEx(image, cv2.MORPH_CLOSE, kernel)

    @staticmethod
    def sharpen(image: np.ndarray) -> np.ndarray:
        """Apply unsharp mask sharpening."""
        blurred = cv2.GaussianBlur(image, (0, 0), 3)
        return cv2.addWeighted(image, 1.5, blurred, -0.5, 0)

    @staticmethod
    def invert(image: np.ndarray) -> np.ndarray:
        """Invert pixel values."""
        return cv2.bitwise_not(image)

    @staticmethod
    def canny_edges(image: np.ndarray, low: int = 30, high: int = 200) -> np.ndarray:
        """Canny edge detection."""
        return cv2.Canny(image, low, high)

    @staticmethod
    def clahe(image: np.ndarray, clip: float = 3.0, grid: int = 8) -> np.ndarray:
        """Contrast Limited Adaptive Histogram Equalisation.

        Lifts detail out of dark/unevenly-lit frames — essential for the
        low-light plate captures this system sees.
        """
        gray = image if len(image.shape) == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid))
        return clahe.apply(gray)

    @staticmethod
    def upscale_min_width(image: np.ndarray, min_width: int = 400) -> np.ndarray:
        """Upscale a small crop so characters are big enough for Tesseract.

        Tesseract wants roughly 30px+ character height; tiny plate crops
        OCR far better after cubic upscaling.
        """
        h, w = image.shape[:2]
        if w == 0 or w >= min_width:
            return image
        scale = min_width / w
        return cv2.resize(image, (min_width, int(h * scale)), interpolation=cv2.INTER_CUBIC)

    # ── Full pipeline ───────────────────────────────────────
    def preprocess_for_detection(self, image: np.ndarray) -> np.ndarray:
        """
        Standard preprocessing pipeline for plate *detection*
        (finding the plate region in the full frame).

        Returns a grayscale + edge image.
        """
        img = self.resize(image, self.target_width)
        img = self.to_grayscale(img)
        img = self.bilateral_filter(img)
        edges = self.canny_edges(img)
        return edges

    def preprocess_for_ocr(self, plate_crop: np.ndarray) -> np.ndarray:
        """
        Standard preprocessing pipeline for *OCR*
        (reading text from an already-cropped plate image).

        Returns a clean binary image ready for Tesseract.
        """
        img = self.to_grayscale(plate_crop)
        img = self.bilateral_filter(img)
        img = self.adaptive_threshold(img)
        img = self.morphology_close(img, ksize=2)
        return img

    def preprocess_for_ocr_enhanced(self, plate_crop: np.ndarray) -> np.ndarray:
        """
        Enhanced pipeline for retry on low-confidence OCR.
        Uses sharpen + Otsu + morphology.
        """
        img = self.to_grayscale(plate_crop)
        img = self.sharpen(img)
        img = self.gaussian_blur(img, ksize=3)
        img = self.otsu_threshold(img)
        img = self.morphology_open(img, ksize=2)
        img = self.morphology_close(img, ksize=2)
        return img
