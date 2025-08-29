# bot.py
import os
import re
import json
import socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl

import psycopg
from psycopg.rows import dict_row

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import asyncio
import tempfile

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
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("planner-bot")

# ---------- ENV ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
PROMPTS_PATH = os.environ.get("PROMPTS_PATH", "prompts.yaml")
DB_PATH = os.environ.get("DB_PATH", "reminders.db")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
DB_DIALECT = ((os.environ.get("DB_DIALECT") or ("postgres" if DATABASE_URL else "sqlite")).strip().lower())
log.info("DB mode pick: DB_DIALECT=%r, DATABASE_URL=%r", DB_DIALECT, DATABASE_URL)

missing = []
if not BOT_TOKEN: missing.append("BOT_TOKEN")
if not os.path.exists(PROMPTS_PATH): missing.append(f"{PROMPTS_PATH} (prompts.yaml)")
if missing:
    log.error("Missing required environment/files: %s", ", ".join(missing))
    sys.exit(1)

if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY не задан — LLM-парсер недоступен, но быстрый парсер покроет типовые кейсы.")

log.info("DB mode: %s (DATABASE_URL=%s)", DB_DIALECT, "set" if DATABASE_URL else "not set")

# ---------- Helpers ----------
def _url_with_ipv4_host(url: str) -> tuple[str, str | None, dict]:
    """
    Возвращает (new_url, ipv4, parts)
    - new_url: URL с подставленным IPv4 в netloc (если вышло), иначе исходный
    - ipv4: найденный IPv4 (или None)
    - parts: разобранные части (scheme, username, password, host, port, path, query)
    """
    if not url:
        return url, None, {}

    p = urlsplit(url)
    host = p.hostname
    port = p.port or 5432
    scheme = p.scheme
    user = p.username
    password = p.password
    query = p.query
    parts = {
        "scheme": scheme, "username": user, "password": password,
        "host": host, "port": port, "path": p.path, "query": query
    }

    if not host:
        return url, None, parts

    # 1) ручной override
    ipv4_env = (os.environ.get("DB_HOST_IPV4") or "").strip() or None
    ipv4 = None
    if ipv4_env:
        ipv4 = ipv4_env
    else:
        # 2) простой фоллбек (IPv4)
        try:
            ipv4 = socket.gethostbyname(host)
        except Exception:
            ipv4 = None

    if not ipv4:
        # не получилось — вернём исходный URL
        return url, None, parts

    # Соберём netloc: [user[:pass]@]ipv4[:port]
    userinfo = ""
    if user:
        userinfo = user
        if password:
            userinfo += f":{password}"
        userinfo += "@"
    netloc = f"{userinfo}{ipv4}:{port}"
    new_url = urlunsplit((scheme, netloc, p.path, query, p.fragment))
    return new_url, ipv4, parts

# ---------- DB ----------
def db():
    """
    Подключение к БД:
    - если postgres: форсим IPv4 (URL или kwargs/hostaddr).
    - иначе sqlite.
    """
    if DB_DIALECT != "postgres":
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    conn_url_ipv4, ipv4, parts = _url_with_ipv4_host(DATABASE_URL)
    log.info("Postgres connect try: url_ipv4=%s, ipv4=%s, host=%s",
             "set" if conn_url_ipv4 != DATABASE_URL else "same",
             ipv4, parts.get("host"))

    # Попытка 1: прям URL с IPv4
    try:
        return psycopg.connect(conn_url_ipv4, autocommit=True, row_factory=dict_row)
    except Exception as e1:
        log.warning("IPv4 URL connect failed, will try kwargs hostaddr. Err=%r", e1)
        last_err = e1

    # Попытка 2: kwargs с hostaddr (если IPv4 есть)
    if not ipv4:
        raise last_err

    qs = dict(parse_qsl(parts.get("query") or "", keep_blank_values=True))
    sslmode = qs.get("sslmode", "require")

    kwargs = {
        "hostaddr": ipv4,
        "host": parts["host"],            # для TLS SNI/cert
        "port": parts["port"] or 5432,
        "dbname": (parts["path"][1:] if parts["path"].startswith("/") else parts["path"] or "postgres"),
        "user": parts["username"],
        "password": parts["password"],
        "sslmode": sslmode,
        "autocommit": True,
        "row_factory": dict_row,
    }
    log.info("Postgres connect kwargs: %s", {k: kwargs[k] for k in ("hostaddr","host","port","dbname","sslmode")})
    return psycopg.connect(**kwargs)

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

