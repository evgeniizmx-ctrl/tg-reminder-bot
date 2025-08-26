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
import tempfile
import uuid

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
                when_iso text,                   -- UTC ISO
                status text default 'scheduled',
                kind text default 'oneoff',      -- 'oneoff' | 'recurring'
                recurrence_json text             -- JSON {type,weekday,day,time,tz}
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
    # не обнуляем секунды, чтобы не попасть в прошлое и не словить misfire
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

_TZ_ROWS = [
    ["Калининград (+2)", "Москва (+3)"],
    ["Самара (+4)", "Екатеринбург (+5)"],
    ["Омск (+6)", "Новосибирск (+7)"],
    ["Иркутск (+8)", "Якутск (+9)"],
    ["Хабаровск (+10)", "Другой…"],
]
CITY_TO_OFFSET = {
    "Калининград (+2)": "+02:00",
    "Москва (+3)": "+03:00",
    "Самара (+4)": "+04:00",
    "Екатеринбург (+5)": "+05:00",
    "Омск (+6)": "+06:00",
    "Новосибирск (+7)": "+07:00",
    "Иркутск (+8)": "+08:00",
    "Якутск (+9)": "+09:00",
    "Хабаровск (+10)": "+10:00",
}
def build_tz_inline_kb() -> InlineKeyboardMarkup:
    rows = []
    for row in _TZ_ROWS:
        btns = []
        for label in row:
            if label == "Другой…":
                btns.append(InlineKeyboardButton(label, callback_data="tz:other"))
            else:
                off = CITY_TO_OFFSET[label]
                btns.append(InlineKeyboardButton(label, callback_data=f"tz:{off}"))
        rows.append(btns)
    return InlineKeyboardMarkup(rows)

async def safe_reply(update: Update, text: str, reply_markup=None):
    if update and update.message:
        return await update.message.reply_text(text, reply_markup=reply_markup)
    chat = update.effective_chat if update else None
    if chat:
        return await chat.send_message(text, reply_markup=reply_markup)
    return None

def normalize_offset(sign: str, hh: str, mm: str | None) -> str:
    return f"{sign}{int(hh):02d}:{int(mm or 0):02d}"

def parse_tz_input(text: str) -> str | None:
    t = (text or "").strip()
    if t in CITY_TO_OFFSET: return CITY_TO_OFFSET[t]
    m = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?$", t)
    if m: return normalize_offset(m.group(1), m.group(2), m.group(3))
    if "/" in t and " " not in t:
        try: ZoneInfo(t); return t
        except Exception: return None
    return None

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

# ---------- Rule-based quick parse ----------
def _clean_spaces(s: str) -> str: return re.sub(r"\s+", " ", s).strip()
def _extract_title(text: str) -> str:
    t = text
    t = re.sub(r"\b(сегодня|завтра|послезавтра)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\bчерез\b\s+[^,;.]+", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\bв\s+\d{1,2}(:\d{2})?\s*(час(?:а|ов)?|ч)?\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\bв\s+\d{1,2}\b", " ", t, flags=re.IGNORECASE)
    t = _clean_spaces(t.strip(" ,.;—-"))
    return t.capitalize() if t else "Напоминание"

def rule_parse(text: str, now_local: datetime):
    s = text.strip().lower()
    m = re.search(r"через\s+(полчаса|минуту|\d+\s*мин(?:ут)?|\d+\s*час(?:а|ов)?)", s)
    if m:
        delta = timedelta()
        ch = m.group(1)
        if "полчаса" in ch: delta = timedelta(minutes=30)
        elif "минуту" in ch: delta = timedelta(minutes=1)
        elif "мин" in ch: delta = timedelta(minutes=int(re.search(r"\d+", ch).group()))
        else: delta = timedelta(hours=int(re.search(r"\d+", ch).group()))
        when_local = now_local + delta
        return {"intent": "create", "title": _extract_title(text), "when_local": when_local}
    md = re.search(r"\b(сегодня|завтра|послезавтра)\b", s)
    mt = re.search(r"\bв\s+(\d{1,2})(?::?(\d{2}))?\s*(час(?:а|ов)?|ч)?\b", s)
    if md and mt:
        base = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[md.group(1)]
        day = (now_local + timedelta(days=base)).date()
        hh = int(mt.group(1)); mm = int(mt.group(2) or 0)
        title = _extract_title(text)
        if mt.group(2) is None and 1 <= hh <= 12:
            return {"intent":"ask","title":title,"base_date":day.isoformat(),"question":"Уточни, пожалуйста, время",
                    "variants":[f"{hh:02d}:00", f"{(hh%12)+12:02d}:00"]}
        when_local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=now_local.tzinfo)
        return {"intent": "create", "title": title, "when_local": when_local}
    return None

