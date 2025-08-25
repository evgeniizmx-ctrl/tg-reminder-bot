# bot.py
import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# OpenAI (pip install openai==1.40.0)
from openai import OpenAI

# APScheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bot")

# ========= ENV & CLIENTS =========
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PROMPTS_PATH = os.getenv("PROMPTS_PATH", "prompts.yaml")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not BOT_TOKEN:
    raise RuntimeError("No BOT_TOKEN/TELEGRAM_TOKEN in env")

# OpenAI
oi = OpenAI(api_key=OPENAI_API_KEY)

# ========= DB (fallback, можно заменить на свой сторедж) =========
DB_PATH = os.getenv("DB_PATH", "data.db")

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS user_prefs(
          user_id INTEGER PRIMARY KEY,
          tz TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          title TEXT NOT NULL,
          iso TEXT NOT NULL,
          tz TEXT,
          done INTEGER DEFAULT 0
        )
        """
    )
    con.commit()
    return con

DB = db_connect()

def db_get_tz(user_id: int) -> Optional[str]:
    cur = DB.execute("SELECT tz FROM user_prefs WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] else None

def db_set_tz(user_id: int, tz: str) -> None:
    DB.execute(
        "INSERT INTO user_prefs(user_id, tz) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz",
        (user_id, tz),
    )
    DB.commit()

def db_add_reminder(user_id: int, title: str, iso: str, tz: Optional[str]) -> int:
    cur = DB.execute(
        "INSERT INTO reminders(user_id, title, iso, tz, done) VALUES(?,?,?,?,0)",
        (user_id, title, iso, tz or ""),
    )
    DB.commit()
    return cur.lastrowid

def db_mark_done(rem_id: int) -> None:
    DB.execute("UPDATE reminders SET done=1 WHERE id=?", (rem_id,))
    DB.commit()

def db_list_future(user_id: int) -> List[sqlite3.Row]:
    DB.row_factory = sqlite3.Row
    cur = DB.execute(
        "SELECT id, title, iso, tz, done FROM reminders WHERE user_id=? AND done=0 ORDER BY iso ASC",
        (user_id,),
    )
    return cur.fetchall()

# ========= PROMPTS =========
@dataclass
class PromptPack:
    system: str
    parse: List[Dict[str, str]]  # few-shot list: [{'role':'user'|'assistant', 'content': '...'}, ...]

def load_prompts() -> PromptPack:
    with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if "system" not in raw or "parse" not in raw:
        raise ValueError("prompts.yaml must have 'system' and 'parse' sections")

    # normalize few-shot to list of dicts
    few = raw["fewshot"] if "fewshot" in raw else raw.get("parse", [])
    parse = []
    for item in few:
        if isinstance(item, dict) and "role" in item and "content" in item:
            parse.append({"role": item["role"], "content": item["content"]})
    pp = PromptPack(system=raw["system"], parse=parse)
    log.info(
        "Prompts loaded: system=%s | fewshot=%d",
        (pp.system[:40] + "…") if len(pp.system) > 40 else pp.system,
        len(pp.parse),
    )
    return pp

PROMPTS = load_prompts()

# ========= SCHEDULER =========
SCHED = AsyncIOScheduler()
SCHED.start()

# ========= Utils =========
def fmt_user_dt(dt: datetime, tz_str: Optional[str]) -> str:
    """
    Показываем человеку без секунд и без оффсета: 25.08 в 11:00
    tz_str может быть IANA ('Europe/Moscow') или смещение '+03:00'
    """
    try:
        if tz_str and "/" in tz_str:
            dt = dt.astimezone(ZoneInfo(tz_str))
        return dt.strftime("%d.%m в %H:%M")
    except Exception:
        return dt.strftime("%d.%m в %H:%M")

def to_rfc3339_no_seconds(dt: datetime) -> str:
    """Формат для хранения: YYYY-MM-DDTHH:MM±HH:MM (без секунд)."""
    # У dt может быть оффсет вида +0300 — преобразуем к +03:00
    s = dt.strftime("%Y-%m-%dT%H:%M%z")
    if len(s) >= 5:
        return s[:-2] + ":" + s[-2:]
    return s

def make_now_iso_and_tzdefault(user_tz: Optional[str]) -> (str, str):
    """
    NOW_ISO — в локальном поясе пользователя.
    TZ_DEFAULT — смещение вида +03:00 (нужно для модели).
    """
    if user_tz and "/" in user_tz:
        now_local = datetime.now(ZoneInfo(user_tz))
    else:
        # смещение вида +03:00 — зададим fixed offset
        if user_tz and re.match(r"^[\+\-]\d{2}:\d{2}$", user_tz):
            hours = int(user_tz[1:3])
            mins = int(user_tz[4:6])
            sign = 1 if user_tz[0] == "+" else -1
            tzinfo = timezone(sign * timedelta(hours=hours, minutes=mins))
            now_local = datetime.now(tzinfo)
        else:
            now_local = datetime.now().astimezone()

    tz_default = now_local.strftime("%z")
    tz_default = tz_default[:3] + ":" + tz_default[3:]  # +0300 -> +03:00
    now_iso = now_local.isoformat(timespec="seconds")   # для модели секунды норм
    return now_iso, tz_default

def build_llm_messages(user_text: str, user_tz: Optional[str], pp: PromptPack) -> List[Dict[str, str]]:
    now_iso, tz_default = make_now_iso_and_tzdefault(user_tz)
    sys_suffix = f"\nNOW_ISO={now_iso}\nTZ_DEFAULT={tz_default}\n"
    msgs: List[Dict[str, str]] = [{"role": "system", "content": pp.system + sys_suffix}]
    msgs.extend(pp.parse)
    msgs.append({"role": "user", "content": user_text})
    return msgs

async def call_llm(user_text: str, user_tz: Optional[str]) -> Dict[str, Any]:
    """
    Возвращает JSON (dict) от LLM по нашему промту
    """
    msgs = build_llm_messages(user_text, user_tz, PROMPTS)
    resp = oi.chat.completions.create(
        model=MODEL,
        messages=msgs,
        temperature=0.2,
    )
    content = resp.choices[0].message.content.strip()
    # LLM обязан вернуть JSON — парсим
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # на крайняк — выдернем JSON через жадный матч
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise
        data = json.loads(m.group(0))
    return data

# ========= Онбординг TZ =========
ASYNC_TZ_FLAG = "awaiting_tz"

async def ensure_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    uid = update.effective_user.id
    tz = db_get_tz(uid)
    if tz:
        return tz
    context.user_data[ASYNC_TZ_FLAG] = True
    await update.effective_chat.send_message(
        "Для начала укажи свой часовой пояс.\n"
        "Можешь прислать:\n"
        "• смещение: +03:00\n"
        "• или IANA-зону: Europe/Moscow"
    )
    return None

def normalize_tz(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if re.match(r"^[\+\-]\d{2}:\d{2}$", raw):
        return raw
    # попробуем IANA
    try:
        ZoneInfo(raw)
        return raw
    except Exception:
        return None

# ========= Планирование (простое) =========
async def fire_reminder(app: Application, user_id: int, rem_id: int, title: str):
    try:
        await app.bot.send_message(
            chat_id=user_id,
            text=f"🔔 {title}",
        )
        db_mark_done(rem_id)
    except Exception as e:
        log.exception("send reminder failed: %s", e)

def schedule_once(app: Application, when_dt: datetime, user_id: int, rem_id: int, title: str):
    SCHED.add_job(
        fire_reminder,
        trigger=DateTrigger(run_date=when_dt),
        args=(app, user_id, rem_id, title),
        id=f"once:{rem_id}",
        replace_existing=True,
        misfire_grace_time=60,
    )

# ========= Handlers =========
async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PROMPTS
    try:
        PROMPTS = load_prompts()
        await update.message.reply_text("Промпты перезагружены ✅")
    except Exception as e:
        await update.message.reply_text(f"Не удалось перезагрузить: {e}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(ASYNC_TZ_FLAG, None)
    tz = db_get_tz(update.effective_user.id)
    if not tz:
        await ensure_tz(update, context)
        return
    await update.message.reply_text(
        f"Часовой пояс установлен: {tz}\n"
        "Теперь напиши что и когда напомнить."
    )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = db_list_future(uid)
    if not rows:
        await update.message.reply_text("Будущих напоминаний нет.")
        return

    lines = ["🗒️ Ближайшие:"]
    kb = []
    for r in rows:
        iso = r["iso"]
        tz = r["tz"] or db_get_tz(uid)
        dt = datetime.fromisoformat(iso)
        lines.append(f"• {fmt_user_dt(dt, tz)} — «{r['title']}»")
        kb.append([
            InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{r['id']}")
        ])
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("del:"):
        rem_id = int(data.split(":", 1)[1])
        db_mark_done(rem_id)
        await q.edit_message_text("Удалено ✅")
        return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()

    # ждём TZ?
    if context.user_data.get(ASYNC_TZ_FLAG):
        tz = normalize_tz(txt)
        if not tz:
            await update.message.reply_text("Не понял. Введи смещение (+03:00) или зону (Europe/Moscow).")
            return
        db_set_tz(uid, tz)
        context.user_data.pop(ASYNC_TZ_FLAG, None)
        await update.message.reply_text(f"Часовой пояс установлен: {tz}\nТеперь напиши что и когда напомнить.")
        return

    user_tz = db_get_tz(uid)
    if not user_tz:
        tz = await ensure_tz(update, context)
        return

    # зовём LLM
    try:
        parsed = await call_llm(txt, user_tz)
    except Exception as e:
        log.exception("LLM error: %s", e)
        await update.message.reply_text("Я не понял, попробуй ещё раз.")
        return

    intent = parsed.get("intent")
    if intent == "chat":
        # фолбек — просто ответим
        await update.message.reply_text("Я не понял, скажи, например: «завтра в 15 позвонить маме».")
        return

    if intent == "ask_clarification":
        # упрощённо — покажем опции, если они есть
        options = parsed.get("options") or []
        if not options:
            await update.message.reply_text("Нужно уточнение.")
            return
        btns = []
        for op in options[:3]:
            iso = op.get("iso_datetime")
            label = op.get("label") or iso
            btns.append([InlineKeyboardButton(label, callback_data=f"pick:{iso}")])
        await update.message.reply_text("Уточни, пожалуйста:", reply_markup=InlineKeyboardMarkup(btns))
        return

    if intent == "create_reminder":
        title = parsed.get("title") or parsed.get("description") or "Напоминание"
        iso = parsed.get("fixed_datetime")
        if not iso:
            await update.message.reply_text("Не нашёл времени. Попробуй точнее.")
            return
        # поддержим оба формата: без секунд (наш) и с секундами — LLM иногда шлёт с секундами
        try:
            dt = datetime.fromisoformat(iso)
        except Exception:
            # поправим, если пришло "YYYY-MM-DD HH:MM:SS+03"
            iso_fixed = iso.replace(" ", "T")
            if re.match(r".*\+\d{2}$", iso_fixed):
                iso_fixed += ":00"
            dt = datetime.fromisoformat(iso_fixed)

        # сохраняем и планируем
        rem_id = db_add_reminder(uid, title, to_rfc3339_no_seconds(dt), user_tz)
        schedule_once(context.application, dt, uid, rem_id, title)

        pretty = fmt_user_dt(dt, user_tz)
        await update.message.reply_text(f"📅 Окей, напомню «{title}» {pretty}")
        return

    # неизвестный интент
    await update.message.reply_text("Я не понял, попробуй ещё раз.")


# CB для выбора из ask_clarification
async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("pick:"):
        return
    iso = data.split(":", 1)[1]
    uid = q.from_user.id
    tz = db_get_tz(uid)
    title = "Напоминание"

    # распарсим дату
    iso_fixed = iso.replace(" ", "T")
    if re.match(r".*\+\d{2}$", iso_fixed):
        iso_fixed += ":00"
    dt = datetime.fromisoformat(iso_fixed)

    rem_id = db_add_reminder(uid, title, to_rfc3339_no_seconds(dt), tz)
    schedule_once(context.application, dt, uid, rem_id, title)

    pretty = fmt_user_dt(dt, tz)
    await q.edit_message_text(f"📅 Окей, напомню «{title}» {pretty}")


# =========================
# APP & ENTRYPOINT (PTB v20)
# =========================

def build_app() -> Application:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Хэндлеры команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("list", cmd_list))

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(on_cb,   pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick:"))

    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


def main():
    app = build_app()
    # Если у тебя где-то есть SCHED.start(), оставь его выше по файлу (он уже стартует)
    # Запуск PTB v20: без asyncio.run/initialize/updater.start_polling
    log.info("Bot starting… polling enabled")
    app.run_polling(close_loop=False)  # close_loop=False чтобы не закрывать уже существующий loop


if __name__ == "__main__":
    main()