def to_user_local(utc_iso: str, user_tz: str) -> datetime:
    return dparser.isoparse(utc_iso).astimezone(tzinfo_from_user(user_tz))

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
    if update and getattr(update, "message", None):
        try:
            return await update.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            pass
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
        _client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _client

async def call_llm(user_text: str, user_tz: str, now_iso_override: str | None = None) -> dict:
    """Возвращает dict-инструкцию. Ожидаемые ключи:
       intent: create | create_recurring | create_interval | ask
       title: str
       when_local: iso (для create)
       recurrence: {type,..., tz?} (для recurring/interval)
       question/variants (для ask)
    """
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
    txt = (resp.choices[0].message.content or "").strip()
    log.debug("LLM raw response: %s", txt)
    m = re.search(r"\{[\s\S]+\}", txt)
    payload = m.group(0) if m else txt
    try:
        return json.loads(payload)
    except Exception:
        log.exception("LLM JSON parse failed. Raw: %s", txt)
        return {}

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

    # интервалы: «каждые 15 мин», «каждый час»
    m_int = re.search(r"\bкажды(е|й|е)\s+(\d+)\s*(сек|секунд\w*|мин\w*|час\w*)\b", s)
    if m_int:
        n = int(m_int.group(2))
        unit_raw = m_int.group(3)
        unit = "second" if unit_raw.startswith("сек") else ("minute" if unit_raw.startswith("мин") else "hour")
        return {"intent": "create_interval", "title": _extract_title(text), "unit": unit, "n": n, "start_at": now_local}

    if re.search(r"\bкажд(ую|ый)\s+минут(у|ы)?\b", s):
        return {"intent": "create_interval", "title": _extract_title(text), "unit": "minute", "n": 1, "start_at": now_local}

    # «через …»
    if re.search(r"\bчерез\s+(полчаса|минуту|\d+\s*мин(?:ут)?|\d+\s*час(?:а|ов)?)\b", s):
        m = re.search(r"через\s+(полчаса|минуту|\d+\s*мин(?:ут)?|\d+\s*час(?:а|ов)?)", s)
        delta = timedelta()
        ch = m.group(1)
        if "полчаса" in ch: delta = timedelta(minutes=30)
        elif "минуту" in ch: delta = timedelta(minutes=1)
        elif "мин" in ch: delta = timedelta(minutes=int(re.search(r"\d+", ch).group()))
        else: delta = timedelta(hours=int(re.search(r"\d+", ch).group()))
        when_local = now_local + delta
        return {"intent": "create", "title": _extract_title(text), "when_local": when_local}

    # «завтра/сегодня/послезавтра в 11[:40]»
    md = re.search(r"\b(сегодня|завтра|послезавтра)\b", s)
    # допускаем без минут («в 11»)
    mt = re.search(r"\bв\s+(\d{1,2})(?::?(\d{2}))?\b", s)
    # пол-формы: «в полшестого», «в пол пятого»
    m_half = re.search(r"\bв\s+пол\s*([а-я]+)ого\b", s)
    if md and (mt or m_half):
        base = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[md.group(1)]
        day = (now_local + timedelta(days=base)).date()
        hh = 0; mm = 0
        if m_half:
            # мапинг русских слов на часы
            names = {
                "втор": 2, "тр": 3, "четв": 4, "пят": 5, "шест": 6, "сед": 7, "восьм": 8, "дев": 9, "десят": 10, "одиннадц": 11, "двенадц": 12
            }
            word = m_half.group(1)
            # подберём по префиксу
            target = next((names[k] for k in names if word.startswith(k)), 6)
            hh = target - 1
            if hh <= 0: hh += 12
            mm = 30
        else:
            hh = int(mt.group(1)); mm = int(mt.group(2) or 0)

        title = _extract_title(text)
        if (mt and mt.group(2) is None) and (not m_half) and (1 <= hh <= 12):
            # уточнение 12/24
            return {"intent":"ask","title":title,"base_date":day.isoformat(),"question":"Уточни, пожалуйста, время",
                    "variants":[f"{hh:02d}:00", f"{(hh%12)+12:02d}:00"]}

        when_local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=now_local.tzinfo)
        return {"intent": "create", "title": title, "when_local": when_local}
    return None