# ---------- Scheduler (APScheduler внутри PTB loop) ----------
scheduler: AsyncIOScheduler | None = None
TG_BOT = None  # PTB bot instance для отправки сообщений из APScheduler

async def fire_reminder(*, chat_id: int, rem_id: int, title: str, kind: str = "oneoff"):
    """Колбэк APScheduler — получает kwargs, без PTB context."""
    try:
        kb_rows = [[
            InlineKeyboardButton("Через 10 мин", callback_data=f"snooze:10:{rem_id}"),
            InlineKeyboardButton("Через 1 час", callback_data=f"snooze:60:{rem_id}")
        ]]
        if kind == "oneoff":
            kb_rows.append([InlineKeyboardButton("✅", callback_data=f"done:{rem_id}")])

        await TG_BOT.send_message(chat_id, f"🔔 «{title}»", reply_markup=InlineKeyboardMarkup(kb_rows))
        log.info("Fired reminder id=%s to chat=%s", rem_id, chat_id)
    except Exception as e:
        log.exception("fire_reminder failed: %s", e)

def ensure_scheduler() -> AsyncIOScheduler:
    if scheduler is None:
        raise RuntimeError("Scheduler not initialized yet")
    return scheduler

def schedule_oneoff(rem_id: int, user_id: int, when_iso_utc: str, title: str, kind: str = "oneoff"):
    sch = ensure_scheduler()
    dt_utc = parse_iso(when_iso_utc)
    sch.add_job(
        fire_reminder, DateTrigger(run_date=dt_utc),
        id=f"rem-{rem_id}", replace_existing=True, misfire_grace_time=300, coalesce=True,
        kwargs={"chat_id": user_id, "rem_id": rem_id, "title": title, "kind": kind},
        name=f"rem {rem_id}",
    )
    log.info("Scheduled oneoff id=%s at %s UTC (title=%s)", rem_id, dt_utc.isoformat(), title)
    sch.print_jobs()

def schedule_recurring(rem_id: int, user_id: int, title: str, recurrence: dict, tz_str: str):
    sch = ensure_scheduler()
    tzinfo = tzinfo_from_user(tz_str)
    rtype = recurrence.get("type"); time_str = recurrence.get("time"); hh, mm = map(int, time_str.split(":"))
    if rtype == "daily":
        trigger = CronTrigger(hour=hh, minute=mm, timezone=tzinfo)
    elif rtype == "weekly":
        trigger = CronTrigger(day_of_week=recurrence.get("weekday"), hour=hh, minute=mm, timezone=tzinfo)
    else:
        trigger = CronTrigger(day=int(recurrence.get("day")), hour=hh, minute=mm, timezone=tzinfo)
    sch.add_job(
        fire_reminder, trigger,
        id=f"rem-{rem_id}", replace_existing=True, misfire_grace_time=600, coalesce=True,
        kwargs={"chat_id": user_id, "rem_id": rem_id, "title": title, "kind": "recurring"},
        name=f"rem {rem_id}",
    )
    log.info("Scheduled recurring id=%s (%s %s, tz=%s)", rem_id, rtype, time_str, tz_str)
    sch.print_jobs()

def reschedule_all():
    sch = ensure_scheduler()
    with db() as conn:
        rows = conn.execute("select * from reminders where status='scheduled'").fetchall()
    for r in rows:
        if (r["kind"] or "oneoff") == "oneoff" and r["when_iso"]:
            schedule_oneoff(r["id"], r["user_id"], r["when_iso"], r["title"], kind="oneoff")
        else:
            rec = json.loads(r["recurrence_json"] or "{}")
            tz = rec.get("tz") or "+03:00"
            if rec:
                schedule_recurring(r["id"], r["user_id"], r["title"], rec, tz)
    log.info("Rescheduled %d reminders from DB", len(rows))

# ---------- RU wording for weekly + list formatting ----------
def ru_weekly_phrase(weekday_code: str) -> str:
    mapping = {
        "mon": ("каждый", "понедельник"),
        "tue": ("каждый", "вторник"),
        "wed": ("каждую", "среду"),
        "thu": ("каждый", "четверг"),
        "fri": ("каждую", "пятницу"),
        "sat": ("каждую", "субботу"),
        "sun": ("каждое", "воскресенье"),
    }
    det, word = mapping.get((weekday_code or "").lower(), ("каждый", weekday_code or "день"))
    return f"{det} {word}"

