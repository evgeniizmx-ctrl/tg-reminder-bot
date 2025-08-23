import os
import re
import asyncio
import tempfile
import shutil
from datetime import datetime, timedelta, date
import pytz

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ========= ENV / TZ =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_TZ = os.getenv("APP_TZ", "Europe/Moscow")
tz = pytz.timezone(APP_TZ)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=APP_TZ)

# ========= REMINDERS =========
REMINDERS: list[dict] = []
PENDING: dict[int, dict] = {}

# ========= HELPERS =========
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "", flags=re.UNICODE).strip()

def clean_desc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", s, flags=re.I)
    s = re.sub(r"^(о|про|насч[её]т)\s+", "", s, flags=re.I)
    return s.strip() or "Напоминание"

def mk_dt(d: date, h: int, m: int) -> datetime:
    return tz.localize(datetime(d.year, d.month, d.day, h % 24, m % 60, 0, 0))

def fmt_dt(dt: datetime) -> str:
    return f"{dt.strftime('%d.%m %H:%M')}"

def soonest(dts): return sorted(dts, key=lambda x: x)

def human_label(dt: datetime) -> str:
    now = datetime.now(tz)
    if dt.date() == now.date():
        dword = "Сегодня"
    elif dt.date() == (now + timedelta(days=1)).date():
        dword = "Завтра"
    else:
        dword = dt.strftime("%d.%m")
    return f"{dword} в {dt.strftime('%H:%M')}"

def kb_variants(dts):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=human_label(dt), callback_data=f"time|{dt.isoformat()}")]
            for dt in soonest(dts)
        ]
    )

def plan(rem):
    scheduler.add_job(send_reminder, "date", run_date=rem["remind_dt"], args=[rem["user_id"], rem["text"]])

async def send_reminder(uid: int, text: str):
    try:
        await bot.send_message(uid, f"🔔🔔 {text}")
    except Exception as e:
        print("send_reminder error:", e)

# ========= FFMPEG DETECTION =========
def resolve_ffmpeg_path() -> str:
    env = os.getenv("FFMPEG_PATH")
    if env:
        return os.path.realpath(env)

    found = shutil.which("ffmpeg")
    if found:
        return os.path.realpath(found)

    raise FileNotFoundError(
        "ffmpeg not found. Установи ffmpeg на сервер или задай FFMPEG_PATH=/usr/bin/ffmpeg"
    )

FFMPEG_PATH = resolve_ffmpeg_path()
print(f"[init] Using ffmpeg at: {FFMPEG_PATH}")

# ========= COMMANDS =========
@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer("Привет! Я бот-напоминалка. Просто напиши текст напоминания и время, например:\n"
                   "— Завтра в 19:00 спорт\n"
                   "— Через 10 минут позвонить\n"
                   "— 25.08 в 14:30 встреча\n"
                   "/list — список, /ping — проверка.")

@dp.message(Command("ping"))
async def cmd_ping(m: Message): 
    await m.answer("pong ✅")

@dp.message(Command("list"))
async def cmd_list(m: Message):
    uid = m.from_user.id
    items = [r for r in REMINDERS if r["user_id"] == uid]
    if not items:
        await m.answer("Пока нет напоминаний.")
        return
    items = sorted(items, key=lambda r: r["remind_dt"])
    lines = [f"• {r['text']} — {fmt_dt(r['remind_dt'])}" for r in items]
    await m.answer("\n".join(lines))

# ========= TEXT HANDLER =========
@dp.message(F.text)
async def on_text(m: Message):
    # здесь должна быть логика парсинга дат/времени (сократил ради примера)
    text = norm(m.text)
    uid = m.from_user.id
    dt = datetime.now(tz) + timedelta(minutes=1)  # временно всегда +1 минута
    desc = clean_desc(text)
    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
    plan(REMINDERS[-1])
    await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt(dt)}")

# ========= CALLBACK =========
@dp.callback_query(F.data.startswith("time|"))
async def cb_time(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in PENDING or not PENDING[uid].get("variants"):
        await cb.answer("Нет активного уточнения"); return
    iso = cb.data.split("|", 1)[1]
    dt = datetime.fromisoformat(iso)
    dt = tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)
    desc = PENDING[uid].get("description","Напоминание")
    PENDING.pop(uid, None)
    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
    plan(REMINDERS[-1])
    try:
        await cb.message.edit_text(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
    except Exception:
        await cb.message.answer(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
    await cb.answer("Установлено ✅")

# ========= RUN =========
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