# ---------- DB helpers ----------
def db_get_user_tz(user_id: int) -> str | None:
    with db() as conn:
        if DB_DIALECT == "postgres":
            r = conn.execute("select tz from users where user_id=%s", (user_id,)).fetchone()
        else:
            r = conn.execute("select tz from users where user_id=?", (user_id,)).fetchone()
        return r["tz"] if r else None

def db_set_user_tz(user_id: int, tz: str):
    with db() as conn:
        if DB_DIALECT == "postgres":
            conn.execute("insert into users(user_id, tz) values(%s,%s) on conflict (user_id) do update set tz=excluded.tz",
                         (user_id, tz))
        else:
            conn.execute("insert or replace into users(user_id, tz) values(?,?)", (user_id, tz))
            conn.commit()

def db_add_reminder_oneoff(user_id: int, title: str, body: str | None, when_iso_utc: str) -> int:
    with db() as conn:
        if DB_DIALECT == "postgres":
            r = conn.execute(
                "insert into reminders(user_id, title, body, when_iso, status, kind) values(%s,%s,%s,%s,'scheduled','oneoff') returning id",
                (user_id, title, body, when_iso_utc)
            ).fetchone()
            return r["id"]
        else:
            cur = conn.execute(
                "insert into reminders(user_id, title, body, when_iso, status, kind) values(?,?,?,?, 'scheduled','oneoff')",
                (user_id, title, body, when_iso_utc)
            )
            conn.commit()
            return cur.lastrowid

def db_add_reminder_recurring(user_id: int, title: str, body: str | None, recurrence: dict, tz: str) -> int:
    rec = dict(recurrence or {})
    if "tz" not in rec: rec["tz"] = tz
    rec_json = json.dumps(rec, ensure_ascii=False)
    with db() as conn:
        if DB_DIALECT == "postgres":
            r = conn.execute(
                "insert into reminders(user_id, title, body, when_iso, status, kind, recurrence_json) "
                "values(%s,%s,%s,%s,'scheduled','recurring',%s) returning id",
                (user_id, title, body, None, rec_json)
            ).fetchone()
            return r["id"]
        else:
            cur = conn.execute(
                "insert into reminders(user_id, title, body, when_iso, status, kind, recurrence_json) "
                "values(?,?,?,?,'scheduled','recurring',?)",
                (user_id, title, body, None, rec_json)
            )
            conn.commit()
            return cur.lastrowid

def db_delete(rem_id: int):
    with db() as conn:
        if DB_DIALECT == "postgres":
            conn.execute("delete from reminders where id=%s", (rem_id,))
        else:
            conn.execute("delete from reminders where id=?", (rem_id,))
            conn.commit()

def db_mark_done(rem_id: int):
    with db() as conn:
        if DB_DIALECT == "postgres":
            conn.execute("update reminders set status='done' where id=%s", (rem_id,))
        else:
            conn.execute("update reminders set status='done' where id=?", (rem_id,))
            conn.commit()

def db_get_reminder(rem_id: int):
    with db() as conn:
        if DB_DIALECT == "postgres":
            r = conn.execute("select * from reminders where id=%s", (rem_id,)).fetchone()
        else:
            r = conn.execute("select * from reminders where id=?", (rem_id,)).fetchone()
        return r

def db_snooze(rem_id: int, minutes: int) -> tuple[str, str | None]:
    """Возвращает (kind, new_when_iso) — для recurring new_when_iso одноразово."""
    with db() as conn:
        if DB_DIALECT == "postgres":
            row = conn.execute("select * from reminders where id=%s", (rem_id,)).fetchone()
        else:
            row = conn.execute("select * from reminders where id=?", (rem_id,)).fetchone()

        if not row: return "missing", None
        kind = (row["kind"] or "oneoff").lower()
        if kind == "oneoff":
            new_iso = iso_utc(dparser.isoparse(row["when_iso"]).astimezone(timezone.utc) + timedelta(minutes=minutes))
            if DB_DIALECT == "postgres":
                conn.execute("update reminders set when_iso=%s where id=%s", (new_iso, rem_id))
            else:
                conn.execute("update reminders set when_iso=? where id=?", (new_iso, rem_id)); conn.commit()
            return kind, new_iso
        else:
            # recurring — не меняем запись; вернём new_when одноразово
            new_iso = iso_utc(datetime.now(timezone.utc) + timedelta(minutes=minutes))
            return kind, new_iso