def format_reminder_line(row: sqlite3.Row, user_tz: str) -> str:
    title = row["title"]
    kind = row["kind"] or "oneoff"
    if kind == "oneoff" and row["when_iso"]:
        dt_local = to_user_local(row["when_iso"], user_tz)
        return f"{dt_local.strftime('%d.%m в %H:%M')} — «{title}»"
    rec = json.loads(row["recurrence_json"]) if row["recurrence_json"] else {}
    rtype = rec.get("type")
    time_str = rec.get("time") or "00:00"
    if rtype == "daily":
        return f"каждый день в {time_str} — «{title}»"
    if rtype == "weekly":
        wd = ru_weekly_phrase(rec.get("weekday", ""))
        return f"{wd} в {time_str} — «{title}»"
    day = rec.get("day")
    return f"каждое {day}-е число в {time_str} — «{title}»"

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tz = db_get_user_tz(user_id)
    if not tz:
        await safe_reply(update,
            "Для начала укажи свой часовой пояс.\n"
            "Выбери город или пришли вручную смещение (+03:00) или IANA (Europe/Moscow).",
            reply_markup=MAIN_MENU_KB
        )
        await safe_reply(update, "Выбери из списка:", reply_markup=build_tz_inline_kb())
        return
    await safe_reply(update, f"Часовой пояс установлен: {tz}\nТеперь напиши что и когда напомнить.",
                     reply_markup=MAIN_MENU_KB)

async def try_handle_tz_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text: return False
    tz = parse_tz_input(update.message.text.strip())
    if tz is None: return False
    db_set_user_tz(update.effective_user.id, tz)
    await safe_reply(update, f"Часовой пояс установлен: {tz}\nТеперь напиши что и когда напомнить.",
                     reply_markup=MAIN_MENU_KB)
    return True

