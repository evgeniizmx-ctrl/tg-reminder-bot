#!/usr/bin/env python3
import os
import re
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

import pytz
import yaml
from pydantic import BaseModel
from openai import OpenAI

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ----------------- ЛОГИ -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("reminder-bot")

# ----------------- ENV -----------------
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_APIKEY") or ""
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY missing — LLM parsing will fail.")

# ----------------- DB -----------------
DB_PATH = os.getenv("DB_PATH", "reminders.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute(
    """
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  when_ts INTEGER,                  -- unix ts для одноразовых и "время суток" старта для периодики
  tz TEXT,                          -- "+03:00"
  repeat TEXT DEFAULT 'none',       -- 'none'|'daily'|'weekly'|'monthly'
  day_of_week INTEGER,              -- 1..7 (Пн..Вс) для weekly
  day_of_month INTEGER,             -- 1..31 для monthly
  state TEXT DEFAULT 'active',      -- 'active'|'done'|'cancelled'
  created_at INTEGER
);
"""
)
conn.commit()

# ----------------- МЕНЮ -----------------
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📋 Список напоминаний")],
        [KeyboardButton("⚙️ Настройки")],
    ],
    resize_keyboard=True,
)

# ----------------- PROMPTS -----------------
class PromptPack(BaseModel):
    system: str | None = None
    parse: Dict[str, Any]
    fewshot: List[Dict[str, str]] | None = None

