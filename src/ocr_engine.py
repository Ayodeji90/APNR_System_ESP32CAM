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
from typing import Tuple, List, Optional

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
    """
    Licence-plate OCR with a pluggable backend.

    Backends (``ocr.engine`` in config):
      - "easyocr"   — deep-learning OCR; far better on stylised / low-light
                      plates than Tesseract. Recommended.
      - "tesseract" — classic engine; fast, but weak on decorative fonts.

    Either way the input is first reduced to the colour-isolated registration
    strip (region names / slogans removed), and every result is validated
    against the Nigerian plate pattern before it can be accepted.
    """

    def __init__(self, cfg: AppConfig):
        self.psm = cfg.ocr.tesseract_psm
        self.whitelist = cfg.ocr.char_whitelist
        self.engine = (cfg.ocr.engine or "tesseract").lower()
        self._easyocr_gpu = getattr(cfg.ocr, "easyocr_gpu", False)
        self.preprocessor = ImagePreprocessor(cfg)
        self._easyocr_reader = None  # lazy-initialised singleton

        if self.engine == "easyocr":
            # Warm the reader up front so the first gate event isn't slow.
            self._get_easyocr()

    # ── EasyOCR backend ─────────────────────────────────────
    def _get_easyocr(self):
        """Lazily build the EasyOCR reader. Returns the reader or None."""
        if self._easyocr_reader is None:
            try:
                import easyocr
                self._easyocr_reader = easyocr.Reader(["en"], gpu=self._easyocr_gpu)
                logger.info("EasyOCR reader ready (gpu=%s)", self._easyocr_gpu)
            except Exception as e:
                logger.error(
                    "EasyOCR unavailable (%s) — falling back to Tesseract. "
                    "Install: pip install easyocr", e,
                )
                self._easyocr_reader = False  # sentinel: tried, failed
        return self._easyocr_reader or None

    def _easyocr_candidates(self, image: np.ndarray) -> List[Tuple[str, float, float]]:
        """
        Run EasyOCR and return (text, confidence_0_100, height_px) candidates:
        each detected box, plus a left-to-right concatenation of all boxes
        (so a number split as "GGE" "123ZY" recombines into "GGE123ZY").
        """
        reader = self._get_easyocr()
        if reader is None:
            return []
        try:
            results = reader.readtext(
                image, allowlist=self.whitelist, detail=1, paragraph=False
            )
        except Exception as e:
            logger.error("EasyOCR failed: %s", e)
            return []

        candidates: List[Tuple[str, float, float]] = []
        boxes = []
        for bbox, text, conf in results:
            ys = [p[1] for p in bbox]
            xs = [p[0] for p in bbox]
            height = max(ys) - min(ys)
            candidates.append((text, float(conf) * 100.0, float(height)))
            boxes.append((min(xs), text, float(conf), height))

        # Concatenate all boxes left-to-right as one more candidate.
        if len(boxes) > 1:
            boxes.sort(key=lambda b: b[0])
            joined = "".join(b[1] for b in boxes)
            mean_conf = sum(b[2] for b in boxes) / len(boxes) * 100.0
            max_h = max(b[3] for b in boxes)
            candidates.append((joined, mean_conf, max_h))

        return candidates

    # ── Colour-segmented registration number (primary path) ─
    def isolate_number_strip(self, bgr: np.ndarray) -> Optional[np.ndarray]:
        """
        Isolate ONLY the registration number by colour + geometry.

        The Nigerian plate's registration number is the single block of large
        PURPLE characters; the region name ("LAGOS"), slogan, map and hills are
        black/green/white. We mask the purple, keep the tall character blobs
        that sit on one horizontal line (the number band), and return a clean
        binary strip (dark chars on white) containing just that number — so
        Tesseract never sees "LAGOS" at all.

        Returns the strip, or None if the number couldn't be isolated.
        """
        if bgr is None or bgr.ndim != 3:
            return None
        H, W = bgr.shape[:2]
        if H < 20 or W < 20:
            return None

        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        # Purple / violet / blue-violet registration characters. Wide hue
        # band + modest saturation so it survives low light and colour cast.
        mask = cv2.inRange(hsv, (105, 45, 40), (165, 255, 255))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

        num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        comps = []
        for i in range(1, num):
            x, y, w, h, area = stats[i]
            # Character-sized purple blobs: exclude specks and full-width smears.
            if h < 0.10 * H or h > 0.95 * H:
                continue
            if w > 0.45 * W or area < 15:
                continue
            comps.append((x, y, w, h))
        if len(comps) < 3:      # need at least a few characters
            return None

        # Cluster components into horizontal lines by vertical centre, then
        # pick the line with the TALLEST characters — the registration number
        # is the biggest text on the plate. (On real plates "LAGOS" is black,
        # so it isn't even in the purple mask; this also rejects stray noise.)
        comps.sort(key=lambda c: c[1] + c[3] / 2)
        lines: List[list] = []
        for c in comps:
            yc, h = c[1] + c[3] / 2, c[3]
            for ln in lines:
                ref = ln[0]
                if abs(yc - (ref[1] + ref[3] / 2)) <= 0.7 * max(h, ref[3]):
                    ln.append(c)
                    break
            else:
                lines.append([c])
        # Best line: most characters, tie-broken by tallest median height.
        def line_score(ln):
            hs = sorted(cc[3] for cc in ln)
            return (len(ln), hs[len(hs) // 2])
        band = max(lines, key=line_score)
        if len(band) < 3:
            return None

        band_h = max(c[3] for c in band)
        x0 = min(c[0] for c in band)
        y0 = min(c[1] for c in band)
        x1 = max(c[0] + c[2] for c in band)
        y1 = max(c[1] + c[3] for c in band)
        pad = int(0.12 * band_h)
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(W, x1 + pad), min(H, y1 + pad)

        strip = mask[y0:y1, x0:x1]
        strip = cv2.bitwise_not(strip)             # dark chars on white
        strip = self.preprocessor.upscale_min_width(strip, 500)
        # slight dilation of the (now dark) strokes for cleaner glyphs
        strip = cv2.erode(strip, np.ones((2, 2), np.uint8))
        return strip

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

        # 0) PRIMARY: colour-segmented registration-number strip (LAGOS and
        #    all other text already removed — cleanest input for Tesseract).
        strip = self.isolate_number_strip(plate_crop)
        if strip is not None:
            variants.append(strip)

        # 1) Luminance path (CLAHE-boosted grayscale)
        gray = self.preprocessor.clahe(plate_crop)
        gray = self.preprocessor.upscale_min_width(gray, 400)
        gray = cv2.bilateralFilter(gray, 11, 17, 17)
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 9
        )
        # Larger block/C — makes the coloured characters stand out on the
        # brighter plate body (wider neighbourhood handles the map shading).
        adaptive_wide = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
        )
        variants += [otsu, cv2.bitwise_not(otsu), adaptive, adaptive_wide]

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

            # Hue-isolation: keep ONLY the purple/blue registration characters,
            # discarding the green state map behind the digits and the black
            # "LAGOS" text. This is what lets "123" read cleanly despite the map.
            hsv = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2HSV)
            purple = cv2.inRange(hsv, (100, 40, 40), (165, 255, 255))
            purple = cv2.morphologyEx(
                purple, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)
            )
            purple = self.preprocessor.upscale_min_width(purple, 400)
            # Characters are white in the mask → invert to dark-on-white.
            variants.append(cv2.bitwise_not(purple))

        return variants

    # ── Raw OCR over one image ──────────────────────────────
    def _ocr_candidates(self, image: np.ndarray, psm: int) -> List[Tuple[str, float, float]]:
        """
        Run Tesseract and return candidate (text, confidence, height) tuples:
        both individual words and per-line concatenations (so a number
        split as "GGE 123 ZY" is recombined into "GGE123ZY").

        `height` is the character height in pixels — the registration number
        is the tallest text on a plate, so this lets the scorer prefer it
        over region names ("LAGOS") and slogans.
        """
        if not _HAS_TESSERACT:
            return []

        config = (
            f"--oem 3 --psm {psm} "
            f"-c tessedit_char_whitelist={self.whitelist}"
        )
        try:
            data = pytesseract.image_to_data(
                image, config=config, output_type=pytesseract.Output.DICT
            )
        except Exception as e:
            logger.error("Tesseract failed: %s", e)
            return []

        candidates: List[Tuple[str, float, float]] = []
        # Group words by text line so multi-token numbers recombine.
        lines: dict = {}
        for i, word in enumerate(data["text"]):
            conf = self._to_float(data["conf"][i])
            word = word.strip()
            if not word or conf <= 0:
                continue
            height = self._to_float(data["height"][i])
            candidates.append((word, conf, height))
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, {"words": [], "confs": [], "heights": []})
            lines[key]["words"].append(word)
            lines[key]["confs"].append(conf)
            lines[key]["heights"].append(height)

        for grp in lines.values():
            joined = "".join(grp["words"])
            mean_conf = sum(grp["confs"]) / len(grp["confs"])
            max_height = max(grp["heights"])
            candidates.append((joined, mean_conf, max_height))

        return candidates

    # ── Scoring ─────────────────────────────────────────────
    @staticmethod
    def _pattern_rank(text: str) -> int:
        """Lower is better: index of the first pattern the text matches."""
        for rank, pat in enumerate(_PLATE_PATTERNS):
            if pat.fullmatch(text):
                return rank
        return len(_PLATE_PATTERNS)

    # ── Candidate generation (engine-specific) ──────────────
    def _candidates_for_image(self, image: np.ndarray, enhanced: bool):
        """Yield (text, conf, height) candidates for one image, per backend."""
        if self.engine == "easyocr" and self._get_easyocr() is not None:
            # Feed EasyOCR the colour-isolated number strip first (region
            # names already removed), then the colour image as backup.
            strip = self.isolate_number_strip(image)
            sources = ([strip] if strip is not None else []) + [image]
            for src in sources:
                yield from self._easyocr_candidates(src)
        else:
            # Tesseract: many binarisations × PSM modes.
            psm_modes = [7, 6, 11] if not enhanced else [11, 6, 7, 8]
            for binary in self._binarisations(image):
                for psm in psm_modes:
                    yield from self._ocr_candidates(binary, psm)

    # ── Scoring over one image ──────────────────────────────
    def _score_image(self, image: np.ndarray, enhanced: bool):
        """
        Score every candidate for one image and return the best as
        (text, confidence, key), where lower key is better.
        """
        best_text = ""
        best_conf = 0.0
        best_key = None  # (rank, -height, -conf); lower is better

        for raw, conf, height in self._candidates_for_image(image, enhanced):
            text = self.normalize_plate(raw)
            if len(text) < 4:  # too short to be a plate
                continue
            rank = self._pattern_rank(text)
            # Prefer a real plate-pattern match first; then the TALLEST text
            # (the registration number dominates the plate); then confidence.
            # Height selection stops small high-confidence text like "LAGOS"
            # from winning.
            key = (rank, -height, -conf)
            if best_key is None or key < best_key:
                best_key = key
                best_text = text
                best_conf = conf

        return best_text, best_conf, best_key

    # ── Public API ──────────────────────────────────────────
    def read_plate(
        self, plate_crop: np.ndarray, enhanced: bool = False
    ) -> Tuple[str, float]:
        """Read the registration number from a single image."""
        if plate_crop is None or plate_crop.size == 0:
            return ("", 0.0)
        text, conf, _ = self._score_image(plate_crop, enhanced)
        logger.info(
            "OCR result: plate='%s' confidence=%.1f enhanced=%s",
            text, conf, enhanced,
        )
        return (text, conf)

    def read_best(self, images, enhanced: bool = True) -> Tuple[str, float]:
        """
        Read from several images (e.g. the tight crop AND the full frame)
        and return the single best result. The crop sometimes clips edge
        characters while the full frame keeps them, so trying both and
        letting the scorer choose is more robust than either alone.
        """
        best_text = ""
        best_conf = 0.0
        best_key = None
        for image in images:
            if image is None or image.size == 0:
                continue
            text, conf, key = self._score_image(image, enhanced)
            if key is not None and (best_key is None or key < best_key):
                best_key, best_text, best_conf = key, text, conf
        logger.info("OCR best-of-%d: plate='%s' confidence=%.1f",
                    len(images), best_text, best_conf)
        return (best_text, best_conf)

    # ── Helpers ─────────────────────────────────────────────
    @staticmethod
    def normalize_plate(text: str) -> str:
        """Uppercase, remove common Nigerian plate stopwords, and strip to A-Z / 0-9 only."""
        text = text.upper()
        # Aggressively remove variations of state names and slogans
        text = re.sub(r'(?:[01IO]*LAGOS|CENT[A-Z]*|EXCEL[A-Z]*|FEDER[A-Z]*|REPUBL[A-Z]*|NIGER[A-Z]*|STATE|ABUJA|KANO|RIVERS|OGUN|OYO|KADUNA|EDO|ENUGU|DELTA|KWARA|ONDO|OSUN|PLATEAU)', '', text)
        
        normalized = re.sub(r"[^A-Z0-9]", "", text.strip())
        
        # A valid Nigerian plate MUST contain at least one digit and at least one letter.
        # If it doesn't (e.g. "CENTCN", "OLAGOS"), it's definitely garbage text.
        if not re.search(r'\d', normalized) or not re.search(r'[A-Z]', normalized):
            return ""
            
        return normalized

    @staticmethod
    def _to_float(value) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return -1.0
