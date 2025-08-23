import os
import re
import asyncio
from datetime import datetime, timedelta, date
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== ОКРУЖЕНИЕ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_TZ = os.getenv("APP_TZ", "Europe/Moscow")
tz = pytz.timezone(APP_TZ)

# ================== ИНИЦ ==================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=APP_TZ)

# PENDING[user_id] = {
#   "description": str,
#   "repeat": "none",
#   "variants": [datetime],        # двусмысленности
#   "base_date": date              # ждём только время
# }
PENDING: dict[int, dict] = {}
REMINDERS: list[dict] = []

# ================== УТИЛИТЫ ==================
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "", flags=re.UNICODE).strip()

def clean_desc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", s, flags=re.I)
    s = re.sub(r"^(о|про|насч[её]т)\s+", "", s, flags=re.I)
    return s.strip() or "Напоминание"

async def send_reminder(uid: int, text: str):
    try:
        await bot.send_message(uid, f"🔔 Напоминание: {text}")
    except Exception as e:
        print("send_reminder error:", e)

def plan(reminder: dict):
    scheduler.add_job(send_reminder, "date",
                      run_date=reminder["remind_dt"],
                      args=[reminder["user_id"], reminder["text"]])

def mk_dt(d: date, h: int, m: int) -> datetime:
    return tz.localize(datetime(d.year, d.month, d.day, h % 24, m % 60, 0, 0))

def soonest(dts: list[datetime]) -> list[datetime]:
    return sorted(dts, key=lambda x: x)

# Человекочитаемая подпись кнопки
def human_label(dt: datetime) -> str:
    now = datetime.now(tz)
    if dt.date() == now.date():
        dword = "Сегодня"
    elif dt.date() == (now + timedelta(days=1)).date():
        dword = "Завтра"
    else:
        dword = dt.strftime("%d.%m")

    h = dt.hour
    m = dt.minute
    if 0 <= h <= 4:
        mer = "ночи"
    elif 5 <= h <= 11:
        mer = "утра"
    elif 12 <= h <= 16:
        mer = "дня"
    else:
        mer = "вечера"

    h12 = h % 12
    if h12 == 0: h12 = 12
    t = f"{h12}:{m:02d}" if m else f"{h12}"
    return f"{dword} в {t} {mer}"

def kb_vari_

