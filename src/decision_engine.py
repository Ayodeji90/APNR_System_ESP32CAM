"""
ANPR System — Decision Engine

Takes OCR results + confidence scores and produces an access decision:
ALLOW, DENY, or UNKNOWN.
"""

import logging
from enum import Enum
from typing import NamedTuple

from src.config import AppConfig
from src.database import Database

logger = logging.getLogger(__name__)


class Decision(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"


class DecisionResult(NamedTuple):
    decision: Decision
    plate_text: str
    reason: str


class DecisionEngine:
    """Evaluates plate text + confidence against whitelist and thresholds."""

    def __init__(self, cfg: AppConfig, db: Database):
        self.min_detection_conf = cfg.detection.min_detection_confidence
        self.min_ocr_conf = cfg.detection.min_ocr_confidence
        self.fuzzy_distance = cfg.detection.whitelist_fuzzy_distance
        self.db = db

    def decide(
        self,
        plate_text: str,
        ocr_confidence: float,
        detection_confidence: float,
    ) -> DecisionResult:
        """
        Make an access decision.

        Logic:
          1. If plate_text is empty → UNKNOWN
          2. If detection confidence < threshold → UNKNOWN
          3. If OCR confidence < threshold → UNKNOWN
          4. If plate on whitelist → ALLOW
          5. Otherwise → DENY
        """
        # Empty plate
        if not plate_text:
            reason = "No plate text detected"
            logger.info("Decision: UNKNOWN — %s", reason)
            return DecisionResult(Decision.UNKNOWN, plate_text, reason)

        # Low detection confidence
        if detection_confidence < self.min_detection_conf:
            reason = (
                f"Detection confidence too low "
                f"({detection_confidence:.2f} < {self.min_detection_conf:.2f})"
            )
            logger.info("Decision: UNKNOWN — %s", reason)
            return DecisionResult(Decision.UNKNOWN, plate_text, reason)

        # Low OCR confidence
        if ocr_confidence < self.min_ocr_conf:
            reason = (
                f"OCR confidence too low "
                f"({ocr_confidence:.1f} < {self.min_ocr_conf:.1f})"
            )
            logger.info("Decision: UNKNOWN — %s", reason)
            return DecisionResult(Decision.UNKNOWN, plate_text, reason)

        # Check whitelist (exact, then fuzzy to absorb OCR errors)
        match = self.db.find_whitelist_match(plate_text, self.fuzzy_distance)
        if match:
            if match == plate_text:
                reason = f"Plate {plate_text} is on the whitelist"
            else:
                reason = f"Plate {plate_text} matched whitelist entry {match} (fuzzy)"
            logger.info("Decision: ALLOW — %s", reason)
            return DecisionResult(Decision.ALLOW, match, reason)

        # Not on whitelist
        reason = f"Plate {plate_text} is NOT on the whitelist"
        logger.info("Decision: DENY — %s", reason)
        return DecisionResult(Decision.DENY, plate_text, reason)
