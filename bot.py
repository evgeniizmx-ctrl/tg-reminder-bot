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

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s, flags=re.UNICODE).strip()

def clean_description(desc: str) -> str:
    d = desc.strip()
    d = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^(о|про|насч[её]т)\s+", "", d, flags=re.IGNORECASE)
    return d or "Напоминание"

# ---------- РОБАСТНЫЙ ПАРСЕР «ЧЕРЕЗ … / СПУСТЯ …» ----------
# поддерживаем: «через минуту/час/день», «спустя минуту», «через 3 минуты», «через полчаса»
REL_NUM_PATTERNS = [
    (r"(через|спустя)\s+(\d+)\s*(секунд(?:у|ы)?|сек\.?)\b", "seconds"),
    (r"(через|спустя)\s+(\d+)\s*(минут(?:у|ы)?|мин\.?)\b", "minutes"),
    (r"(через|спустя)\s+(\d+)\s*(час(?:а|ов)?|ч\.?)\b",     "hours"),
    (r"(через|спустя)\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b","days"),
]
REL_SINGULAR_PATTERNS = [  # без числа = 1
    (r"(через|спустя)\s+секунд(?:у)\b", "seconds", 1),
    (r"(через|спустя)\s+минут(?:у|ку)\b", "minutes", 1),
    (r"(через|спустя)\s+час\b", "hours", 1),
    (r"(через|спустя)\s+день\b", "days", 1),
]
REL_HALF_HOUR_RX = re.compile(r"(через)\s+пол\s*часа\b", re.IGNORECASE | re.UNICODE)

REL_NUM_REGEXES = [re.compile(p, re.IGNORECASE | re.UNICODE | re.DOTALL) for p, _ in REL_NUM_PATTERNS]
REL_SING_REGEXES = [(re.compile(p, re.IGNORECASE | re.UNICODE | re.DOTALL), kind, val)
                    for p, kind, val in REL_SINGULAR_PATTERNS]

def parse_relative_phrase(raw_text: str):
    """
    Возвращает (dt, remainder) если нашёл относительное выражение.
    """
    s = normalize_spaces(raw_text)
    now = datetime.now(tz).replace(second=0, microsecond=0)

    # полчаса
    m = REL_HALF_HOUR_RX.search(s)
    if m:
        dt = now + timedelta(minutes=30)
        remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        print(f"[REL] 'полчаса' → {dt}")
        return dt, remainder

    # формы без числа (секунду/минуту/час/день)
    for rx, kind, val in REL_SING_REGEXES:
        m = rx.search(s)
        if m:
            if kind == "seconds": dt = now + timedelta(seconds=val)
            elif kind == "minutes": dt = now + timedelta(minutes=val)
            elif kind == "hours": dt = now + timedelta(hours=val)
            elif kind == "days": dt = now + timedelta(days=val)
            else: continue
            remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")
            print(f"[REL] {kind}=1 → {dt}")
            return dt, remainder

    # формы с числом
    for rx, (_, kind) in zip(REL_NUM_REGEXES, REL_NUM_PATTERNS):
        m = rx.search(s)
        if not m: 
            continue
        amount = int(m.group(2))
        if kind == "seconds": dt = now + timedelta(seconds=amount)
        elif kind == "minutes": dt = now + timedelta(minutes=amount)
        elif kind == "hours":   dt = now + timedelta(hours=amount)
        elif kind == "days":    dt = now + timedelta(days=amount)
        else: continue
        remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        print(f"[REL] {kind}={amount} → {dt}")
        return dt, remainder

    return None

# ---------- «В ЭТО ЖЕ ВРЕМЯ» ----------
SAME_TIME_RX = re.compile(r"\bв это же время\b", re.IGNORECASE | re.UNICODE)
TOMORROW_RX = re.compile(r"\bзавтра\b", re.IGNORECASE | re.UNICODE)
AFTER_TOMORROW_RX = re.compile(r"\bпослезавтра\b", re.IGNORECASE | re.UNICODE)
IN_N_DAYS_RX = re.compile(r"(через|спустя)\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b", re.IGNORECASE | re.UNICODE)