def db_future(user_id: int):
    with db() as conn:
        q = (
            "select * from reminders where user_id=%s and status='scheduled' order by id desc"
            if DB_DIALECT == "postgres"
            else "select * from reminders where user_id=? and status='scheduled' order by id desc"
        )
        try:
            cur = conn.execute(q, (user_id,))
            rows = cur.fetchall() or []
            return rows
        except Exception:
            log.exception("db_future query failed")
            return []

# ---------- Scheduler ----------
scheduler: AsyncIOScheduler | None = None
TG_BOT = None

async def fire_reminder(*, chat_id: int, rem_id: int, title: str, kind: str = "oneoff"):
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
    dt_utc = dparser.isoparse(when_iso_utc)
    sch.add_job(
        fire_reminder, DateTrigger(run_date=dt_utc),
        id=f"rem-{rem_id}", replace_existing=True, misfire_grace_time=300, coalesce=True,
        kwargs={"chat_id": user_id, "rem_id": rem_id, "title": title, "kind": kind},
        name=f"rem {rem_id}",
    )
    sch.print_jobs()

def schedule_recurring(rem_id: int, user_id: int, title: str, recurrence: dict, tz_str: str):
    sch = ensure_scheduler()
    rtype = (recurrence.get("type") or "").lower()

    if rtype == "interval":
        unit = (recurrence.get("unit") or "").lower()
        n = int(recurrence.get("n") or 1)
        start_at = recurrence.get("start_at")
        start_dt_local = dparser.isoparse(start_at) if start_at else now_in_user_tz(tz_str)
        start_dt_utc = start_dt_local.astimezone(timezone.utc)
        kwargs = {}
        if unit == "second":
            kwargs["seconds"] = n
        elif unit == "minute":
            kwargs["minutes"] = n
        else:
            kwargs["hours"] = n
        trigger = IntervalTrigger(start_date=start_dt_utc, **kwargs)
    else:
        tzinfo = tzinfo_from_user(tz_str)
        time_str = recurrence.get("time") or "00:00"
        hh, mm = map(int, time_str.split(":"))
        if rtype == "daily":
            trigger = CronTrigger(hour=hh, minute=mm, timezone=tzinfo)
        elif rtype == "weekly":
            trigger = CronTrigger(day_of_week=recurrence.get("weekday"), hour=hh, minute=mm, timezone=tzinfo)
        elif rtype == "monthly":
            trigger = CronTrigger(day=int(recurrence.get("day")), hour=hh, minute=mm, timezone=tzinfo)
        elif rtype == "yearly":
            month = int(recurrence.get("month")); day = int(recurrence.get("day"))
            trigger = CronTrigger(month=month, day=day, hour=hh, minute=mm, timezone=tzinfo)
        else:
            trigger = CronTrigger(hour=hh, minute=mm, timezone=tzinfo)

    sch.add_job(
        fire_reminder, trigger,
        id=f"rem-{rem_id}", replace_existing=True, misfire_grace_time=600, coalesce=True,
        kwargs={"chat_id": user_id, "rem_id": rem_id, "title": title, "kind": "recurring"},
        name=f"rem {rem_id}",
    )
    sch.print_jobs()

def reschedule_all():
    sch = ensure_scheduler()
    with db() as conn:
        rows = conn.execute("select * from reminders where status='scheduled'").fetchall()
    for r in rows:
        row = dict(r) if not isinstance(r, dict) else r
        if (row.get("kind") or "oneoff") == "oneoff" and row.get("when_iso"):
            schedule_oneoff(row["id"], row["user_id"], row["when_iso"], row["title"], kind="oneoff")
        else:
            rec = json.loads(row.get("recurrence_json") or "{}")
            tz = rec.get("tz") or "+03:00"
            if rec:
                schedule_recurring(row["id"], row["user_id"], row["title"], rec, tz)
    log.info("Rescheduled %d reminders from DB", len(rows))

# ---------- RU wording ----------
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

