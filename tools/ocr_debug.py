"""
OCR / plate-detection debug tool.

Runs the full detection + OCR pipeline on a single image and prints every
intermediate result, so you can see exactly where a plate read succeeds or
fails and tune parameters against a real capture.

Usage (from the project root, inside the venv):

    python -m tools.ocr_debug data/events/2026-07-06/UNKNOWN_024729_016536.jpg

It also writes annotated debug images next to the input:
    <name>.debug_crop.jpg   — the detected/deskewed plate crop
    <name>.debug_bin.jpg    — the binarised image fed to Tesseract
"""

import os
import sys
import logging

import cv2

# Allow running as `python tools/ocr_debug.py <img>` too.
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from src.config import load_config
from src.plate_detector import PlateDetector
from src.ocr_engine import OcrEngine

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    img_path = sys.argv[1]
    frame = cv2.imread(img_path)
    if frame is None:
        print(f"ERROR: could not read image: {img_path}")
        sys.exit(1)

    print(f"\n=== Image: {img_path}  ({frame.shape[1]}x{frame.shape[0]}) ===\n")

    cfg = load_config()
    detector = PlateDetector(cfg)
    ocr = OcrEngine(cfg)

    base, _ = os.path.splitext(img_path)

    # ── Detection ───────────────────────────────────────────
    crop, det_conf = detector.detect(frame)
    print(f"Detection confidence: {det_conf:.2f}")
    print(f"min_detection_confidence: {cfg.detection.min_detection_confidence}")

    target = crop
    if crop is not None:
        cv2.imwrite(base + ".debug_crop.jpg", crop)
        print(f"Crop saved: {base}.debug_crop.jpg  ({crop.shape[1]}x{crop.shape[0]})")
    else:
        print("No crop detected — will OCR full frame.")
        target = frame

    # Save the first binarisation Tesseract sees (for visual inspection).
    try:
        bins = ocr._binarisations(target)
        cv2.imwrite(base + ".debug_bin.jpg", bins[0])
        print(f"Binarised image saved: {base}.debug_bin.jpg")
    except Exception as e:
        print(f"Could not build binarisation preview: {e}")

    # ── OCR (crop, then full-frame fallback) ────────────────
    print("\n--- OCR on detected crop ---")
    text, conf = (ocr.read_plate(crop, enhanced=True) if crop is not None else ("", 0.0))
    print(f"  plate='{text}'  ocr_conf={conf:.1f}")

    if not text:
        print("\n--- OCR on full frame (fallback) ---")
        text, conf = ocr.read_plate(frame, enhanced=True)
        print(f"  plate='{text}'  ocr_conf={conf:.1f}")

    print("\n=== FINAL ===")
    print(f"  plate_text = '{text}'")
    print(f"  ocr_conf   = {conf:.1f}   (min_ocr_confidence={cfg.detection.min_ocr_confidence})")
    print(f"  whitelisted = {ocr.normalize_plate(text) and _whitelisted(cfg, text)}")
    print()


def _whitelisted(cfg, text: str) -> bool:
    try:
        from src.database import Database
        return Database(cfg).is_whitelisted(text)
    except Exception:
        return False


if __name__ == "__main__":
    main()
