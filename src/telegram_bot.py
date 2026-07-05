"""
ANPR System — Telegram Notifier

Sends event notifications, alerts, and images to configured Telegram
chat IDs.  All methods are no-ops when Telegram is disabled or the
bot_token is empty, so the gate loop is never blocked by network issues.
"""

import asyncio
import logging
import os
from typing import Optional, Dict, Any

from src.config import AppConfig

logger = logging.getLogger(__name__)

# ── Try importing telegram library ─────────────────────────────
try:
    from telegram import Bot
    from telegram.error import TelegramError
    _HAS_TELEGRAM = True
except ImportError:
    _HAS_TELEGRAM = False
    logger.warning(
        "python-telegram-bot not installed — Telegram notifications disabled. "
        "Run: pip install 'python-telegram-bot==21.*'"
    )


class TelegramNotifier:
    """
    Outbound Telegram notification helper.

    All public methods are synchronous wrappers around async Telegram
    API calls.  They catch all exceptions so a Telegram outage or
    misconfiguration never crashes the gate loop.
    """

    def __init__(self, cfg: AppConfig):
        self._enabled = (
            cfg.telegram.enabled
            and bool(cfg.telegram.bot_token)
            and _HAS_TELEGRAM
        )
        self._token = cfg.telegram.bot_token
        self._chat_ids = list(cfg.telegram.allowed_chat_ids)
        self._notify_allow = cfg.telegram.notify_on_allow
        self._notify_deny = cfg.telegram.notify_on_deny
        self._notify_unknown = cfg.telegram.notify_on_unknown
        self._send_image = cfg.telegram.send_image

        if self._enabled:
            logger.info(
                "Telegram notifier active — broadcasting to %d chat(s)",
                len(self._chat_ids),
            )
        else:
            reason = (
                "disabled in config" if not cfg.telegram.enabled
                else "token not set" if not cfg.telegram.bot_token
                else "python-telegram-bot not installed"
            )
            logger.info("Telegram notifier inactive (%s).", reason)

    # ── Public sync API ─────────────────────────────────────────
    def send_message(self, text: str) -> None:
        """Send a plain text message to all allowed chat IDs."""
        if not self._enabled:
            return
        self._run(self._async_send_message(text))

    def send_image(self, image_path: str, caption: str = "") -> None:
        """Send a photo with optional caption to all allowed chat IDs."""
        if not self._enabled or not self._send_image:
            return
        if not image_path or not os.path.isfile(image_path):
            logger.debug("Telegram: image not found at %s — sending text only", image_path)
            if caption:
                self.send_message(caption)
            return
        self._run(self._async_send_image(image_path, caption))

    def notify_event(
        self,
        plate: str,
        decision: str,
        ocr_conf: float,
        detection_conf: float,
        image_path: str = "",
        note: str = "",
    ) -> None:
        """
        Format and send a complete gate event card.
        Respects notify_on_allow/deny/unknown toggles.
        """
        if not self._enabled:
            return

        decision_upper = decision.upper()
        if decision_upper == "ALLOW" and not self._notify_allow:
            return
        if decision_upper == "DENY" and not self._notify_deny:
            return
        if decision_upper == "UNKNOWN" and not self._notify_unknown:
            return

        # Build emoji prefix
        emoji = {"ALLOW": "✅", "DENY": "🚫", "UNKNOWN": "⚠️"}.get(decision_upper, "🔔")

        plate_display = plate if plate else "UNREADABLE"
        text = (
            f"{emoji} *Gate Event*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🚗 *Plate:* `{plate_display}`\n"
            f"📋 *Decision:* *{decision_upper}*\n"
            f"🎯 *OCR Confidence:* {ocr_conf:.1f}%\n"
            f"📐 *Detection Score:* {detection_conf:.2f}\n"
        )
        if note:
            text += f"📝 *Note:* {note}\n"

        # Send with image if available, else text only
        if self._send_image and image_path and os.path.isfile(image_path):
            self._run(self._async_send_image(image_path, caption=text, parse_mode="Markdown"))
        else:
            self._run(self._async_send_message(text, parse_mode="Markdown"))

    def notify_boot(self) -> None:
        """Send a startup notification."""
        if not self._enabled:
            return
        self.send_message(
            "🟢 *ANPR Gate System started*\n"
            "All services initialised and ready.",
        )

    def notify_health(self, status: Dict[str, Any]) -> None:
        """Send a system health snapshot."""
        if not self._enabled:
            return
        lines = ["🔧 *System Health*\n━━━━━━━━━━━━━━━━━━"]
        for key, val in status.items():
            lines.append(f"• *{key}:* {val}")
        self._run(self._async_send_message("\n".join(lines), parse_mode="Markdown"))

    # ── Async internals ─────────────────────────────────────────
    async def _async_send_message(
        self, text: str, parse_mode: Optional[str] = None
    ) -> None:
        bot = Bot(token=self._token)
        for chat_id in self._chat_ids:
            try:
                async with bot:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=parse_mode,
                    )
                logger.debug("Telegram message sent to %s", chat_id)
            except Exception as exc:
                logger.error("Telegram send_message error (chat=%s): %s", chat_id, exc)

    async def _async_send_image(
        self,
        image_path: str,
        caption: str = "",
        parse_mode: Optional[str] = None,
    ) -> None:
        bot = Bot(token=self._token)
        for chat_id in self._chat_ids:
            try:
                async with bot:
                    with open(image_path, "rb") as photo:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=photo,
                            caption=caption[:1024],   # Telegram caption limit
                            parse_mode=parse_mode,
                        )
                logger.debug("Telegram photo sent to %s", chat_id)
            except Exception as exc:
                logger.error("Telegram send_photo error (chat=%s): %s", chat_id, exc)

    # ── asyncio runner ──────────────────────────────────────────
    @staticmethod
    def _run(coro) -> None:
        """Run a coroutine from sync context without blocking the event loop."""
        def _done_callback(task):
            """Log exceptions from fire-and-forget tasks."""
            if task.cancelled():
                return
            exc = task.exception()
            if exc:
                logger.error("Telegram async task error: %s", exc)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already inside an event loop (e.g. command handler thread)
                task = asyncio.ensure_future(coro)
                task.add_done_callback(_done_callback)
            else:
                loop.run_until_complete(coro)
        except RuntimeError:
            # No event loop — create a fresh one (common in threads)
            asyncio.run(coro)
        except Exception as exc:
            logger.error("Telegram async runner error: %s", exc)