def _format_interval_phrase(unit: str, n: int) -> str:
    unit = (unit or "").lower()
    n = int(n or 1)
    if unit == "second":
        return "каждую секунду" if n == 1 else f"каждые {n} сек"
    if unit == "minute":
        return "каждую минуту" if n == 1 else f"каждые {n} мин"
    return "каждый час" if n == 1 else f"каждые {n} часов"

def format_reminder_line(row, user_tz: str) -> str:
    if not isinstance(row, dict):
        row = dict(row)
    title = row.get("title", "Напоминание")
    kind = (row.get("kind") or "oneoff").lower()
    if kind == "oneoff" and row.get("when_iso"):
        dt_local = to_user_local(row["when_iso"], user_tz)
        return f"{dt_local.strftime('%d.%m в %H:%M')} — «{title}»"
    rec = json.loads(row.get("recurrence_json") or "{}")
    rtype = (rec.get("type") or "").lower()
    time_str = rec.get("time") or "00:00"
    if rtype == "interval":
        phrase = _format_interval_phrase(rec.get("unit"), rec.get("n"))
        return f"{phrase} — «{title}»"
    if rtype == "daily":
        return f"каждый день в {time_str} — «{title}»"
    if rtype == "weekly":
        wd = ru_weekly_phrase(rec.get("weekday", ""))
        return f"{wd} в {time_str} — «{title}»"
    if rtype == "yearly":
        day = int(rec.get("day", 1)); month = int(rec.get("month", 1))
        return f"каждый год {day:02d}.{month:02d} в {time_str} — «{title}»"
    day = int(rec.get("day", 1))
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
    try:
        user_id = update.effective_user.id
        rows = db_future(user_id)
        if not rows:
            return await safe_reply(update, "Будущих напоминаний нет.", reply_markup=MAIN_MENU_KB)
        tz = db_get_user_tz(user_id) or "+03:00"
        await safe_reply(update, "🗓 Ближайшие напоминания —")
        PAD = "⠀" * 20
        for r in rows:
            try:
                line = format_reminder_line(r, tz)
            except Exception:
                log.exception("format_reminder_line failed on row=%r", r)
                title = r.get("title") if isinstance(r, dict) else (r["title"] if r else "Напоминание")
                line = f"«{title}» (некорректные данные)"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 Удалить {PAD}", callback_data=f"del:{r['id']}")]])
            await safe_reply(update, line, reply_markup=kb)
            await asyncio.sleep(0.05)
    except Exception:
        log.exception("cmd_list fatal")
        return await safe_reply(update, "Не удалось получить список. Попробуй ещё раз.", reply_markup=MAIN_MENU_KB)

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
                fire_reminder, DateTrigger(run_date=dparser.isoparse(when)),
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
    await safe_reply(update, f"⏰ Окей, напомню «{title}» {dt_local.strftime('%d.%m в %H:%M')}", reply_markup=kb)

async def cb_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    try: await q.edit_message_reply_markup(None)
    except Exception: pass
    data = q.data or ""
    if not data.startswith("answer:"): return
    choice = data.split("answer:",1)[1].strip()
    cstate = context.user_data.get("clarify_state") or {}
    base_date = cstate.get("base_date")
    title = cstate.get("title") or "Напоминание"
    user_id = q.message.chat.id
    tz = db_get_user_tz(user_id) or "+03:00"

    if base_date:
        m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?$", choice)
        if m:
            hh = int(m.group(1)); mm = int(m.group(2) or 0)
            when_local = datetime.fromisoformat(base_date).replace(hour=hh, minute=mm, tzinfo=tzinfo_from_user(tz))
            when_iso_utc = iso_utc(when_local)
            rem_id = db_add_reminder_oneoff(user_id, title, None, when_iso_utc)
            schedule_oneoff(rem_id, user_id, when_iso_utc, title, kind="oneoff")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
            await safe_reply(update, f"⏰ Окей, напомню «{title}» {when_local.strftime('%d.%m в %H:%M')}", reply_markup=kb)
            return

    context.user_data["__auto_answer"] = choice
    await handle_text(update, context)