def load_prompts() -> PromptPack:
    path = os.getenv("PROMPTS_PATH", "prompts.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    pack = PromptPack(**raw)
    log.info(
        "Prompts loaded: system=%s | fewshot=%s",
        (pack.parse.get("system", "")[:40] + "...") if pack.parse.get("system") else None,
        len(pack.fewshot or []),
    )
    return pack

PROMPTS = load_prompts()

# ----------------- ДАТЫ/ВРЕМЯ -----------------
def ensure_tz_offset(s: Optional[str]) -> str:
    if not s:
        return "+03:00"
    m = re.fullmatch(r"[+-]\d{2}:\d{2}", s.strip())
    return s.strip() if m else "+03:00"

def now_iso_with_tz(tz_offset: str) -> str:
    sign = 1 if tz_offset.startswith("+") else -1
    hh, mm = map(int, tz_offset[1:].split(":"))
    off = timezone(timedelta(minutes=sign * (hh * 60 + mm)))
    return datetime.now(off).replace(microsecond=0).isoformat()

def iso_to_unix(iso_str: str) -> int:
    dt = datetime.fromisoformat(iso_str)
    return int(dt.timestamp())

def unix_to_local_str(ts: int, tz_offset: str) -> str:
    sign = 1 if tz_offset.startswith("+") else -1
    hh, mm = map(int, tz_offset[1:].split(":"))
    off = timezone(timedelta(minutes=sign * (hh * 60 + mm)))
    dt = datetime.fromtimestamp(ts, tz=off)
    return dt.strftime("%d.%m %H:%M")

def build_today_time_iso(tz_offset: str, hhmm: str) -> str:
    sign = 1 if tz_offset.startswith("+") else -1
    hh_off, mm_off = map(int, tz_offset[1:].split(":"))
    off = timezone(timedelta(minutes=sign * (hh_off * 60 + mm_off)))
    now = datetime.now(off)
    h, m = map(int, hhmm.split(":"))
    dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if dt <= now:
        dt += timedelta(days=1)
    return dt.isoformat()

# ----------------- OPENAI -----------------
openai_client = OpenAI(api_key=OPENAI_API_KEY)

async def call_llm(text: str, tz_offset: str) -> Optional[dict]:
    tz_offset = ensure_tz_offset(tz_offset)
    now_iso = now_iso_with_tz(tz_offset)
    system_text = PROMPTS.parse.get("system", "")

    messages = []
    if system_text:
        messages.append({"role": "system", "content": system_text})

    messages.append({"role": "user", "content": f"NOW_ISO={now_iso}  TZ_DEFAULT={tz_offset}"})

    for fs in PROMPTS.fewshot or []:
        role = fs.get("role")
        content = fs.get("content", "")
        if role and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": text})

    try:
        resp = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(json|JSON)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        data = json.loads(raw)
        return data
    except Exception as e:
        log.error("LLM error: %s", e, exc_info=False)
        return None

# ----------------- УТОЧНЕНИЯ -----------------
PENDING: Dict[int, Dict[str, Any]] = {}  # user_id -> {"pending":{}, "clarify_count":int}
MAX_CLARIFY = 2

def upsert_pending(user_id: int, payload: dict):
    d = PENDING.get(user_id, {"pending": {}, "clarify_count": 0})
    pen = d["pending"]
    for k in ["title", "description", "timezone", "fixed_datetime", "repeat", "day_of_week", "day_of_month"]:
        if payload.get(k) is not None:
            pen[k] = payload[k]
    PENDING[user_id] = {"pending": pen, "clarify_count": d["clarify_count"]}

def inc_clarify(user_id: int):
    d = PENDING.get(user_id, {"pending": {}, "clarify_count": 0})
    d["clarify_count"] += 1
    PENDING[user_id] = d

def render_options_keyboard(options: List[dict], cb_prefix: str = "clarify") -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, opt in enumerate(options):
        data = {
            "iso": opt.get("iso_datetime") or "",
            "dow": str(opt.get("day_of_week") or ""),
            "dom": str(opt.get("day_of_month") or ""),
        }
        cb_data = f"{cb_prefix}:{data['iso']}:{data['dow']}:{data['dom']}"
        label = opt.get("label", "…")
        row.append(InlineKeyboardButton(label, callback_data=cb_data))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ----------------- CRUD -----------------
def save_reminder(
    user_id: int,
    chat_id: int,
    title: str,
    when_iso: Optional[str],
    tz_offset: str,
    repeat: str = "none",
    day_of_week: Optional[int] = None,
    day_of_month: Optional[int] = None,
) -> int:
    when_ts = iso_to_unix(when_iso) if when_iso else None
    cur.execute(
        """
        INSERT INTO reminders (user_id, chat_id, title, when_ts, tz, repeat, day_of_week, day_of_month, state, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            user_id,
            chat_id,
            title,
            when_ts,
            tz_offset,
            repeat,
            day_of_week,
            day_of_month,
            "active",
            int(datetime.utcnow().timestamp()),
        ),
    )
    conn.commit()
    return cur.lastrowid

def snooze_reminder(rem_id: int, minutes: int) -> Optional[int]:
    cur.execute("SELECT * FROM reminders WHERE id=? AND state='active'", (rem_id,))
    row = cur.fetchone()
    if not row:
        return None
    new_ts = (row["when_ts"] or int(datetime.utcnow().timestamp())) + minutes * 60
    cur.execute("UPDATE reminders SET when_ts=? WHERE id=?", (new_ts, rem_id))
    conn.commit()
    return new_ts

def delete_reminder(rem_id: int) -> bool:
    cur.execute("UPDATE reminders SET state='cancelled' WHERE id=?", (rem_id,))
    conn.commit()
    return cur.rowcount > 0

# ----------------- ПЕРИОДИКА -----------------
def compute_next_fire(row: sqlite3.Row) -> Optional[int]:
    repeat = row["repeat"]
    if repeat == "none":
        return None

    tz_off = ensure_tz_offset(row["tz"] or "+03:00")
    sign = 1 if tz_off.startswith("+") else -1
    hh, mm = map(int, tz_off[1:].split(":"))
    off_minutes = sign * (hh * 60 + mm)
    off = timezone(timedelta(minutes=off_minutes))

    now_local = datetime.now(off).replace(second=0, microsecond=0)
    base_time = datetime.fromtimestamp(row["when_ts"], tz=off) if row["when_ts"] else now_local

    if repeat == "daily":
        nxt = now_local.replace(hour=base_time.hour, minute=base_time.minute, second=0, microsecond=0)
        if nxt <= now_local:
            nxt += timedelta(days=1)
        return int(nxt.astimezone(timezone.utc).timestamp())

    if repeat == "weekly":
        target_dow = int(row["day_of_week"] or 1)  # 1..7
        current_dow = now_local.isoweekday()
        delta = (target_dow - current_dow) % 7
        nxt = now_local.replace(hour=base_time.hour, minute=base_time.minute, second=0, microsecond=0)
        if delta == 0 and nxt <= now_local:
            delta = 7
        nxt = nxt + timedelta(days=delta)
        return int(nxt.astimezone(timezone.utc).timestamp())

    if repeat == "monthly":
        dom = int(row["day_of_month"] or 1)
        y, m = now_local.year, now_local.month
        import calendar
        last = calendar.monthrange(y, m)[1]
        d = dom if dom <= last else last
        candidate = now_local.replace(day=d, hour=base_time.hour, minute=base_time.minute, second=0, microsecond=0)
        if candidate <= now_local:
            if m == 12:
                y, m = y + 1, 1
            else:
                m += 1
            last = calendar.monthrange(y, m)[1]
            d = dom if dom <= last else last
            candidate = candidate.replace(year=y, month=m, day=d)
        return int(candidate.astimezone(timezone.utc).timestamp())

    return None

def bump_next(row_id: int, next_ts: Optional[int]):
    if next_ts:
        cur.execute("UPDATE reminders SET when_ts=? WHERE id=?", (next_ts, row_id))
    else:
        cur.execute("UPDATE reminders SET state='done' WHERE id=?", (row_id,))
    conn.commit()

# ----------------- SCHEDULER (JobQueue tick) -----------------
async def scheduler_tick(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    try:
        now_ts = int(datetime.utcnow().timestamp())
        cur.execute(
            "SELECT * FROM reminders WHERE state='active' AND when_ts IS NOT NULL AND when_ts <= ?",
            (now_ts,),
        )
        due = cur.fetchall()
        for row in due:
            tz_off = row["tz"] or "+03:00"
            when_str = unix_to_local_str(row["when_ts"], tz_off)
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("⏰ через 10 мин", callback_data=f"snooze:10:{row['id']}"),
                    InlineKeyboardButton("🕒 через 1 час", callback_data=f"snooze:60:{row['id']}"),
                    InlineKeyboardButton("✅ Готово", callback_data=f"done:{row['id']}")
                ]]
            )
            try:
                await app.bot.send_message(
                    chat_id=row["chat_id"],
                    text=f"📅 Напоминание: {row['title']}\n⏱ {when_str}",
                    reply_markup=kb,
                )
            except Exception as e:
                log.error("Send reminder failed: %s", e, exc_info=False)

            next_ts = compute_next_fire(row)
            bump_next(row["id"], next_ts)
    except Exception as e:
        log.error("Scheduler error: %s", e, exc_info=False)

# ----------------- ХЕЛПЕРЫ UI -----------------
def format_period_suffix(row: sqlite3.Row) -> str:
    if row["repeat"] == "none":
        return ""
    if row["repeat"] == "daily":
        return " (каждый день)"
    if row["repeat"] == "weekly":
        map_dow = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}
        lbl = map_dow.get(row["day_of_week"] or 1, "?")
        return f" (еженедельно, {lbl})"
    if row["repeat"] == "monthly":
        dom = row["day_of_month"] or 1
        return f" (ежемесячно, {dom} числа)"
    return ""

# ----------------- HANDLERS -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я напоминалка. Напиши что и когда напомнить.\n"
        "Например: «завтра в 11 падел», «через 10 минут позвонить», «каждый день в 9 пить таблетки».",
        reply_markup=MAIN_MENU,
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Примеры:\n"
        "• завтра в 11 падел\n"
        "• через 10 минут позвонить\n"
        "• каждый день в 9 пить таблетки\n"
        "• раз в неделю в среду в 19 — зал\n"
        "• каждое 5 число месяца в 18 — баня\n",
        reply_markup=MAIN_MENU,
    )

async def reload_prompts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PROMPTS
    try:
        PROMPTS = load_prompts()
        await update.message.reply_text("🔄 Промпты перезагружены.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка загрузки промптов: {e}")

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cur.execute(
        "SELECT * FROM reminders WHERE user_id=? AND state='active' ORDER BY when_ts IS NULL, when_ts ASC",
        (user_id,),
    )
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("Пока нет активных напоминаний.")
        return

    for r in rows:
        tz = r["tz"] or "+03:00"
        when_s = "-" if r["when_ts"] is None else unix_to_local_str(r["when_ts"], tz)
        suffix = format_period_suffix(r)
        text = f"• {r['title']}\n   ⏱ {when_s}{suffix}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{r['id']}")]])
        await update.message.reply_text(text, reply_markup=kb)

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.startswith("📋"):
        await list_reminders(update, context)
    elif txt.startswith("⚙️"):
        await update.message.reply_text("Настройки пока в разработке.", reply_markup=MAIN_MENU)
    else:
        await handle_text(update, context)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    tz_offset = ensure_tz_offset(context.user_data.get("tz_offset") or "+03:00")

    data = await call_llm(text, tz_offset)
    if not data:
        await update.message.reply_text("Не понял. Пример: «завтра в 6 падел».")
        return

    intent = data.get("intent", "create_reminder")
    title = data.get("title") or "Напоминание"
    fixed = data.get("fixed_datetime")
    repeat = data.get("repeat", "none")
    day_of_week = data.get("day_of_week")
    day_of_month = data.get("day_of_month")
    timezone_str = ensure_tz_offset(data.get("timezone") or tz_offset)

    if intent == "ask_clarification":
        # сохраняем промежуточные
        upsert_pending(user.id, data)
        d = PENDING.get(user.id, {"clarify_count": 0})
        if d["clarify_count"] >= MAX_CLARIFY:
            pen = PENDING[user.id]["pending"]
            rem_id = save_reminder(
                user.id,
                chat_id,
                pen.get("title", title),
                pen.get("fixed_datetime"),
                ensure_tz_offset(pen.get("timezone") or timezone_str),
                pen.get("repeat", "none"),
                pen.get("day_of_week"),
                pen.get("day_of_month"),
            )
            PENDING.pop(user.id, None)
            pretty = (
                unix_to_local_str(iso_to_unix(pen["fixed_datetime"]), ensure_tz_offset(pen.get("timezone") or timezone_str))
                if pen.get("fixed_datetime")
                else "—"
            )
            await update.message.reply_text(f"📅 Окей, записал: {pen.get('title','Напоминание')} — {pretty}", reply_markup=MAIN_MENU)
            return

        d["clarify_count"] += 1
        PENDING[user.id] = d
        opts = data.get("options") or []
        if not opts:
            std = ["08:00", "12:00", "19:00"]
            opts = [{"iso_datetime": build_today_time_iso(timezone_str, hh), "label": hh} for hh in std]
        kb = render_options_keyboard(opts, cb_prefix="clarify")
        await update.message.reply_text("Уточни, пожалуйста:", reply_markup=kb)
        return

    # create_reminder
    rem_id = save_reminder(
        user.id,
        chat_id,
        title,
        fixed,
        timezone_str,
        repeat,
        day_of_week,
        day_of_month,
    )
    if repeat == "none":
        pretty = unix_to_local_str(iso_to_unix(fixed), timezone_str) if fixed else "—"
        await update.message.reply_text(f"📅 Окей, напомню: {title}\n⏱ {pretty}", reply_markup=MAIN_MENU)
    else:
        when_s = "-" if not fixed else unix_to_local_str(iso_to_unix(fixed), timezone_str)
        suffix = ""
        if repeat == "daily":
            suffix = "каждый день"
        elif repeat == "weekly":
            map_dow = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}
            suffix = f"еженедельно, {map_dow.get(day_of_week or 1,'?')}"
        elif repeat == "monthly":
            suffix = f"каждое {day_of_month} число"
        await update.message.reply_text(
            f"📅 Окей, напомню: {title}\n⏱ {when_s} ({suffix})",
            reply_markup=MAIN_MENU,
        )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    if data.startswith("snooze:"):
        try:
            _, mins, rem_id = data.split(":", 2)
            mins = int(mins)
            rem_id = int(rem_id)
        except:
            return
        new_ts = snooze_reminder(rem_id, mins)
        if new_ts:
            cur.execute("SELECT tz, title FROM reminders WHERE id=?", (rem_id,))
            row = cur.fetchone()
            tz_off = row["tz"] or "+03:00"
            new_local = unix_to_local_str(new_ts, tz_off)
            await q.edit_message_text(f"🕒 Отложено до {new_local} — {row['title']}")
    elif data.startswith("done:"):
        try:
            _, rem_id = data.split(":", 1)
            rem_id = int(rem_id)
        except:
            return
        cur.execute("UPDATE reminders SET state='done' WHERE id=?", (rem_id,))
        conn.commit()
        await q.edit_message_text("✅ Выполнено")
    elif data.startswith("del:"):
        try:
            _, rem_id = data.split(":", 1)
            rem_id = int(rem_id)
        except:
            return
        ok = delete_reminder(rem_id)
        if ok:
            await q.edit_message_text("🗑 Удалено")
    elif data.startswith("clarify:"):
        try:
            _, iso, dow, dom = data.split(":", 3)
        except:
            return
        user_id = q.from_user.id
        d = PENDING.get(user_id, {"pending": {}, "clarify_count": 0})
        pen = d["pending"]
        if iso:
            pen["fixed_datetime"] = iso
        if dow:
            try:
                pen["day_of_week"] = int(dow)
            except:
                pass
        if dom:
            try:
                pen["day_of_month"] = int(dom)
            except:
                pass
        PENDING[user_id] = {"pending": pen, "clarify_count": d["clarify_count"]}

        repeat = pen.get("repeat", "none")
        ready = False
        if repeat == "none":
            ready = bool(pen.get("fixed_datetime"))
        elif repeat == "daily":
            ready = bool(pen.get("fixed_datetime"))
        elif repeat == "weekly":
            ready = bool(pen.get("fixed_datetime")) and bool(pen.get("day_of_week"))
        elif repeat == "monthly":
            ready = bool(pen.get("fixed_datetime")) and bool(pen.get("day_of_month"))

        if ready:
            save_reminder(
                user_id,
                q.message.chat_id,
                pen.get("title", "Напоминание"),
                pen.get("fixed_datetime"),
                ensure_tz_offset(pen.get("timezone") or "+03:00"),
                pen.get("repeat", "none"),
                pen.get("day_of_week"),
                pen.get("day_of_month"),
            )
            PENDING.pop(user_id, None)
            pretty = (
                unix_to_local_str(iso_to_unix(pen["fixed_datetime"]), ensure_tz_offset(pen.get("timezone") or "+03:00"))
                if pen.get("fixed_datetime")
                else "-"
            )
            await q.edit_message_text(f"✅ Записал: {pen.get('title','Напоминание')} — {pretty}")
        else:
            d = PENDING.get(user_id, {"pending": pen, "clarify_count": 0})
            if d["clarify_count"] >= MAX_CLARIFY:
                save_reminder(
                    user_id,
                    q.message.chat_id,
                    pen.get("title", "Напоминание"),
                    pen.get("fixed_datetime"),
                    ensure_tz_offset(pen.get("timezone") or "+03:00"),
                    pen.get("repeat", "none"),
                    pen.get("day_of_week"),
                    pen.get("day_of_month"),
                )
                PENDING.pop(user_id, None)
                await q.edit_message_text("✅ Записал (по лучшему предположению).")
                return
            d["clarify_count"] += 1
            PENDING[user_id] = d

            opts = []
            if not pen.get("fixed_datetime"):
                for hh in ["08:00", "12:00", "19:00"]:
                    iso = build_today_time_iso(ensure_tz_offset(pen.get("timezone") or "+03:00"), hh)
                    opts.append({"iso_datetime": iso, "label": hh, "day_of_week": None, "day_of_month": None})
                await q.edit_message_text("Уточни время:", reply_markup=render_options_keyboard(opts))
                return
            if pen.get("repeat") == "weekly" and not pen.get("day_of_week"):
                dows = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
                opts = [{"iso_datetime": "", "label": lbl, "day_of_week": i + 1, "day_of_month": None} for i, lbl in enumerate(dows)]
                await q.edit_message_text("Выбери день недели:", reply_markup=render_options_keyboard(opts))
                return
            if pen.get("repeat") == "monthly" and not pen.get("day_of_month"):
                dom_opts = [1, 5, 10, 15, 20, 25]
                opts = [{"iso_datetime": "", "label": str(x), "day_of_week": None, "day_of_month": x} for x in dom_opts]
                await q.edit_message_text("Выбери число месяца:", reply_markup=render_options_keyboard(opts))
                return

# ----------------- APP -----------------
def build_app() -> Application:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reload", reload_prompts))

    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(snooze|done|del|clarify):"))
    app.add_handler(MessageHandler(filters.Regex(r"^(📋 Список напоминаний|⚙️ Настройки)$"), handle_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # 🔔 Планировщик через JobQueue: каждые 30 сек, первый запуск через 5 сек
    app.job_queue.run_repeating(scheduler_tick, interval=30, first=5)

    return app

def main():
    log.info("Bot starting… polling enabled")
    app = build_app()
    # PTB v20+: run_polling() вместо ручного initialize/start/updater.idle()
    app.run_polling()

if __name__ == "__main__":
    main()