async def cb_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data
    if not data.startswith("tz:"): return
    value = data.split(":",1)[1]; chat_id = q.message.chat.id
    if value == "other":
        await q.edit_message_text("Пришли смещение вида +03:00 или IANA-зону (Europe/Moscow)."); return
    db_set_user_tz(chat_id, value)
    await q.edit_message_text(f"Часовой пояс установлен: {value}\nТеперь напиши что и когда напомнить.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = db_future(user_id)
    if not rows:
        return await safe_reply(update, "Будущих напоминаний нет.", reply_markup=MAIN_MENU_KB)

    tz = db_get_user_tz(user_id) or "+03:00"
    header = "🗓 Ближайшие напоминания —"
    kb_rows = []
    for r in rows:
        line = format_reminder_line(r, tz)
        kb_rows.append([InlineKeyboardButton(f"🗑 {line}", callback_data=f"del:{r['id']}")])

    await safe_reply(update, header, reply_markup=InlineKeyboardMarkup(kb_rows))

async def cb_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data or ""
    if data.startswith("del:"):
        rem_id = int(data.split(":")[1]); db_delete(rem_id)
        sch = ensure_scheduler(); job = sch.get_job(f"rem-{rem_id}")
        if job: job.remove()
        await q.edit_message_text("Удалено ✅"); return
    if data.startswith("snooze:"):
        _, mins, rem_id = data.split(":"); rem_id = int(rem_id); mins = int(mins)
        kind, _ = db_snooze(rem_id, mins); row = db_get_reminder(rem_id)
        if not row: return await q.edit_message_text("Ошибка: напоминание не найдено.")
        if kind == "oneoff":
            schedule_oneoff(rem_id, row["user_id"], row["when_iso"], row["title"], kind="oneoff")
            await q.edit_message_text(f"⏲ Отложено на {mins} мин.")
        else:
            when = iso_utc(datetime.now(timezone.utc) + timedelta(minutes=mins))
            sch = ensure_scheduler()
            sch.add_job(
                fire_reminder, DateTrigger(run_date=parse_iso(when)),
                id=f"snooze-{rem_id}", replace_existing=True, misfire_grace_time=60, coalesce=True,
                kwargs={"chat_id": row["user_id"], "rem_id": rem_id, "title": row["title"], "kind":"oneoff"},
                name=f"snooze {rem_id}",
            )
            await q.edit_message_text(f"⏲ Отложено на {mins} мин. (одноразово)")
        return
    if data.startswith("done:"):
        rem_id = int(data.split(":")[1]); db_mark_done(rem_id)
        sch = ensure_scheduler(); job = sch.get_job(f"rem-{rem_id}")
        if job: job.remove()
        await q.edit_message_text("✅ Выполнено"); return

async def cb_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    try: await q.edit_message_reply_markup(None)
    except Exception: pass
    data = q.data or ""
    if not data.startswith("pick:"): return
    iso_local = data.split("pick:")[1]; user_id = q.message.chat.id
    tz = db_get_user_tz(user_id) or "+03:00"; title = "Напоминание"
    when_local = dparser.isoparse(iso_local)
    if when_local.tzinfo is None: when_local = when_local.replace(tzinfo=tzinfo_from_user(tz))
    when_iso_utc = iso_utc(when_local)
    rem_id = db_add_reminder_oneoff(user_id, title, None, when_iso_utc)
    schedule_oneoff(rem_id, user_id, when_iso_utc, title, kind="oneoff")
    dt_local = to_user_local(when_iso_utc, tz)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
    await safe_reply(update, f"🔔🔔 Окей, напомню «{title}» {dt_local.strftime('%d.%m в %H:%M')}", reply_markup=kb)

async def cb_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    try: await q.edit_message_reply_markup(None)
    except Exception: pass
    data = q.data or ""
    if not data.startswith("answer:"): return
    choice = data.split("answer:",1)[1]
    cstate = context.user_data.get("clarify_state") or {}
    base_date = cstate.get("base_date"); title = cstate.get("title") or "Напоминание"
    user_id = q.message.chat.id; tz = db_get_user_tz(user_id) or "+03:00"
    if base_date:
        m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?$", choice.strip())
        if m:
            hh = int(m.group(1)); mm = int(m.group(2) or 0)
            when_local = datetime.fromisoformat(base_date).replace(hour=hh, minute=mm, tzinfo=tzinfo_from_user(tz))
            when_iso_utc = iso_utc(when_local)
            rem_id = db_add_reminder_oneoff(user_id, title, None, when_iso_utc)
            schedule_oneoff(rem_id, user_id, when_iso_utc, title, kind="oneoff")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
            return await safe_reply(update, f"🔔🔔 Окей, напомню «{title}» {when_local.strftime('%d.%m в %H:%M')}",
                                    reply_markup=kb)
    context.user_data["__auto_answer"] = choice
    await handle_text(update, context)

def get_clarify_state(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.get("clarify_state")
def set_clarify_state(context: ContextTypes.DEFAULT_TYPE, state: dict | None):
    if state is None: context.user_data.pop("clarify_state", None)
    else: context.user_data["clarify_state"] = state

# ---------- main text ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await try_handle_tz_input(update, context): return
    user_id = update.effective_user.id
    incoming_text = (context.user_data.pop("__auto_answer", None)
                     or (update.message.text.strip() if update.message and update.message.text else ""))

    if incoming_text == "📝 Список напоминаний" or incoming_text.lower() == "/list":
        return await cmd_list(update, context)
    if incoming_text == "⚙️ Настройки" or incoming_text.lower() == "/settings":
        return await safe_reply(update, "Раздел «Настройки» в разработке.", reply_markup=MAIN_MENU_KB)

    user_tz = db_get_user_tz(user_id)
    if not user_tz:
        await safe_reply(update, "Сначала укажи часовой пояс.", reply_markup=MAIN_MENU_KB)
        await safe_reply(update, "Выбери из списка:", reply_markup=build_tz_inline_kb()); return

    now_local = now_in_user_tz(user_tz)
    r = rule_parse(incoming_text, now_local)
    if r:
        if r["intent"] == "create":
            title = r["title"]; when_iso_utc = iso_utc(r["when_local"])
            rem_id = db_add_reminder_oneoff(user_id, title, None, when_iso_utc)
            schedule_oneoff(rem_id, user_id, when_iso_utc, title, kind="oneoff")
            dt_local = to_user_local(when_iso_utc, user_tz)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
            return await safe_reply(update, f"🔔🔔 Окей, напомню «{title}» {dt_local.strftime('%d.%м в %H:%M')}",
                                    reply_markup=kb)
        if r["intent"] == "ask":
            set_clarify_state(context, {
                "original": incoming_text,
                "base_date": r["base_date"],
                "title": r["title"],
            })
            kb_rows = [[InlineKeyboardButton(v, callback_data=f"answer:{v}")] for v in r["variants"]]
            return await safe_reply(update, r["question"], reply_markup=InlineKeyboardMarkup(kb_rows))

    # LLM
    result = await call_llm(incoming_text, user_tz)
    intent = result.get("intent")

    if intent == "ask_clarification":
        question = result.get("question") or "Уточни, пожалуйста."
        variants = result.get("variants") or []
        set_clarify_state(context, {"original": result.get("text_original") or incoming_text})
        kb_rows = []
        for v in variants[:6]:
            if isinstance(v, dict):
                label = v.get("label") or v.get("text") or v.get("iso_datetime") or "Выбрать"
                iso = v.get("iso_datetime")
                kb_rows.append([InlineKeyboardButton(label, callback_data=(f"pick:{iso}" if iso else f"answer:{label}"))])
            else:
                kb_rows.append([InlineKeyboardButton(str(v), callback_data=f"answer:{v}")])
        return await safe_reply(update, question, reply_markup=InlineKeyboardMarkup(kb_rows) if kb_rows else None)

    if intent == "create_reminder":
        title = result.get("title") or "Напоминание"
        body = result.get("description")
        dt_iso_local = result.get("fixed_datetime")
        recurrence = result.get("recurrence")

        if recurrence:
            rem_id = db_add_reminder_recurring(user_id, title, body, recurrence, user_tz)
            schedule_recurring(rem_id, user_id, title, recurrence, user_tz)
            rtype = recurrence.get("type")
            if rtype == "daily":
                text = f"🔔🔔 Окей, буду напоминать «{title}» каждый день в {recurrence.get('time')}"
            elif rtype == "weekly":
                text = f"🔔🔔 Окей, буду напоминать «{title}» {ru_weekly_phrase(recurrence.get('weekday'))} в {recurrence.get('time')}"
            else:
                text = f"🔔🔔 Окей, буду напоминать «{title}» каждое {recurrence.get('day')}-е число в {recurrence.get('time')}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
            return await safe_reply(update, text, reply_markup=kb)

        if not dt_iso_local:
            return await safe_reply(update, "Не понял время. Напиши, например: «сегодня 18:30».")
        when_local = dparser.isoparse(dt_iso_local)
        if when_local.tzinfo is None:
            when_local = when_local.replace(tzinfo=tzinfo_from_user(user_tz))
        when_iso_utc = iso_utc(when_local)
        rem_id = db_add_reminder_oneoff(user_id, title, body, when_iso_utc)
        schedule_oneoff(rem_id, user_id, when_iso_utc, title, kind="oneoff")
        dt_local = to_user_local(when_iso_utc, user_tz)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
        return await safe_reply(update, f"🔔🔔 Окей, напомню «{title}» {dt_local.strftime('%d.%m в %H:%M')}",
                                reply_markup=kb)

    await safe_reply(update, "Я не понял, попробуй ещё раз.", reply_markup=MAIN_MENU_KB)

# ---------- Voice -> Text ----------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Распознаём voice и передаём результат в ту же логику, что и текст."""
    try:
        voice = update.message.voice if update.message else None
        if not voice:
            return
        file = await voice.get_file()
        # сохраняем во временный файл
        tmpdir = tempfile.gettempdir()
        path_ogg = os.path.join(tmpdir, f"{uuid.uuid4().hex}.ogg")
        await file.download_to_drive(path_ogg)

        client = get_openai()
        with open(path_ogg, "rb") as f:
            tr = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f,
            )
        text = (getattr(tr, "text", "") or "").strip()
        try:
            os.remove(path_ogg)
        except Exception:
            pass

        if not text:
            return await update.message.reply_text("Не расслышал голосовое. Скажи ещё раз текстом?")

        # подменяем текст в апдейте и используем тот же пайплайн
        update.message.text = text
        await handle_text(update, context)

    except Exception as e:
        log.exception("Voice recognition failed: %s", e)
        await update.message.reply_text("Не смог распознать голосовое. Попробуй текстом, пожалуйста.")

# ---------- Startup: APScheduler в PTB loop ----------
async def on_startup(app: Application):
    global scheduler, TG_BOT
    TG_BOT = app.bot
    loop = asyncio.get_running_loop()
    scheduler = AsyncIOScheduler(
        timezone=timezone.utc,
        event_loop=loop,
        job_defaults={"coalesce": True, "misfire_grace_time": 600}
    )
    scheduler.start()
    log.info("APScheduler started in PTB event loop")
    reschedule_all()

# ---------- main ----------
def main():
    log.info("Starting PlannerBot...")
    db_init()

    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(on_startup)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("settings", lambda u,c: u.message.reply_text(
        "Раздел «Настройки» в разработке.", reply_markup=MAIN_MENU_KB)))
    app.add_handler(CallbackQueryHandler(cb_tz, pattern=r"^tz:"))
    app.add_handler(CallbackQueryHandler(cb_inline, pattern=r"^(del:|done:|snooze:)"))
    app.add_handler(CallbackQueryHandler(cb_pick, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(cb_answer, pattern=r"^answer:"))

    # текст + войс
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
