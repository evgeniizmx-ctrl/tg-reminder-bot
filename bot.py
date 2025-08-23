# bot.py
import os
import re
import json
import shutil
import tempfile
import asyncio
from datetime import datetime, timedelta
import pytz

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ========= OpenAI =========
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")   # для парсинга текста
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")   # STT

# ========= Telegram / TZ =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_TZ_NAME = os.getenv("APP_TZ", "Europe/Moscow")       # фолбэк до выбора пользователем
BASE_TZ = pytz.timezone(BASE_TZ_NAME)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

router = Router()
dp.include_router(router)

# планировщик — будем передавать aware-время
scheduler = AsyncIOScheduler(timezone=BASE_TZ)

# ========= In-memory (MVP) =========
REMINDERS: list[dict] = []             # {"user_id","text","remind_dt","repeat"}
PENDING: dict[int, dict] = {}          # {"description","candidates":[iso,...]}
USER_TZS: dict[int, str] = {}          # user_id -> IANA или "UTC+<minutes>"

# ========= FFmpeg path resolve =========
def resolve_ffmpeg_path() -> str:
    env = os.getenv("FFMPEG_PATH")
    if env and os.path.exists(env):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    # brew default on Apple Silicon
    return "/opt/homebrew/bin/ffmpeg"

FFMPEG_PATH = resolve_ffmpeg_path()
print(f"[init] Using ffmpeg at: {FFMPEG_PATH}")

# ========= TZ helpers =========
RU_TZ_CHOICES = [
    ("Калининград (+2)",  "Europe/Kaliningrad",  2),
    ("Москва (+3)",       "Europe/Moscow",       3),
    ("Самара (+4)",       "Europe/Samara",       4),
    ("Екатеринбург (+5)", "Asia/Yekaterinburg",  5),
    ("Омск (+6)",         "Asia/Omsk",           6),
    ("Новосибирск (+7)",  "Asia/Novosibirsk",    7),
    ("Иркутск (+8)",      "Asia/Irkutsk",        8),
    ("Якутск (+9)",       "Asia/Yakutsk",        9),
    ("Хабаровск (+10)",   "Asia/Vladivostok",   10),
]

def tz_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"settz|{iana}")]
            for (label, iana, _off) in RU_TZ_CHOICES]
    rows.append([InlineKeyboardButton(text="Ввести смещение (+/-часы)", callback_data="settz|ASK_OFFSET")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

OFFSET_FLEX_RX = re.compile(r"^[+-]?\s*(\d{1,2})(?::\s*([0-5]\d))?$")

def parse_user_tz_string(s: str):
    s = (s or "").strip()
    # IANA?
    try:
        return pytz.timezone(s)
    except Exception:
        pass
    # +HH[:MM] (знак по умолчанию '+')
    m = OFFSET_FLEX_RX.match(s)
    if not m:
        return None
    sign = -1 if s.strip().startswith("-") else +1
    hh = int(m.group(1)); mm = int(m.group(2) or 0)
    if hh > 23:
        return None
    return pytz.FixedOffset(sign * (hh * 60 + mm))

def get_user_tz(uid: int):
    name = USER_TZS.get(uid)
    if not name:
        return BASE_TZ
    if name.startswith("UTC+"):
        return pytz.FixedOffset(int(name[4:]))
    return pytz.timezone(name)

def store_user_tz(uid: int, tzobj):
    zone = getattr(tzobj, "zone", None)
    if isinstance(zone, str):
        USER_TZS[uid] = zone
    else:
        ofs_min = int(tzobj.utcoffset(datetime.utcnow()).total_seconds() // 60)
        USER_TZS[uid] = f"UTC+{ofs_min}"

def need_tz(uid: int) -> bool:
    return uid not in USER_TZS

async def ask_tz(m: Message):
    await m.answer(
        "Для начала укажи свой часовой пояс.\n"
        "Выбери из списка или введи либо смещение формата +03:00.",
        reply_markup=tz_kb()
    )

# ========= Common helpers =========
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "", flags=re.UNICODE).strip()

def clean_desc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", s, flags=re.I)
    s = re.sub(r"^(о|про|насч[её]т)\s+", "", s, flags=re.I)
    return s.strip() or "Напоминание"

def fmt_dt_local(dt: datetime) -> str:
    re