def parse_same_time_phrase(raw_text: str):
    s = normalize_spaces(raw_text)
    if not SAME_TIME_RX.search(s):
        return None
    now = datetime.now(tz).replace(second=0, microsecond=0)
    days = None
    if AFTER_TOMORROW_RX.search(s): days = 2
    elif TOMORROW_RX.search(s):     days = 1
    else:
        m = IN_N_DAYS_RX.search(s)
        if m:
            try: days = int(m.group(2))
            except: days = None
    if days is None: 
        return None
    target = (now + timedelta(days=days)).replace(hour=now.hour, minute=now.minute)
    remainder = s
    remainder = SAME_TIME_RX.sub("", remainder)
    remainder = TOMORROW_RX.sub("", remainder)
    remainder = AFTER_TOMORROW_RX.sub("", remainder)
    remainder = IN_N_DAYS_RX.sub("", remainder)
    remainder = remainder.strip(" ,.-")
    print(f"[SAME] +{days}d → {target}")
    return target, remainder

# ===================== OpenAI (GPT/Whisper) =====================
OPENAI_BASE = "https://api.openai.com/v1"

async def gpt_parse(text: str) -> dict:
    system = (
        "Ты — ассистент для напоминаний на русском. "
        "Верни СТРОГО JSON с ключами: description, event_time, remind_time, repeat(daily|weekly|none), "
        "needs_clarification, clarification_question. "
        "Если указано 'напомни за X', вычисли remind_time относительно event_time. "
        "Даты/время возвращай в формате 'YYYY-MM-DD HH:MM' (24h)."
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
        return json.loads(answer)
    except json.JSONDecodeError:
        return {
            "description": text,
            "event_time": "",
            "remind_time": "",
            "repeat": "none",
            "needs_clarification": True,
            "clarification_question": "Уточните дату и время напоминания (например, 25.08 14:25)."
        }

async def openai_whisper_bytes(ogg_bytes: bytes) -> str:
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    files = {"file": ("voice.ogg", ogg_bytes, "audio/ogg"),
             "model": (None, "whisper-1"),
             "language": (None, "ru")}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{OPENAI_BASE}/audio/transcriptions", headers=headers, files=files)
        r.raise_for_status()
        return r.json().get("text", "").strip()

# ===================== OCR.Space =====================
async def ocr_space_image(bytes_png: bytes) -> str:
    url = "https://api.ocr.space/parse/image"
    data = {"apikey": OCR_SPACE_API_KEY, "language": "rus", "OCREngine": 2}
    files = {"file": ("image.png", bytes_png, "image/png")}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(url, data=data, files=files)
        r.raise_for_status()
        js = r.json()
    try:
        return js["ParsedResults"][0]["ParsedText"].strip()
    except Exception:
        return ""

# ===================== ХЕНДЛЕРЫ =====================
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я бот-напоминалка.\n"
        "• Пиши: «Запись к стоматологу сегодня 14:25», «напомни через 3 минуты помыться», "
        "или «завтра в это же время позвонить»\n"
        "• Пришли голосовое/скрин — я распознаю.\n"
        "• /ping — проверка, жив ли бот.\n"
        "• /list — список напоминаний (в текущей сессии)."
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
        lines.append(
            f"• {r['text']} — {r['remind_dt'].strftime('%d.%m %H:%M')} ({TZ}) "
            + (f"[{r['repeat']}]" if r['repeat']!='none' else "")
        )
    await message.answer("\n".join(lines))

@dp.message(F.text)
async def on_any_text(message: Message):
    uid = message.from_user.id
    raw_text = message.text or ""
    text = normalize_spaces(raw_text)

    # 1) если ждём уточнение времени
    if uid in PENDING:
        # пробуем распознать «через …» или «в это же время»
        rel = parse_relative_phrase(text)
        same = None if rel else parse_same_time_phrase(text)

        # если в ответе есть и время, и новый «остаток» текста — считаем это НОВОЙ задачей
        if rel and rel[1]:
            dt, remainder = rel
            desc = clean_description(remainder)
            PENDING.pop(uid, None)
            reminder = {"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"}
            REMINDERS.append(reminder)
            schedule_one(reminder)
            await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
            return
        if same and same[1]:
            dt, remainder = same
            desc = clean_description(remainder)
            PENDING.pop(uid, None)
            reminder = {"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"}
            REMINDERS.append(reminder)
            schedule_one(reminder)
            await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
            return

        # иначе — используем описание из черновика
        dt = None
        if rel: dt = rel[0]
        elif same: dt = same[0]
        else: dt = as_local_iso(text)

        if not dt:
            await message.reply("Не понял время. Пример: «25.08 14:25», «через минуту» или «завтра в это же время».")
            return

        draft = PENDING.pop(uid)
        desc = clean_description(draft.get("description","Напоминание"))
        reminder = {"user_id": uid, "text": desc, "remind_dt": dt, "repeat": draft.get("repeat","none")}
        REMINDERS.append(reminder)
        schedule_one(reminder)
        await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
        return

    # 2) новая фраза — сначала «через …»
    rel = parse_relative_phrase(text)
    if rel:
        dt, remainder = rel
        desc = clean_description(remainder or text)
        reminder = {"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"}
        REMINDERS.append(reminder)
        schedule_one(reminder)
        await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
        return

    # 2b) «в это же время»
    same = parse_same_time_phrase(text)
    if same:
        dt, remainder = same
        desc = clean_description(remainder or text)
        reminder = {"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"}
        REMINDERS.append(reminder)
        schedule_one(reminder)
        await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
        return

    # 3) иначе — GPT
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    plan = await gpt_parse(text)

    desc = clean_description(plan.get("description") or "Напоминание")
    repeat = (plan.get("repeat") or "none").lower()
    remind_iso = plan.get("remind_time") or plan.get("event_time")
    remind_dt = as_local_iso(remind_iso)

    if plan.get("needs_clarification") or not remind_dt:
        question = plan.get("clarification_question") or "Уточните дату и время (например, 25.08 14:25, «через минуту» или «завтра в это же время»):"
        PENDING[uid] = {"description": desc, "repeat": "none"}
        await message.reply(question)
        return

    reminder = {"user_id": uid, "text": desc, "remind_dt": remind_dt,
                "repeat": "none" if repeat not in ("daily","weekly") else repeat}
    REMINDERS.append(reminder)
    schedule_one(reminder)
    await message.reply(f"Готово. Напомню: «{desc}» в {remind_dt.strftime('%d.%m %H:%M')} ({TZ})")

# ---- войсы ----
@dp.message(F.voice)
async def on_voice(message: Message):
    await bot.send_chat_action(message.chat.id, ChatAction.RECORD_VOICE)
    file = await bot.get_file(message.voice.file_id)
    buf = await bot.download_file(file.file_path)
    buf.seek(0)
    text = await openai_whisper_bytes(buf.read())
    if not text:
        await message.reply("Не удалось распознать голос. Попробуйте ещё раз.")
        return
    await on_any_text(Message.model_construct(**{**message.model_dump(), "text": text}))

# ---- фото/док с изображением ----
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
    await on_any_text(Message.model_construct(**{**message.model_dump(), "text": text}))

# ===================== ЗАПУСК =====================
async def main():
    print("STEP: starting scheduler...")
    scheduler.start()
    print("STEP: scheduler started")
    print("STEP: start polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        print("STEP: asyncio.run(main())")
        asyncio.run(main())
    except Exception as e:
        import traceback, time
        print("FATAL:", e)
        traceback.print_exc()
        time.sleep(120)
        raise
