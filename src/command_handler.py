"""
ANPR System — Telegram Command Handler

Starts a python-telegram-bot polling Application in a background
daemon thread.  Receives inbound Telegram commands and maps them to
gate actions, whitelist management, and system queries.

Security: every handler checks the sender's chat_id against the
allowed list in config. Unknown users receive a silent rejection.
"""

import asyncio
import signal
import logging
import threading
from datetime import datetime
from typing import Optional, Any

from src.config import AppConfig

logger = logging.getLogger(__name__)

# ── Try importing telegram library ─────────────────────────────
try:
    from telegram import Update
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
    )
    _HAS_TELEGRAM = True
except ImportError:
    # Stub types so method signatures in the class body parse at import time
    # even when python-telegram-bot is not installed.
    class _Stub:
        DEFAULT_TYPE = Any
    Update = Any
    ContextTypes = _Stub
    Application = Any
    CommandHandler = Any
    _HAS_TELEGRAM = False


class TelegramCommandHandler:
    """
    Inbound Telegram command processor.

    Runs in a dedicated daemon thread.  Requires references to
    the actuator, database, camera, and state machine so commands
    can trigger real gate actions.
    """

    def __init__(self, cfg: AppConfig, db, actuator, camera, state_machine):
        self._enabled = (
            cfg.telegram.enabled
            and bool(cfg.telegram.bot_token)
            and _HAS_TELEGRAM
        )
        self._token = cfg.telegram.bot_token
        self._allowed = set(int(cid) for cid in cfg.telegram.allowed_chat_ids)
        self._db = db
        self._actuator = actuator
        self._camera = camera
        self._sm = state_machine

        self._app: Optional[object] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._started_at = datetime.now()

        if not self._enabled:
            reason = (
                "disabled in config" if not cfg.telegram.enabled
                else "token not set" if not cfg.telegram.bot_token
                else "python-telegram-bot not installed"
            )
            logger.info("Telegram command handler inactive (%s).", reason)

    # ── Auth guard ──────────────────────────────────────────────
    def _is_allowed(self, chat_id: int) -> bool:
        return chat_id in self._allowed

    def _auth_guard(self, fn):
        """Decorator that silently ignores unauthorized senders."""
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat_id = update.effective_chat.id
            if not self._is_allowed(chat_id):
                logger.warning("Rejected Telegram command from chat_id=%s", chat_id)
                return
            return await fn(update, context)
        wrapper.__name__ = fn.__name__
        return wrapper

    # ── Command handlers ────────────────────────────────────────
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "🤖 *ANPR Gate Bot — Commands*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 `/status` — System state & uptime\n"
            "📋 `/last_event` — Most recent detection\n"
            "📸 `/snapshot` — Capture & send live image\n"
            "\n🚧 *Gate Control*\n"
            "▶️ `/open_gate` — Open barrier\n"
            "⏹️ `/close_gate` — Close barrier\n"
            "\n📝 *Whitelist*\n"
            "➕ `/add_plate ABC123` — Add plate\n"
            "➖ `/remove_plate ABC123` — Remove plate\n"
            "📄 `/list_plates` — Show all approved plates"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uptime = datetime.now() - self._started_at
        h, remainder = divmod(int(uptime.total_seconds()), 3600)
        m, s = divmod(remainder, 60)

        state_name = "UNKNOWN"
        if self._sm and hasattr(self._sm, "state"):
            state_name = self._sm.state.name

        today_count = self._db.get_today_event_count()
        total_count = self._db.get_event_count()
        barrier = "🟢 OPEN" if (self._actuator and self._actuator.is_open) else "🔴 CLOSED"

        text = (
            f"📊 *System Status*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ *State:* `{state_name}`\n"
            f"🚧 *Barrier:* {barrier}\n"
            f"⏱️ *Uptime:* {h:02d}h {m:02d}m {s:02d}s\n"
            f"📅 *Events today:* {today_count}\n"
            f"📦 *Total events:* {total_count}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        self._log_command(update.effective_chat.id, "status", "", "ok")

    async def _cmd_last_event(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        events = self._db.get_recent_events(limit=1)
        if not events:
            await update.message.reply_text("📭 No events recorded yet.")
            return

        ev = events[0]
        plate = ev.get("plate_text") or "UNREADABLE"
        decision = ev.get("decision", "UNKNOWN")
        emoji = {"ALLOW": "✅", "DENY": "🚫", "UNKNOWN": "⚠️"}.get(decision.upper(), "🔔")
        text = (
            f"{emoji} *Last Detection*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🚗 *Plate:* `{plate}`\n"
            f"📋 *Decision:* *{decision}*\n"
            f"🎯 *OCR Confidence:* {ev.get('ocr_confidence', 0):.1f}%\n"
            f"📐 *Detection Score:* {ev.get('detection_confidence', 0):.2f}\n"
            f"🕐 *Time:* {ev.get('timestamp', 'N/A')}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        self._log_command(update.effective_chat.id, "last_event", "", "ok")

    async def _cmd_snapshot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📸 Capturing image …")
        try:
            import cv2, tempfile, os
            frame = self._camera.capture_frame()
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            cv2.imwrite(tmp.name, frame)
            with open(tmp.name, "rb") as photo:
                await update.message.reply_photo(photo=photo, caption="📸 Live snapshot")
            os.unlink(tmp.name)
            self._log_command(update.effective_chat.id, "snapshot", "", "ok")
        except Exception as exc:
            logger.error("Snapshot command failed: %s", exc)
            await update.message.reply_text(f"❌ Snapshot failed: {exc}")

    async def _cmd_open_gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            self._actuator.open_barrier()
            await update.message.reply_text("✅ Gate opened remotely.")
            self._log_command(update.effective_chat.id, "open_gate", "", "opened")
            logger.info("Gate opened via Telegram by chat_id=%s", update.effective_chat.id)
        except Exception as exc:
            await update.message.reply_text(f"❌ Failed to open gate: {exc}")

    async def _cmd_close_gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            self._actuator.close_barrier()
            await update.message.reply_text("✅ Gate closed.")
            self._log_command(update.effective_chat.id, "close_gate", "", "closed")
        except Exception as exc:
            await update.message.reply_text(f"❌ Failed to close gate: {exc}")

    async def _cmd_add_plate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: `/add_plate ABC123`", parse_mode="Markdown"
            )
            return
        plate = args[0].upper().strip()
        try:
            self._db.add_vehicle(plate)
            await update.message.reply_text(f"✅ `{plate}` added to whitelist.", parse_mode="Markdown")
            self._log_command(update.effective_chat.id, "add_plate", plate, "added")
        except Exception as exc:
            await update.message.reply_text(f"❌ Error: {exc}")

    async def _cmd_remove_plate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: `/remove_plate ABC123`", parse_mode="Markdown"
            )
            return
        plate = args[0].upper().strip()
        try:
            self._db.remove_vehicle(plate)
            await update.message.reply_text(f"✅ `{plate}` removed from whitelist.", parse_mode="Markdown")
            self._log_command(update.effective_chat.id, "remove_plate", plate, "removed")
        except Exception as exc:
            await update.message.reply_text(f"❌ Error: {exc}")

    async def _cmd_list_plates(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        vehicles = self._db.get_all_vehicles()
        if not vehicles:
            await update.message.reply_text("📭 Whitelist is empty.")
            return
        lines = ["📄 *Approved Plates*\n━━━━━━━━━━━━━━━━━━"]
        for v in vehicles:
            owner = v.get("owner_name") or "—"
            lines.append(f"• `{v['plate_text']}` ({owner})")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        self._log_command(update.effective_chat.id, "list_plates", "", f"{len(vehicles)} plates")

    # ── Audit logging ───────────────────────────────────────────
    def _log_command(self, chat_id: int, command: str, args: str, result: str) -> None:
        try:
            self._db.log_telegram_command(chat_id, command, args, result)
        except Exception as exc:
            logger.warning("Failed to log Telegram command: %s", exc)

    # ── Polling thread ──────────────────────────────────────────
    def start(self) -> None:
        """Start polling for commands in a daemon thread."""
        if not self._enabled:
            return

        self._thread = threading.Thread(
            target=self._run_polling,
            name="telegram-cmd-handler",
            daemon=True,
        )
        self._thread.start()
        logger.info("Telegram command handler thread started.")

    def stop(self) -> None:
        """Signal the polling loop to stop."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("Telegram command handler stopped.")

    def _run_polling(self) -> None:
        """Thread entry point — owns its own asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_app())
        except Exception as exc:
            logger.error("Telegram polling loop error: %s", exc)
        finally:
            self._loop.close()

    async def _start_app(self) -> None:
        """Build and start the Application, then poll until stopped."""
        self._app = Application.builder().token(self._token).build()

        # Register all command handlers (wrapped with auth guard)
        for cmd_name, handler_fn in [
            ("start",         self._cmd_start),
            ("status",        self._cmd_status),
            ("last_event",    self._cmd_last_event),
            ("snapshot",      self._cmd_snapshot),
            ("open_gate",     self._cmd_open_gate),
            ("close_gate",    self._cmd_close_gate),
            ("add_plate",     self._cmd_add_plate),
            ("remove_plate",  self._cmd_remove_plate),
            ("list_plates",   self._cmd_list_plates),
        ]:
            self._app.add_handler(
                CommandHandler(cmd_name, self._auth_guard(handler_fn))
            )

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started.")

        # Block until the loop is stopped externally
        while self._loop.is_running():
            await asyncio.sleep(1)

        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
