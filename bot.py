import os
import io
import json
import re
import asyncio
from datetime import datetime, timedelta
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ChatAction

import httpx
from dateutil import parser as dateparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ===================== ОКРУЖЕНИЕ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY")

TZ = os.getenv("APP_TZ", "Europe/Moscow")
tz = pytz.timezone(TZ)

print("ENV CHECK:",
      "BOT_TOKEN set:", bool(BOT_TOKEN),
      "OPENAI_API_KEY set:", bool(OPENAI_API_KEY),
      "OCR_SPACE_API_KEY set:", bool(OCR_SPACE_API_KEY))

# ===================== ИНИЦИАЛИЗАЦИЯ =====================
print("STEP: creating Bot/Dispatcher...")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
print("STEP: Bot/Dispatcher OK")

scheduler = AsyncIOScheduler(timezone=TZ)

# Память (MVP)
PENDING = {}    # user_id -> {"description": str, "repeat": "none|daily|weekly"}
REMINDERS = []  # {user_id, text, remind_dt, repeat}

# ===================== УТИЛИТЫ =====================
async def send_reminder(user_id: int, text: str):
    try:
        await bot.send_message(user_id, f"🔔 Напоминание: {text}")
    except Exception as e:
        print("Send reminder error:", e)

def schedule_one(reminder: dict):
    run_dt = reminder["remind_dt"]
    scheduler.add_job(send_reminder, "date", run_date=run_dt,
                      args=[reminder["user_id"], reminder["text"]])

def as_local_iso(dt_like: str | None) -> datetime | None:
    """Парсим дату/время вида '25.08 14:25', 'сегодня 18:30', '2025-08-25 15:00' и т.п."""
    if not dt_like:
        return None
    try:
        dt = dateparser.parse(dt_like)
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        else:
            dt = dt.astimezone(tz)
        return dt.replace(second=0, microsecond=0)
    except Exception:
        return None

# ---------- НОРМАЛИЗАЦИЯ ТЕКСТА ----------
def normalize_spaces(s: str) -> str:
    # заменяем переносы/табы/множественные пробелы на одиночный пробел
    return re.sub(r"\s+", " ", s, flags=re.UNICODE).strip()

# ---------- РОБАСТНЫЙ ПАРСЕР «ЧЕРЕЗ N ... / СПУСТЯ N ...» ----------
RELATIVE_PATTERNS = [
    (r"(через|спустя)\s+(\d+)\s*(секунд(?:у|ы)?|сек\.?)\b", "seconds"),
    (r"(через|спустя)\s+(\d+)\s*(минут(?:у|ы)?|мин\.?)\b", "minutes"),
    (r"(через)\s+пол\s*часа\b", "half_hour"),
    (r"(через|спустя)\s+(\d+)\s*(час(?:а|ов)?|ч\.?)\b"*
