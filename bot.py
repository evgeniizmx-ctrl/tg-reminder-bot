import os
import io
import json
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

# --------- КЛЮЧИ/НАСТРОЙКИ ---------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY")

TZ = os.getenv("APP_TZ", "Europe/Moscow")
tz = pytz.timezone(TZ)

# --------- ИНИЦИАЛИЗАЦИЯ ---------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)

# Храним черновики и напоминания в памяти (MVP)
PENDING = {}   # user_id -> {"description": str, "repeat": "none|daily|weekly"}
REMINDERS = [] # список словарей: {user_id, text, remind_dt, repeat}

# --------- УТИЛИТЫ ---------
async def send_reminder(user_id: int, text: str):
    try:
        await bot.send_message(user_id, f"🔔 Напоминание: {text}")
    except Exception as e:
        print("Send reminder error:", e)

def schedule_one(reminder: dict):
    run_dt = reminder["remind_dt"]
    scheduler.add_job(send_reminder, "date", run_date=run_dt, args=[reminder["user_id"], reminder["text"]])

def as_local_iso(dt_like: str | None) -> datetime | None:
    """Парсим «сегодня 14:25» / «25.08 15:00» / ISO и приводим к таймзоне TZ."""
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

# --------- OpenAI GPT / Whisper ---------
OPENAI_BASE = "https://api.openai.com/v1"

async def gpt_parse(text: str) -> dict:
    """Просим GPT вернуть JSON-структуру задачи."""
    system = (
        "Ты — ассистент для напоминаний на русском. "
        "Разбирай текст пользователя и возвращай СТРОГО JSON со структурой: "
        "{description, event_time, remind_time, repeat(daily|weekly|none), needs_clarification, clarification_question}. "
        "Если указано 'напомни за X', вычисли remind_time относительно event_time. "
        "Даты/время возвращай в формате 'YYYY-MM-DD HH:MM' 24h."
    )
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text}
        ],
        "temperature": 0
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{OPENAI_BASE}/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"]
    try:
        data = json.loads(answer)
    except json.JSONDecodeError:
        data = {
            "description": text,
            "event_time": "",
            "remind_time": "",
            "repeat": "none",
            "needs_clarification": True,
            "clarification_question": "Уточните дату и время напоминания (например, 25.08 14:25)."
        }
    return data

async def openai_whisper_bytes(ogg_bytes: bytes) -> str:
    """Распознаём голос через OpenAI Whisper API."""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    files = {
        "file": ("voice.ogg", ogg_bytes, "audio/ogg"),
        "model": (None, "whisper-1"),
        "language": (None, "ru")
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{OPENAI_BASE}/audio/transcriptions", headers=headers, files=files)
        r.raise_for_status()
        return r.json().get("text", "").strip()

# --------- OCR.Space (скрины) ---------
async def ocr_space_image(bytes_png: bytes) -> str:
    url = "https://api.ocr.space/parse/image"
    data = {"apikey": OCR_SPACE_API_KEY, "language": "rus", "OCREngine": 2}
    files = {"file": ("image.png", bytes_png, "image/png")}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(url, data=data, files=files)
        r.raise_for_status()
        js = r.json()
    try:
        parsed = js["ParsedResults"][0]["ParsedText"]
    except Exception:
        parsed = ""
    return parsed.strip()

# --------- КОМАНДЫ ---------
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я бот-напоминалка.\n"
        "• Пиши: «Запись к стоматологу сегодня 14:25»\n"
        "• Или пришли голосовое/скрин — я распознаю.\n"
        "• Команда /ping — проверка, жив ли бот.\n"
        "• Команда /list — показать созданные напоминания (сессии)."
    )

@dp.message(Command("ping"))
async def ping(message: Message):
    await message.answer("pong ✅")

@dp.message(Command("list"))
async def list_cmd(message: Message):
    uid = message.from_user.id
    items = [r for r in REMINDERS if r["user_id"] == uid]
    if not items:
        await message.answer("Пока нет напоминаний (в этой сессии).")
        return
    lines = []
    for r in items:
        lines.append(f"• {r['text']} — {r['remind_dt'].strftime('%d.%m %H:%M')} ({TZ}) "
                     + (f"[{r['repeat']}]" if r['repeat']!='none' else ""))
    await message.answer("\n".join(lines))

# --------- ЕДИНЫЙ обработчик текста (новое/уточнение) ---------
@dp.message(F.text)
async def on_any_text(message: Message):
    uid = message.from_user.id
    text = message.text.strip()

    # Если ждём уточнение времени — обрабатываем прямо здесь
    if uid in PENDING:
        dt = as_local_iso(text)
        if not dt:
            await message.reply("Не понял время. Пример: 25.08 14:25")
            return
        draft = PENDING.pop(uid)
        reminder = {"user_id": uid, "text": draft["description"], "remind_dt": dt, "repeat": draft.get("repeat","none")}
        REMINDERS.append(reminder)
        schedule_one(reminder)
        await message.reply(f"Принял. Напомню: «{reminder['text']}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
        return

    # Иначе — обычное новое сообщение
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    plan = await gpt_parse(text)

    desc = (plan.get("description") or "Напоминание").strip()
    repeat = (plan.get("repeat") or "none").lower()
    remind_iso = plan.get("remind_time") or plan.get("event_time")
    remind_dt = as_local_iso(remind_iso)

    if plan.get("needs_clarification") or not remind_dt:
        question = plan.get("clarification_question") or "Уточните дату и время напоминания (например, 25.08 14:25):"
        PENDING[uid] = {"description": desc, "repeat": "none"}
        await message.reply(question)
        return

    reminder = {"user_id": uid, "text": desc, "remind_dt": remind_dt,
                "repeat": "none" if repeat not in ("daily","weekly") else repeat}
    REMINDERS.append(reminder)
    schedule_one(reminder)
    await message.reply(f"Готово. Напомню: «{desc}» в {remind_dt.strftime('%d.%m %H:%M')} ({TZ})")

# --------- ВОЙСЫ ---------
@dp.message(F.voice)
async def on_voice(message: Message):
    await bot.send_chat_action(message.chat.id, ChatAction.RECORD_VOICE)
    file = await bot.get_file(message.voice.file_id)
    buf = await bot.download_file(file.file_path)  # BytesIO
    buf.seek(0)
    text = await openai_whisper_bytes(buf.read())
    if not text:
        await message.reply("Не удалось распознать голос. Попробуйте ещё раз.")
        return
    # Переиспользуем общий путь
    await on_any_text(Message.model_construct(**{**message.model_dump(), "text": text}))

# --------- ФОТО / ДОК с изображением ---------
@dp.message(F.photo | F.document)
async def on_image(message: Message):
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)

    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and str(message.document.mime_type).startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.reply("Пришлите изображение (фото/скрин) с текстом.")
        return

    file = await bot.get_file(file_id)
    buf = await bot.download_file(file.file_path)
    buf.seek(0)
    text = await ocr_sp_
