"""Tests for src.ocr_engine — OCR text normalization."""

import pytest

from src.ocr_engine import OcrEngine


class TestNormalizePlate:
    """Test the static normalize_plate method (no Tesseract needed)."""

    def test_uppercase(self):
        assert OcrEngine.normalize_plate("abc123") == "ABC123"

    def test_strip_whitespace(self):
        assert OcrEngine.normalize_plate("  ABC 123  ") == "ABC123"

    def test_remove_special_chars(self):
        assert OcrEngine.normalize_plate("AB-C.1 2/3") == "ABC123"

    def test_empty_string(self):
        assert OcrEngine.normalize_plate("") == ""

    def test_only_special_chars(self):
        assert OcrEngine.normalize_plate("---...   ") == ""

    def test_already_clean(self):
        assert OcrEngine.normalize_plate("XYZ789") == "XYZ789"

    def test_mixed_case_plates(self):
        assert OcrEngine.normalize_plate("lAg 234 Bc") == "LAG234BC"

    def test_rejects_pure_region_name(self):
        # No digits -> not a plate, regardless of how confidently it read.
        assert OcrEngine.normalize_plate("LAGOS") == ""
        assert OcrEngine.normalize_plate("FEDERAL REPUBLIC OF NIGERIA") == ""

    def test_preserves_state_prefixed_real_plates(self):
        # Regression test: normalize_plate must NOT strip state-name
        # substrings from real plate numbers. Several states' plates
        # literally begin with their own abbreviation (Edo, Oyo, Kano, ...);
        # an earlier version of this function blindly deleted those
        # substrings and corrupted the plate (e.g. "EDO123XY" -> "123XY").
        assert OcrEngine.normalize_plate("EDO123XY") == "EDO123XY"
        assert OcrEngine.normalize_plate("OYO456AB") == "OYO456AB"
        assert OcrEngine.normalize_plate("KANO12CD") == "KANO12CD"
