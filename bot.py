import os
import io
import json
import re
import asyncio
from datetime import datetime, timedelta
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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

# ===================== ИНИЦИАЛИЗАЦИЯ =====================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)

PENDING = {}
REMINDERS = []

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

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s, flags=re.UNICODE).strip()

def clean_description(desc: str) -> str:
    d = desc.strip()
    d = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^(о|про|насч[её]т)\s+", "", d, flags=re.IGNORECASE)
    return d or "Напоминание"

# ===================== ПАРСЕРЫ =====================
# --- "через ..." ---
REL_NUM_PATTERNS = [
    (r"(через|спустя)\s+(\d+)\s*(секунд(?:у|ы)?|сек\.?)\b", "seconds"),
    (r"(через|спустя)\s+(\d+)\s*(минут(?:у|ы)?|мин\.?)\b", "minutes"),
    (r"(через|спустя)\s+(\d+)\s*(час(?:а|ов)?|ч\.?)\b",     "hours"),
    (r"(через|спустя)\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b","days"),
]
REL_NUM_REGEXES = [re.compile(p, re.IGNORECASE | re.UNICODE | re.DOTALL) for p, _ in REL_NUM_PATTERNS]

def parse_relative_phrase(raw_text: str):
    s = normalize_spaces(raw_text)
    now = datetime.now(tz).replace(second=0, microsecond=0)
    for rx, (_, kind) in zip(REL_NUM_REGEXES, REL_NUM_PATTERNS):
        m = rx.search(s)
        if not m: continue
        amount = int(m.group(2))
        if kind == "seconds": dt = now + timedelta(seconds=amount)
        elif kind == "minutes": dt = now + timedelta(minutes=amount)
        elif kind == "hours":   dt = now + timedelta(hours=amount)
        elif kind == "days":    dt = now + timedelta(days=amount)
        remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return dt, remainder
    return None

# --- "сегодня/завтра/послезавтра в ..." ---
DAYTIME_RX = re.compile(
    r"\b(сегодня|завтра|послезавтра)\b.*?\bв\s*(\d{1,2})(?::(\d{2}))?(?:\s*(утра|дня|вечера|ночи))?\b",
    re.IGNORECASE | re.UNICODE | re.DOTALL
)

# --- "просто в HH[:MM]" (без дня) ---
ONLYTIME_RX = re.compile(
    r"\bв\s*(\d{1,2})(?::(\d{2}))?\s*(час(?:ов|а)?|ч\.)?\b",
    re.IGNORECASE | re.UNICODE
)

def parse_daytime_or_onlytime(raw_text: str):
    s = normalize_spaces(raw_text)
    now = datetime.now(tz).replace(second=0, microsecond=0)

    # сначала ищем "сегодня/завтра/послезавтра"
    m = DAYTIME_RX.search(s)
    if m:
        day_word = m.group(1).lower()
        hour_raw = int(m.group(2))
        minute = int(m.group(3) or 0)
        mer = (m.group(4) or "").lower()
        if day_word == "сегодня": base = now
        elif day_word == "завтра": base = now + timedelta(days=1)
        else: base = now + timedelta(days=2)
        # если указан "утра/вечера" → одно значение
        if mer in ("утра","дня","вечера","ночи"):
            h = hour_raw
            if mer in ("дня","вечера") and h < 12: h += 12
            if mer == "ночи" and h == 12: h = 0
            return ("ok", base.replace(hour=h, minute=minute), "")
        # иначе двусмысленно → вернём варианты (утро/вечер)
        dt1 = base.replace(hour=hour_raw, minute=minute)
        dt2 = base.replace(hour=(hour_raw+12)%24, minute=minute)
        return ("amb", None, [dt1, dt2])

    # ищем "в 17 часов" без дня
    m2 = ONLYTIME_RX.search(s)
    if m2:
        hour_raw = int(m2.group(1))
        minute = int(m2.group(2) or 0)
        target = now.replace(hour=hour_raw, minute=minute, second=0, microsecond=0)
        if target <= now:  # если уже прошло → завтра
            target = target + timedelta(days=1)
        return ("ok", target, "")

    return None

# ===================== GPT =====================
OPENAI_BASE = "https://api.openai.com/v1"
async def gpt_parse(text: str) -> dict:
    return {"description": text, "event_time":"", "remind_time":"", "repeat":"none", "needs_clarification":True,
            "clarification_question":"Уточните дату и время (например, 25.08 14:25)."}

# ===================== ХЕНДЛЕРЫ =====================
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я бот-напоминалка.\n"
        "Теперь можно писать и просто «напомни в 17 часов» — я поставлю на ближайшие 17:00."
    )

@dp.message(F.text)
async def on_text(message: Message):
    uid = message.from_user.id
    text = normalize_spaces(message.text or "")

    # 1) "в HH"
    pack = parse_daytime_or_onlytime(text)
    if pack:
        tag = pack[0]
        if tag=="ok":
            _, dt, _ = pack
            desc = clean_description(text)
            r = {"user_id":uid,"text":desc,"remind_dt":dt,"repeat":"none"}
            REMINDERS.append(r); schedule_one(r)
            await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
            return
        elif tag=="amb":
            _,_,variants = pack
            desc = clean_description(text)
            PENDING[uid]={"description":desc,"variants":variants,"repeat":"none"}
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=dt.strftime('%d.%m %H:%M'),callback_data=f"time|{dt.isoformat()}")]
                for dt in variants])
            await message.reply(f"Уточните время для «{desc}»", reply_markup=kb)
            return

    # 2) "через ..."
    rel = parse_relative_phrase(text)
    if rel:
        dt, remainder = rel
        desc = clean_description(remainder or text)
        r={"user_id":uid,"text":desc,"remind_dt":dt,"repeat":"none"}
        REMINDERS.append(r); schedule_one(r)
        await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
        return

    await message.reply("Не понял время. Попробуйте: «в 17 часов» или «завтра в 9».")

@dp.callback_query(F.data.startswith("time|"))
async def on_choice(cb: CallbackQuery):
    iso = cb.data.split("|",1)[1]
    dt=datetime.fromisoformat(iso).astimezone(tz)
    desc=PENDING[cb.from_user.id]["description"]
    r={"user_id":cb.from_user.id,"text":desc,"remind_dt":dt,"repeat":"none"}
    REMINDERS.append(r); schedule_one(r)
    await cb.message.edit_text(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
    PENDING.pop(cb.from_user.id,None)
    await cb.answer("Установлено ✅")

# ===================== ЗАПУСК =====================
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())