def get_clarify_state(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.get("clarify_state")

def set_clarify_state(context: ContextTypes.DEFAULT_TYPE, state: dict | None):
    if state is None: context.user_data.pop("clarify_state", None)
    else: context.user_data["clarify_state"] = state

# ---------- VOICE ----------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        voice = update.message.voice
        if not voice:
            return await safe_reply(update, "Не смог распознать голосовое. Попробуй текстом, пожалуйста.")

        tg_file = await voice.get_file()

        with tempfile.TemporaryDirectory() as td:
            in_path = os.path.join(td, f"voice_{update.message.message_id}.oga")
            wav_path = os.path.join(td, f"voice_{update.message.message_id}.wav")

            await tg_file.download_to_drive(custom_path=in_path)

            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", in_path, "-ac", "1", "-ar", "16000", wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await proc.wait()
            if rc != 0 or not os.path.exists(wav_path):
                log.error("ffmpeg convert failed rc=%s", rc)
                return await safe_reply(update, "Не смог распознать голосовое. Попробуй текстом, пожалуйста.")

            client = get_openai()
            with open(wav_path, "rb") as f:
                try:
                    tr = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=f,
                        response_format="text",
                        language="ru",
                    )
                    text = tr if isinstance(tr, str) else getattr(tr, "text", "")
                except Exception as e:
                    log.exception("Whisper transcription error: %s", e)
                    return await safe_reply(update, "Не смог распознать голосовое. Попробуй текстом, пожалуйста.")

        text = (text or "").strip()
        if not text:
            return await safe_reply(update, "Не смог распознать голосовое. Попробуй текстом, пожалуйста.")

        # Критично: отправляем в общий текстовый обработчик
        context.user_data["__auto_answer"] = text
        return await handle_text(update, context)

    except Exception as e:
        log.exception("handle_voice failed: %s", e)
        return await safe_reply(update, "Ошибка обработки аудио")

