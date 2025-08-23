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

# ==== Ключи и базовые настройки ====
BOT_TOKEN = os.getenv("BOT_TOKEN")            # из Railway Variables
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # из Railway Variables
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY")  # из Railway Variables

# Часовой пояс по умолчанию (ставим Europe/Moscow; при желании замени)
TZ = os.getenv("APP_TZ", "Europe/Moscow")
tz = pytz.timezone(TZ)

# ==== Инициализация ====
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)

# Память-в-рамке для "черновиков" напоминаний (простенько, без БД)
PENDING = {}   # user_id -> dict
REMINDERS = [] # список словарей: {user_id, text, remind_dt, repeat}

# ==== Утилиты планировщика ====
async def send_reminder(user_id: int, text: str):
    try:
        await bot.send_message(user_id, f"🔔 Напоминание: {text}")
    except Exception as e:
        print("Send reminder error:", e)

def schedule_one(reminder: dict):
    run_dt = reminder["remind_dt"]
    scheduler.add_job(send_reminder, "date", run_date=run_dt, args=[reminder["user_id"], reminder["text"]])

def schedule_next_if_repeat(reminder: dict):
    if reminder.get("repeat") in ("daily", "weekly"):
        delta = timedelta(days=1) if reminder["repeat"] == "daily" else timedelta(weeks=1)
        next_dt = reminder["remind_dt"] + delta
        reminder["remind_dt"] = next_dt
        schedule_one(reminder)

# ==== OpenAI: GPT (анализ текста) + Whisper (распозн. речи) ====
# Используем официальный клиент openai (через httpx вручную для Audio — унифицировано)
OPENAI_BASE = "https://api.openai.com/v1"

async def gpt_parse(text: str) -> dict:
    """
    Просим GPT вернуть JSON со структурой:
    {
      "description": "строка",
      "event_time": "ISO или пусто",
      "remind_time": "ISO или пусто",
      "repeat": "daily|weekly|none",
      "needs_clarification": true/false,
      "clarification_question": "если нужно"
    }
    """
    system = (
        "Ты — ассистент для напоминаний на русском. "
        "Разбирай текст пользователя и возвращай строго JSON. "
        "Если дата/время неочевидны — отметь needs_clarification=true и сформулируй короткий вопрос. "
        "Если указано 'напомни за X', вычисли remind_time относительно event_time. "
        "Время и даты возвращай в ISO-формате (YYYY-MM-DD HH:MM, 24h). "
        "Ключи: description, event_time, remind_time, repeat(daily|weekly|none), needs_clarification, clarification_question."
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
            "clarification_question": "Уточните, когда именно напомнить (дата и время)?"
        }
    return data

async def openai_whisper_bytes(ogg_bytes: bytes) -> str:
    """Отправляем голос (ogg/opus) в Whisper API и получаем текст на русском."""
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

# ==== OCR.Space для скринов ====
async def ocr_space_image(bytes_png: bytes) -> str:
    """
    Отправляем картинку в OCR.Space.
    Советы: лучше скрины/фото текста. PDF/сложные таблицы не для MVP.
    """
    url = "https://api.ocr.space/parse/image"
    data = {
        "apikey": OCR_SPACE_API_KEY,
        "language": "rus",
        "OCREngine": 2
    }
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

# ==== Вспомогательная нормализация времени ====
def as_local_iso(dt_like: str) -> datetime | None:
    """Парсим человеко-понятную дату в datetime в нашем часовом поясе."""
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
        # округлим секунды
        return dt.replace(second=0, microsecond=0)
    except Exception:
        return None

# ==== Диалоги/хендлеры ====
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я бот-напоминалка.\n"
        "Отправь текст/голосовое/скрин с задачей (например: «Запись к врачу 25.08 в 15:00, напомни за день»)."
    )

# Текст
@dp.message(F.text)
async def on_text(message: Message):
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    plan = await gpt_parse(message.text)
    await handle_plan(message, plan)

# Войсы
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
    plan = await gpt_parse(text)
    await handle_plan(message, plan)

# Фото/док как картинка
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
    text = await ocr_space_image(buf.read())
    if not text:
        await message.reply("Не удалось прочитать текст на изображении.")
        return
    plan = await gpt_parse(text)
    await handle_plan(message, plan)

async def handle_plan(message: Message, plan: dict):
    # Разбор ответа GPT
    desc = plan.get("description", "").strip() or "Напоминание"
    repeat = (plan.get("repeat") or "none").lower()
    need = plan.get("needs_clarification", False)

    # Пробуем собрать remind_time
    remind_iso = plan.get("remind_time", "") or plan.get("event_time", "")
    remind_dt = as_local_iso(remind_iso)

    # Если GPT сказал, что надо уточнить — спросим
    if need or not remind_dt:
        question = plan.get("clarification_question") or "Уточните дату и время напоминания (например, 25.08 15:00):"
        # Сохраняем "черновик"
        PENDING[message.from_user.id] = {
            "description": desc,
            "repeat": "none"
        }
        await message.reply(question)
        return

    # Всё есть → сохраняем и планируем
    reminder = {
        "user_id": message.from_user.id,
        "text": desc,
        "remind_dt": remind_dt,
        "repeat": "none" if repeat not in ("daily", "weekly") else repeat
    }
    REMINDERS.append(reminder)
    schedule_one(reminder)
    await message.reply(f"Готово. Напомню: «{desc}» в {remind_dt.strftime('%d.%m %H:%M')} ({TZ})")

# Ответ на уточнение (если бот задал вопрос)
@dp.message(F.text & (F.from_user.id.in_(lambda uids: True)))
async def clarifying(message: Message):
    uid = message.from_user.id
    if uid not in PENDING:
        return  # нет черновика — пропускаем

    dt = as_local_iso(message.text)
    if not dt:
        await message.reply("Не понял время. Пример: 25.08 15:00")
        return

    draft = PENDING.pop(uid)
    reminder = {
        "user_id": uid,
        "text": draft["description"],
        "remind_dt": dt,
        "repeat": draft.get("repeat", "none")
    }
    REMINDERS.append(reminder)
    schedule_one(reminder)
    await message.reply(f"Принял. Напомню: «{reminder['text']}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")

async def main():
    scheduler.start()
    print("Bot started.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
