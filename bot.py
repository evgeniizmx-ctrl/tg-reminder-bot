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

missing = []
if not BOT_TOKEN: missing.append("BOT_TOKEN")
if not OPENAI_API_KEY: missing.append("OPENAI_API_KEY")
if not os.path.exists(PROMPTS_PATH): missing.append(f"{PROMPTS_PATH} (prompts.yaml)")
if missing:
    log.error("Missing required environment/files: %s", ", ".join(missing))
    sys.exit(1)

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
        try: conn.execute("alter table reminders add column kind text default 'oneoff'")
        except Exception: pass
        try: conn.execute("alter table reminders add column recurrence_json text")
        except Exception: pass
        conn.commit()

def db_get_user_tz(user_id: int) -> str | None:
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

def db_add_reminder_oneoff(user_id: int, title: str, body: str | None, when_iso_utc: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "insert into reminders(user_id,title,body,when_iso,kind) values(?,?,?,?,?)",
            (user_id, title, body, when_iso_utc, 'oneoff')
        )
        conn.commit()
        return cur.lastrowid

def db_add_reminder_recurring(user_id: int, title: str, body: str | None, recurrence: dict, tz: str) -> int:
    rec = dict(recurrence or {})
    rec["tz"] = tz
    with db() as conn:
        cur = conn.execute(
            "insert into reminders(user_id,title,body,when_iso,kind,recurrence_json) values(?,?,?,?,?,?)",
            (user_id, title, body, None, 'recurring', json.dumps(rec, ensure_ascii=False))
        )
        conn.commit()
        return cur.lastrowid

def db_mark_done(rem_id: int):
    with db() as conn:
        conn.execute("update reminders set status='done' where id=?", (rem_id,))
        conn.commit()

def db_snooze(rem_id: int, minutes: int):
    with db() as conn:
        row = conn.execute("select when_iso, kind from reminders where id=?", (rem_id,)).fetchone()
        if not row:
            return None, None
        if row["kind"] == "recurring":
            return "recurring", None
        dt = parse_iso(row["when_iso"]) + timedelta(minutes=minutes)
        new_iso = iso_utc(dt)
        conn.execute("update reminders set when_iso=?, status='scheduled' where id=?", (new_iso, rem_id))
        conn.commit()
        return "oneoff", dt

def db_delete(rem_id: int):
    with db() as conn:
        conn.execute("delete from reminders where id=?", (rem_id,))
        conn.commit()

def db_future(user_id: int):
    with db() as conn:
        return conn.execute(
            "select * from reminders where user_id=? and status='scheduled' order by id desc",
            (user_id,)
        ).fetchall()

def db_get_reminder(rem_id: int):
    with db() as conn:
        return conn.execute("select * from reminders where id=?", (rem_id,)).fetchone()

# ---------- TZ / ISO ----------
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
    if dt.tzinfo is None: raise ValueError("aware dt required")
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat()

def parse_iso(s: str) -> datetime:
    return dparser.isoparse(s)

def to_user_local(utc_iso: str, user_tz: str) -> datetime:
    return parse_iso(utc_iso).astimezone(tzinfo_from_user(user_tz))

# ---------- UI ----------
MAIN_MENU_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📝 Список напоминаний"), KeyboardButton("⚙️ Настройки")]],
    resize_keyboard=True, one_time_keyboard=False
)

# ---------- Prompts ----------
import yaml
def load_prompts():
    with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
PROMPTS = load_prompts()

# ---------- OpenAI ----------
from openai import OpenAI
_client = None
def get_openai():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client

async def call_llm(user_text: str, user_tz: str, now_iso_override: str | None = None) -> dict:
    now_local = now_in_user_tz(user_tz)
    if now_iso_override:
        try: now_local = dparser.isoparse(now_iso_override)
        except Exception: pass
    header = f"NOW_ISO={now_local.replace(microsecond=0).isoformat()}\nTZ_DEFAULT={user_tz or '+03:00'}"
    messages = [
        {"role": "system", "content": PROMPTS["system"]},
        {"role": "system", "content": header},
        {"role": "system", "content": PROMPTS["parse"]["system"]},
    ]
    messages.extend(PROMPTS.get("fewshot") or [])
    messages.append({"role": "user", "content": user_text})
    client = get_openai()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.2
    )
    txt = resp.choices[0].message.content.strip()
    m = re.search(r"\{[\s\S]+\}", txt)
    return json.loads(m.group(0) if m else txt)

# ---------- Handlers ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (оставлено как было у тебя — логика напоминаний + LLM)
    pass  # тут твой код без изменений

# ---------- Voice Handler ----------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Распознаём voice и пускаем в ту же логику, что и текст."""
    try:
        voice = update.message.voice
        if not voice:
            return
        tg_file = await voice.get_file()
        local_path = f"/tmp/{voice.file_id}.ogg"
        await tg_file.download_to_drive(local_path)

        client = get_openai()
        with open(local_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f,
            )
        text = (transcript.text or "").strip()

        if not text:
            await update.message.reply_text("Не расслышал голосовое. Скажи ещё раз текстом?")
            return

        update.message.text = text
        await handle_text(update, context)

    except Exception as e:
        log.exception("voice -> text failed: %s", e)
        await update.message.reply_text("Не смог распознать голосовое. Попробуй текстом, пожалуйста.")

# ---------- main ----------
def main():
    log.info("Starting PlannerBot...")
    db_init()

    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(lambda a: None)
           .build())

    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Привет!")))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