# ---------- main text ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if await try_handle_tz_input(update, context): return
        user_id = update.effective_user.id
        incoming_text = (context.user_data.pop("__auto_answer", None)
                        or (update.message.text.strip() if update.message and update.message.text else ""))
        log.debug("handle_text: user_id=%s text=%r", user_id, incoming_text)

        if incoming_text == "📝 Список напоминаний" or incoming_text.lower() == "/list":
            return await cmd_list(update, context)
        if incoming_text == "⚙️ Настройки" or incoming_text.lower() == "/settings":
            return await safe_reply(update, "Раздел «Настройки» в разработке.", reply_markup=MAIN_MENU_KB)

        user_tz = db_get_user_tz(user_id)
        if not user_tz:
            await safe_reply(update, "Сначала укажи часовой пояс.", reply_markup=MAIN_MENU_KB)
            await safe_reply(update, "Выбери из списка:", reply_markup=build_tz_inline_kb())
            return

        now_local = now_in_user_tz(user_tz)

        # 1) быстрый парсер
        r = rule_parse(incoming_text, now_local)
        if not r and OPENAI_API_KEY:
            # 2) LLM парсер
            r = await call_llm(incoming_text, user_tz)

        if r:
            intent = (r.get("intent") or "").lower()

            if intent == "create_interval":
                title = r.get("title") or _extract_title(incoming_text)
                unit = (r.get("unit") or "minute").lower()
                n = int(r.get("n") or 1)
                start_at_local = r.get("start_at") or now_local
                if isinstance(start_at_local, str):
                    start_at_local = dparser.isoparse(start_at_local)
                recurrence = {"type":"interval","unit":unit,"n":int(n),"start_at":start_at_local.replace(microsecond=0).isoformat()}
                rem_id = db_add_reminder_recurring(user_id, title, None, recurrence, user_tz)
                schedule_recurring(rem_id, user_id, title, recurrence, user_tz)
                phrase = _format_interval_phrase(unit, n)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
                await safe_reply(update, f"⏰ Окей, буду напоминать «{title}» {phrase}", reply_markup=kb)
                return

            if intent == "create":
                title = r.get("title") or _extract_title(incoming_text)
                when_local = r.get("when_local")
                if isinstance(when_local, str):
                    when_local = dparser.isoparse(when_local)
                if when_local.tzinfo is None: when_local = when_local.replace(tzinfo=tzinfo_from_user(user_tz))
                when_iso_utc = iso_utc(when_local)
                rem_id = db_add_reminder_oneoff(user_id, title, None, when_iso_utc)
                schedule_oneoff(rem_id, user_id, when_iso_utc, title, kind="oneoff")
                dt_local = to_user_local(when_iso_utc, user_tz)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
                await safe_reply(update, f"⏰ Окей, напомню «{title}» {dt_local.strftime('%d.%m в %H:%M')}", reply_markup=kb)
                return

            if intent in ("create_recurring","recurring","repeat"):
                title = r.get("title") or _extract_title(incoming_text)
                rec = r.get("recurrence") or {}
                rem_id = db_add_reminder_recurring(user_id, title, None, rec, user_tz)
                schedule_recurring(rem_id, user_id, title, rec, user_tz)
                # человеко-фраза:
                msg = "⏰ Окей, расписание создано."
                try:
                    rtype = (rec.get("type") or "").lower()
                    if rtype == "interval":
                        msg = f"⏰ Окей, буду напоминать «{title}» {_format_interval_phrase(rec.get('unit'), rec.get('n'))}"
                    elif rtype == "weekly":
                        msg = f"⏰ Окей, буду напоминать «{title}» {ru_weekly_phrase(rec.get('weekday',''))} в {rec.get('time','00:00')}"
                    elif rtype == "daily":
                        msg = f"⏰ Окей, буду напоминать «{title}» каждый день в {rec.get('time','00:00')}"
                    elif rtype == "monthly":
                        msg = f"⏰ Окей, буду напоминать «{title}» каждое {rec.get('day')} число в {rec.get('time','00:00')}"
                    elif rtype == "yearly":
                        msg = f"⏰ Окей, буду напоминать «{title}» каждый год {int(rec.get('day')):02d}.{int(rec.get('month')):02d} в {rec.get('time','00:00')}"
                except Exception:
                    pass
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
                await safe_reply(update, msg, reply_markup=kb)
                return

            if intent == "ask":
                # сохраним состояние уточнения
                context.user_data["clarify_state"] = {
                    "base_date": r.get("base_date"),
                    "title": r.get("title") or _extract_title(incoming_text),
                }
                variants = r.get("variants") or []
                if variants:
                    buttons = [[InlineKeyboardButton(v, callback_data=f"answer:{v}") ] for v in variants]
                    await safe_reply(update, r.get("question") or "Уточни, пожалуйста:", reply_markup=InlineKeyboardMarkup(buttons))
                else:
                    await safe_reply(update, r.get("question") or "Уточни, пожалуйста.")
                return
            # ---- LLM Fallback ----
        try:
            parsed = await call_llm(incoming_text, user_tz)
            intent = (parsed.get("intent") or "").lower()
            title = parsed.get("title") or _extract_title(incoming_text)

            if intent == "create":
                when_local_iso = parsed.get("when_local")
                if not when_local_iso:
                    return await safe_reply(update, "Не смог распарсить время.", reply_markup=MAIN_MENU_KB)
                when_local = dparser.isoparse(when_local_iso)
                if when_local.tzinfo is None:
                    when_local = when_local.replace(tzinfo=tzinfo_from_user(user_tz))
                when_iso_utc = iso_utc(when_local)
                rem_id = db_add_reminder_oneoff(user_id, title, None, when_iso_utc)
                schedule_oneoff(rem_id, user_id, when_iso_utc, title, kind="oneoff")
                dt_local = to_user_local(when_iso_utc, user_tz)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
                return await safe_reply(update, f"⏰ Окей, напомню «{title}» {dt_local.strftime('%d.%m в %H:%M')}", reply_markup=kb)

            if intent == "create_interval":
                unit = (parsed.get("unit") or "minute").lower()
                n = int(parsed.get("n") or 1)
                start_at_local = dparser.isoparse(parsed.get("start_at")) if parsed.get("start_at") else now_local
                recurrence = {"type":"interval","unit":unit,"n":n,"start_at":start_at_local.replace(microsecond=0).isoformat()}
                rem_id = db_add_reminder_recurring(user_id, title, None, recurrence, user_tz)
                schedule_recurring(rem_id, user_id, title, recurrence, user_tz)
                phrase = _format_interval_phrase(unit, n)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
                return await safe_reply(update, f"⏰ Окей, буду напоминать «{title}» {phrase}", reply_markup=kb)

            if intent == "create_recurring":
                rec = parsed.get("recurrence") or {}
                # ожидаем type ∈ {daily, weekly, monthly, yearly} и поля time / weekday / day / month
                rem_id = db_add_reminder_recurring(user_id, title, None, rec, user_tz)
                schedule_recurring(rem_id, user_id, title, rec, user_tz)
                # короткая фраза подтверждения:
                rtype = (rec.get("type") or "").lower()
                if rtype == "daily":
                    msg = f"каждый день в {rec.get('time','00:00')}"
                elif rtype == "weekly":
                    msg = f"{ru_weekly_phrase(rec.get('weekday',''))} в {rec.get('time','00:00')}"
                elif rtype == "monthly":
                    msg = f"каждое {rec.get('day')} число в {rec.get('time','00:00')}"
                elif rtype == "yearly":
                    msg = f"каждый год {int(rec.get('day',1)):02d}.{int(rec.get('month',1)):02d} в {rec.get('time','00:00')}"
                else:
                    msg = "по расписанию"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"del:{rem_id}")]])
                return await safe_reply(update, f"⏰ Окей, буду напоминать «{title}» {msg}", reply_markup=kb)

            if intent == "ask":
                # бот просит уточнения (например, 11 → 11:00 или 23:00)
                set_clarify_state(context, {
                    "base_date": parsed.get("base_date"),
                    "title": title
                })
                variants = parsed.get("variants") or []
                if variants:
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton(v, callback_data=f"answer:{v}")] for v in variants])
                    return await safe_reply(update, parsed.get("question") or "Уточни, пожалуйста, время", reply_markup=kb)
                return await safe_reply(update, parsed.get("question") or "Уточни, пожалуйста, время")

        except Exception:
            log.exception("LLM fallback failed")

        return await safe_reply(update, "Я не понял, попробуй ещё раз.", reply_markup=MAIN_MENU_KB)

    except Exception:
        log.exception("handle_text fatal")
        await safe_reply(update, "Упс, что-то пошло не так. Напиши ещё раз, пожалуйста.")

