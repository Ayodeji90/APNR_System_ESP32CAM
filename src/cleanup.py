"""
ANPR System — Event Image Cleanup

Removes event image directories older than the configured retention period.
Called from main.py at startup and can be run standalone.
"""

import os
import shutil
import logging
from datetime import datetime, timedelta

from src.config import AppConfig, resolve_path

logger = logging.getLogger(__name__)


def cleanup_old_events(cfg: AppConfig) -> int:
    """
    Delete event image directories older than *event_retention_days*.

    Event directories are named YYYY-MM-DD under the events_dir path.
    Returns the number of directories removed.

    If event_retention_days is 0 or negative, no cleanup is performed.
    """
    retention_days = cfg.paths.event_retention_days
    if retention_days <= 0:
        logger.debug("Event cleanup disabled (retention_days=%d).", retention_days)
        return 0

    events_dir = resolve_path(cfg, cfg.paths.events_dir)
    if not os.path.isdir(events_dir):
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0

    for entry in os.listdir(events_dir):
        entry_path = os.path.join(events_dir, entry)
        if not os.path.isdir(entry_path):
            continue

        # Parse directory name as date
        try:
            dir_date = datetime.strptime(entry, "%Y-%m-%d")
        except ValueError:
            continue  # skip non-date directories

        if dir_date < cutoff:
            try:
                shutil.rmtree(entry_path)
                removed += 1
                logger.info("Cleaned up old event directory: %s", entry)
            except OSError as e:
                logger.warning("Failed to remove %s: %s", entry_path, e)

    if removed:
        logger.info("Event cleanup: removed %d directories older than %d days.", removed, retention_days)

    return removed
