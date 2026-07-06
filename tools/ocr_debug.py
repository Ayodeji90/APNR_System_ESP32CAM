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

    # Save the colour-isolated number strip (the primary OCR input).
    strip = ocr.isolate_number_strip(target if crop is not None else frame)
    if strip is None and crop is not None:
        strip = ocr.isolate_number_strip(frame)
    if strip is not None:
        cv2.imwrite(base + ".debug_strip.jpg", strip)
        print(f"Isolated number strip saved: {base}.debug_strip.jpg  ({strip.shape[1]}x{strip.shape[0]})")
        st_text, st_conf, _ = ocr._score_image(strip, enhanced=True)
        print(f"  strip OCR → '{ocr.normalize_plate(st_text)}'  conf={st_conf:.1f}")
    else:
        print("Number strip: could NOT isolate purple characters "
              "(check the hue range or lighting).")

    # Save the first full binarisation too (for visual inspection).
    try:
        bins = ocr._binarisations(target)
        cv2.imwrite(base + ".debug_bin.jpg", bins[0])
        print(f"Binarised image saved: {base}.debug_bin.jpg")
    except Exception as e:
        print(f"Could not build binarisation preview: {e}")

    # ── Dump ALL raw OCR candidates (the key diagnostic) ────
    # Shows whether the registration number is being READ at all vs being
    # read but not selected.
    _dump_candidates(ocr, target, "detected crop" if crop is not None else "full frame")
    if crop is not None:
        _dump_candidates(ocr, frame, "full frame")

    # ── Pipeline result: best of crop + full frame (matches app.py) ──
    text, conf = ocr.read_best([crop, frame], enhanced=True)

    print("\n=== FINAL (matches live pipeline) ===")
    print(f"  plate_text = '{text}'")
    print(f"  ocr_conf   = {conf:.1f}   (min_ocr_confidence={cfg.detection.min_ocr_confidence})")
    match = _whitelist_match(cfg, text)
    fd = cfg.detection.whitelist_fuzzy_distance
    if match and match == text:
        print(f"  whitelist  = EXACT match '{match}'  → ALLOW")
    elif match:
        print(f"  whitelist  = FUZZY match '{match}' (within {fd} edits)  → ALLOW")
    else:
        print(f"  whitelist  = no match within {fd} edits  → DENY/UNKNOWN")
    print()


def _whitelist_match(cfg, text: str):
    if not text:
        return None
    try:
        from src.database import Database
        return Database(cfg).find_whitelist_match(text, cfg.detection.whitelist_fuzzy_distance)
    except Exception:
        return None


def _dump_candidates(ocr, image, label: str) -> None:
    """Print every OCR candidate from every binarisation × PSM combo."""
    print(f"\n--- ALL raw candidates on {label} ---")
    seen = {}
    for bi, binary in enumerate(ocr._binarisations(image)):
        for psm in (7, 6, 11, 8):
            for raw, conf, height in ocr._ocr_candidates(binary, psm):
                text = ocr.normalize_plate(raw)
                if len(text) < 4:
                    continue
                rank = ocr._pattern_rank(text)
                # keep the best-scoring appearance of each distinct text
                key = (rank, -height, -conf)
                if text not in seen or key < seen[text][0]:
                    seen[text] = (key, conf, height, rank, bi, psm)
    if not seen:
        print("  (no candidates ≥4 chars)")
        return
    # Sort by the same key the engine uses to pick the winner
    for text, (key, conf, height, rank, bi, psm) in sorted(seen.items(), key=lambda kv: kv[1][0]):
        print(f"  '{text:<10}' rank={rank} height={height:>4.0f}px "
              f"conf={conf:>5.1f}  [bin#{bi} psm{psm}]")




if __name__ == "__main__":
    main()