# ---------- Error handler ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error in PTB")
    try:
        if isinstance(update, Update):
            await safe_reply(update, "Случилась ошибка. Попробуй ещё раз 🙏")
    except Exception:
        pass

# ---------- Startup ----------
async def on_startup(app: Application):
    global scheduler, TG_BOT
    TG_BOT = app.bot
    loop = asyncio.get_running_loop()

    jobstores = None
    if DB_DIALECT == "postgres" and DATABASE_URL:
        jobstore_url, _, _ = _url_with_ipv4_host(
            DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
        )
        jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url)}

    scheduler = AsyncIOScheduler(
        timezone=timezone.utc,
        event_loop=loop,
        jobstores=jobstores,
        job_defaults={"coalesce": True, "misfire_grace_time": 600}
    )
    scheduler.start()
    log.info("APScheduler started in PTB event loop")
    reschedule_all()

# ---------- DB INIT ----------
def db_init():
    with db() as conn:
        if DB_DIALECT == "postgres":
            conn.execute("""
                create table if not exists users (
                  user_id bigint primary key,
                  tz text
                )
            """)
            conn.execute("""
                create table if not exists reminders (
                  id bigserial primary key,
                  user_id bigint not null,
                  title text not null,
                  body text,
                  when_iso text,
                  status text default 'scheduled',
                  kind text default 'oneoff',
                  recurrence_json text
                )
            """)
            conn.execute("create index if not exists reminders_user_idx on reminders(user_id)")
            conn.execute("create index if not exists reminders_status_idx on reminders(status)")
        else:
            import sqlite3
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
            # защита от повторных миграций
            try: conn.execute("alter table reminders add column kind text default 'oneoff'")
            except Exception: pass
            try: conn.execute("alter table reminders add column recurrence_json text")
            except Exception: pass
            conn.commit()

# ---------- MAIN ----------
def main():
    log.info("Starting PlannerBot...")
    db_init()

    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(on_startup)
           .build())

    app.add_error_handler(on_error)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("settings", lambda u,c: u.message.reply_text(
        "Раздел «Настройки» в разработке.", reply_markup=MAIN_MENU_KB)))
    app.add_handler(CallbackQueryHandler(cb_tz, pattern=r"^tz:"))
    app.add_handler(CallbackQueryHandler(cb_inline, pattern=r"^(del:|done:|snooze:)"))
    app.add_handler(CallbackQueryHandler(cb_pick, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(cb_answer, pattern=r"^answer:"))

    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
