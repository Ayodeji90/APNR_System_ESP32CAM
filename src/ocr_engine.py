"""
ANPR System — OCR Engine (Tesseract)

Reads a plate registration number from a cropped (or full-frame) plate
image.  Robust to dark, stylised, multi-line plates:

  - CLAHE contrast boost + cubic upscaling
  - several binarisations tried (Otsu, inverted Otsu, adaptive)
  - several Tesseract page-segmentation modes tried (block / line / sparse)
  - candidate strings scored against a licence-plate pattern, so the
    registration number is extracted even when the plate also carries
    region names and slogans (e.g. "LAGOS", "CENTRE OF EXCELLENCE").
"""

import re
import logging
from typing import Tuple, List

import cv2
import numpy as np

from src.config import AppConfig
from src.preprocessing import ImagePreprocessor

logger = logging.getLogger(__name__)

# ── Try to import pytesseract ───────────────────────────────
try:
    import pytesseract
    _HAS_TESSERACT = True
except ImportError:
    _HAS_TESSERACT = False
    logger.warning(
        "pytesseract not available — OCR will return empty results. "
        "Install: pip install pytesseract  +  sudo apt install tesseract-ocr"
    )

# Plate-number patterns, most specific first. GGE123ZY (Nigerian current
# format: 3 letters, 3 digits, 2 letters) matches the first pattern.
_PLATE_PATTERNS = [
    re.compile(r"[A-Z]{3}[0-9]{2,3}[A-Z]{2}"),   # AAA000AA (Nigerian)
    re.compile(r"[A-Z]{2,3}[0-9]{2,4}[A-Z]{1,3}"),  # looser letter-digit-letter
    re.compile(r"[A-Z0-9]{5,9}"),                 # generic alnum fallback
]


class OcrEngine:
    """Tesseract-based OCR for licence-plate text reading."""

    def __init__(self, cfg: AppConfig):
        self.psm = cfg.ocr.tesseract_psm
        self.whitelist = cfg.ocr.char_whitelist
        self.preprocessor = ImagePreprocessor(cfg)

    # ── Binarisation variants ───────────────────────────────
    def _binarisations(self, plate_crop: np.ndarray) -> List[np.ndarray]:
        """Produce several binary images to feed Tesseract.

        Includes colour-aware channels: coloured plate characters (e.g. the
        purple "GGE-123ZY" on Lagos plates) have poor luminance contrast on
        white, but pop strongly in the green channel or the per-pixel min of
        the BGR channels. Both black region names and coloured registration
        numbers come out dark-on-light this way.
        """
        variants: List[np.ndarray] = []

        # 1) Luminance path (CLAHE-boosted grayscale)
        gray = self.preprocessor.clahe(plate_crop)
        gray = self.preprocessor.upscale_min_width(gray, 400)
        gray = cv2.bilateralFilter(gray, 11, 17, 17)
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 9
        )
        variants += [otsu, cv2.bitwise_not(otsu), adaptive]

        # 2) Colour-aware paths (only for BGR input)
        if plate_crop.ndim == 3:
            # Per-pixel min channel: any saturated colour OR black → dark;
            # white → bright. Great universal "coloured text on white" mask.
            min_ch = plate_crop.min(axis=2).astype(np.uint8)
            min_ch = self.preprocessor.upscale_min_width(min_ch, 400)
            _, o_min = cv2.threshold(min_ch, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            # Green channel: best contrast specifically for purple/red text.
            green = self.preprocessor.upscale_min_width(plate_crop[:, :, 1], 400)
            _, o_green = cv2.threshold(green, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            variants += [o_min, o_green]

        return variants

    # ── Raw OCR over one image ──────────────────────────────
    def _ocr_candidates(self, image: np.ndarray, psm: int) -> List[Tuple[str, float]]:
        """
        Run Tesseract and return candidate (text, confidence) tuples:
        both individual words and per-line concatenations (so a number
        split as "GGE 123 ZY" is recombined into "GGE123ZY").
        """
        if not _HAS_TESSERACT:
            return []

        config = f"--psm {psm} -c tessedit_char_whitelist={self.whitelist}"
        try:
            data = pytesseract.image_to_data(
                image, config=config, output_type=pytesseract.Output.DICT
            )
        except Exception as e:
            logger.error("Tesseract failed: %s", e)
            return []

        candidates: List[Tuple[str, float]] = []
        # Group words by text line so multi-token numbers recombine.
        lines: dict = {}
        for i, word in enumerate(data["text"]):
            conf = self._to_float(data["conf"][i])
            word = word.strip()
            if not word or conf <= 0:
                continue
            # Individual word candidate
            candidates.append((word, conf))
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, {"words": [], "confs": []})
            lines[key]["words"].append(word)
            lines[key]["confs"].append(conf)

        for grp in lines.values():
            joined = "".join(grp["words"])
            mean_conf = sum(grp["confs"]) / len(grp["confs"])
            candidates.append((joined, mean_conf))

        return candidates

    # ── Scoring ─────────────────────────────────────────────
    @staticmethod
    def _pattern_rank(text: str) -> int:
        """Lower is better: index of the first pattern the text matches."""
        for rank, pat in enumerate(_PLATE_PATTERNS):
            if pat.fullmatch(text):
                return rank
        return len(_PLATE_PATTERNS)

    # ── Public API ──────────────────────────────────────────
    def read_plate(
        self, plate_crop: np.ndarray, enhanced: bool = False
    ) -> Tuple[str, float]:
        """
        Read the registration number from a plate crop (or full frame).

        Returns:
            (normalized_plate_text, confidence_0_to_100)
        """
        if plate_crop is None or plate_crop.size == 0:
            return ("", 0.0)

        psm_modes = [7, 6, 11] if not enhanced else [11, 6, 7, 8]

        best_text = ""
        best_conf = 0.0
        best_rank = len(_PLATE_PATTERNS) + 1

        for binary in self._binarisations(plate_crop):
            for psm in psm_modes:
                for raw, conf in self._ocr_candidates(binary, psm):
                    text = self.normalize_plate(raw)
                    if len(text) < 4:  # too short to be a plate
                        continue
                    rank = self._pattern_rank(text)
                    # Prefer better pattern match; break ties by confidence.
                    if rank < best_rank or (rank == best_rank and conf > best_conf):
                        best_rank = rank
                        best_text = text
                        best_conf = conf

        logger.info(
            "OCR result: plate='%s' confidence=%.1f rank=%d enhanced=%s",
            best_text, best_conf, best_rank, enhanced,
        )
        return (best_text, best_conf)

    # ── Helpers ─────────────────────────────────────────────
    @staticmethod
    def normalize_plate(text: str) -> str:
        """Uppercase and strip to A–Z / 0–9 only."""
        return re.sub(r"[^A-Z0-9]", "", text.upper().strip())

    @staticmethod
    def _to_float(value) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return -1.0
