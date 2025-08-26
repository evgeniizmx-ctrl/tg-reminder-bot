# bot.py
import os
import re
import json
import sqlite3
import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import asyncio
import subprocess
import tempfile

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from dateutil import parser as dparser

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.ext import filters as F

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("planner-bot")

# ---------- ENV ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
PROMPTS_PATH = os.environ.get("PROMPTS_PATH", "prompts.yaml")
DB_PATH = os.environ.get("DB_PATH", "reminders.db")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not BOT_TOKEN:
    log.error("BOT_TOKEN не задан")
    sys.exit(1)
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY не задан — Whisper/LLM работать не будет")

# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db() as conn:
        conn.execute("""
            create table if not exists users (
                user_id integer primary key,
                tz text
            )
        """)
        conn.execute("""
            create table if not exists reminders (
                id integer primary key autoincrement,
                user_id integer not null,
                title text not null,
                body text,
                when_iso text,
                status text default 'scheduled',
                kind text default 'oneoff',
                recurrence_json text
            )
        """)
        conn.commit()

def db_get_user_tz(user_id: int):
    with db() as conn:
        row = conn.execute("select tz from users where user_id=?", (user_id,)).fetchone()
        return row["tz"] if row and row["tz"] else None

def db_set_user_tz(user_id: int, tz: str):
    with db() as conn:
        conn.execute(
            "insert into users(user_id, tz) values(?, ?) "
            "on conflict(user_id) do update set tz=excluded.tz",
            (user_id, tz)
        )
        conn.commit()

# ---------- TZ helpers ----------
def tzinfo_from_user(tz_str: str) -> timezone | ZoneInfo:
    tz_str = (tz_str or "+03:00").strip()
    if tz_str[0] in "+-":
        m = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?$", tz_str)
        if not m: raise ValueError("invalid offset")
        sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        delta = timedelta(hours=hh, minutes=mm)
        if sign == "-": delta = -delta
        return timezone(delta)
    return ZoneInfo(tz_str)

def now_in_user_tz(tz_str: str) -> datetime:
    return datetime.now(tzinfo_from_user(tz_str))

def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()

def parse_iso(s: str) -> datetime:
    return dparser.isoparse(s)

# ---------- UI ----------
MAIN_MENU_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📝 Список напоминаний"), KeyboardButton("⚙️ Настройки")]],
    resize_keyboard=True, one_time_keyboard=False
)

# ---------- OpenAI ----------
from openai import OpenAI
_client = None
def get_openai():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client

# ---------- Voice / Audio ----------
async def handle_any_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скачиваем voice/audio/video_note/document → ffmpeg → wav → Whisper"""
    try:
        file = None
        if update.message.voice:
            file = await update.message.voice.get_file()
        elif update.message.audio:
            file = await update.message.audio.get_file()
        elif update.message.video_note:
            file = await update.message.video_note.get_file()
        elif update.message.document:
            file = await update.message.document.get_file()

        if not file:
            return await update.message.reply_text("Не смог распознать файл")

        with tempfile.TemporaryDirectory() as td:
            in_path = os.path.join(td, "in.ogg")
            out_path = os.path.join(td, "out.wav")
            await file.download_to_drive(custom_path=in_path)

            cmd = ["ffmpeg", "-y", "-i", in_path, "-ac", "1", "-ar", "16000", out_path]
            proc = await asyncio.create_subprocess_exec(*cmd)
            await proc.wait()
            if not os.path.exists(out_path):
                return await update.message.reply_text("Ошибка при обработке файла")

            client = get_openai()
            with open(out_path, "rb") as f:
                tr = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text"
                )
                text = tr if isinstance(tr, str) else getattr(tr, "text", "")
        text = (text or "").strip()
        if not text:
            return await update.message.reply_text("Не удалось распознать аудио")
        update.message.text = text
        return await handle_text(update, context)
    except Exception as e:
        log.exception("audio error: %s", e)
        return await update.message.reply_text("Ошибка обработки аудио")

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен. Напиши напоминание!")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Здесь будет список напоминаний")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    await update.message.reply_text(f"Ты сказал: {text}")

# ---------- Startup ----------
async def on_startup(app: Application):
    global scheduler
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.start()
    log.info("APScheduler started")

# ---------- main ----------
def main():
    log.info("Starting PlannerBot...")
    db_init()
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(on_startup)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CallbackQueryHandler(cmd_list, pattern=r"^tz:"))

    # 🎙 Voice/Audio/VideoNote/Docs
    app.add_handler(
        MessageHandler(
            F.VOICE
            | F.AUDIO
            | F.VIDEO_NOTE
            | F.Document.MimeType("audio/ogg")
            | F.Document.MimeType("audio/mpeg")
            | F.Document.FileExtension("ogg")
            | F.Document.FileExtension("mp3")
            | F.Document.FileExtension("wav"),
            handle_any_audio
        )
    )

    # ✍️ Text
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
