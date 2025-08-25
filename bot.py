import os
import io
import re
import json
import yaml
import logging
import secrets
import sqlite3
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from calendar import monthrange

from pydantic import BaseModel, Field, ValidationError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import UpdateType
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from openai import OpenAI

# =====================
# Logging & env
# =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def _extract_token(raw: str | None) -> str:
    if not raw:
        return ""
    raw = raw.strip().replace("\u200b", "").replace("\u200c", "").replace("\uFEFF", "")
    raw = raw.strip(" '\"")
    m = re.search(r"[0-9]+:[A-Za-z0-9_-]{30,}", raw)
    return m.group(0) if m else raw

def _mask(s: str | None) -> str:
    if not s: return ""
    s = s.strip()
    return s[:6] + "..." + s[-4:] if len(s) > 12 else "***"

RAW_TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
RAW_BOT_TOKEN = os.getenv("BOT_TOKEN")
TOKEN = _extract_token(RAW_TELEGRAM_TOKEN) or _extract_token(RAW_BOT_TOKEN)

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
PROMPTS_PATH = os.getenv("PROMPTS_PATH", "prompts.yaml")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
TRANSCRIBE_MODEL = os.getenv("ASR_MODEL", "whisper-1")

DEFAULT_TZ = os.getenv("DEFAULT_TZ", "+03:00")
DB_PATH = os.getenv("DB_PATH", "reminders.db")
LIST_PAGE_SIZE = int(os.getenv("LIST_PAGE_SIZE", "8"))

def _valid_token(t: str) -> bool:
    return bool(re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}", t))

logging.info("Env debug: TELEGRAM_TOKEN=%s BOT_TOKEN=%s | picked=%s",
             _mask(RAW_TELEGRAM_TOKEN), _mask(RAW_BOT_TOKEN), _mask(TOKEN))

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN / BOT_TOKEN not set (empty)")
if not _valid_token(TOKEN):
    raise RuntimeError(f"TELEGRAM_TOKEN invalid format → {TOKEN!r} (must be 123456789:AAAA...)")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

client = OpenAI(api_key=OPENAI_API_KEY)

# =====================
# Time helpers
# =====================
def tz_from_offset(off: str) -> timezone:
    off = off.strip()
    if re.fullmatch(r"[+-]\d{1,2}$", off):
        sign = off[0]; hh = int(off[1:])
        off = f"{sign}{hh:02d}:00"
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})?", off)
    if not m:
        return timezone.utc
    sign, hh, mm = m.group(1), m.group(2), m.group(3) or "00"
    delta = timedelta(hours=int(hh), minutes=int(mm))
    if sign == "-":
        delta = -delta
    return timezone(delta)

def now_iso_for_tz(tz_str: str) -> str:
    tz = tz_from_offset(tz_str)
    return datetime.now(tz).replace(microsecond=0).isoformat()

def fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m в %H:%M")
    except Exception:
        return iso

def bump_to_future(iso_when: str) -> str:
    try:
        when = datetime.fromisoformat(iso_when)
        now = datetime.now(when.tzinfo)
        if when <= now:
            when = now + timedelta(seconds=5)
        return when.replace(microsecond=0).isoformat()
    except Exception:
        return iso_when

# =====================
# DB (SQLite)
# =====================
class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            note TEXT,
            tz TEXT NOT NULL,
            due_at TEXT,
            rrule TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','done','canceled')),
            last_msg_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            origin TEXT
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reminders_chat_due ON reminders(chat_id, due_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reminders_status_due ON reminders(status, due_at);")
        self.conn.commit()

    def add(self, chat_id: int, title: str, tz: str, due_at: str, rrule: Optional[str] = None, origin: Optional[str] = None) -> str:
        rid = secrets.token_urlsafe(8)
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        cur = self.conn.cursor()
        cur.execute("""
          INSERT INTO reminders (id, chat_id, title, tz, due_at, rrule, status, created_at, updated_at, origin)
          VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?);
        """, (rid, chat_id, title, tz, due_at, rrule, now, now, origin))
        self.conn.commit()
        return rid

    def set_last_msg_id(self, rid: str, msg_id: int):
        cur = self.conn.cursor()
        cur.execute("UPDATE reminders SET last_msg_id=?, updated_at=? WHERE id=?;",
                    (msg_id, datetime.utcnow().replace(microsecond=0).isoformat()+"Z", rid))
        self.conn.commit()

    def get(self, rid: str) -> Optional[sqlite3.Row]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM reminders WHERE id=?;", (rid,))
        return cur.fetchone()

    def update_due(self, rid: str, new_iso: str):
        cur = self.conn.cursor()
        cur.execute("UPDATE reminders SET due_at=?, updated_at=? WHERE id=?;",
                    (new_iso, datetime.utcnow().replace(microsecond=0).isoformat()+"Z", rid))
        self.conn.commit()

    def set_status(self, rid: str, status: str):
        cur = self.conn.cursor()
        cur.execute("UPDATE reminders SET status=?, updated_at=? WHERE id=?;",
                    (status, datetime.utcnow().replace(microsecond=0).isoformat()+"Z", rid))
        self.conn.commit()

    def upcoming(self, chat_id: int, now_iso: str, limit: int, offset: int) -> List[sqlite3.Row]:
        cur = self.conn.cursor()
        cur.execute("""
          SELECT * FROM reminders
          WHERE chat_id=? AND status='active' AND (due_at IS NOT NULL AND due_at >= ?)
          ORDER BY due_at ASC
          LIMIT ? OFFSET ?;
        """, (chat_id, now_iso, limit, offset))
        return cur.fetchall()

    def count_upcoming(self, chat_id: int, now_iso: str) -> int:
        cur = self.conn.cursor()
        cur.execute("""
          SELECT COUNT(*) AS c FROM reminders
          WHERE chat_id=? AND status='active' AND (due_at IS NOT NULL AND due_at >= ?);
        """, (chat_id, now_iso))
        return int(cur.fetchone()["c"])

    def active_to_schedule(self) -> List[sqlite3.Row]:
        cur = self.conn.cursor()
        cur.execute("""
          SELECT * FROM reminders
          WHERE status='active' AND due_at IS NOT NULL;
        """)
        return cur.fetchall()

db = DB(DB_PATH)

# =====================
# Prompts
# =====================
class PromptPack(BaseModel):
    system: str
    fewshot: List[dict] = []

def load_prompts() -> PromptPack:
    with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if "system" in raw:
        return PromptPack(system=raw["system"], fewshot=raw.get("fewshot", []))
    if "parse" in raw and isinstance(raw["parse"], dict):
        sys_txt = raw["parse"].get("system") or raw["parse"].get("instruction")
        shots = raw["parse"].get("fewshot") or raw.get("examples") or []
        if sys_txt:
            return PromptPack(system=sys_txt, fewshot=shots)
    raise ValueError("prompts.yaml должен содержать ключи 'system' и (опционально) 'fewshot'.")

try:
    PROMPTS = load_prompts()
    logging.info("Prompts loaded: system=%s... | fewshot=%d",
                 (PROMPTS.system or "")[:40].replace("\n", " "),
                 len(PROMPTS.fewshot))
except Exception as e:
    logging.exception("Failed to load prompts.yaml: %s", e)
    class _PP(BaseModel):
        system: str
        fewshot: list = []
    PROMPTS = _PP(system="Fallback system prompt", fewshot=[])

# =====================
# LLM schema
# =====================
class ReminderOption(BaseModel):
    iso_datetime: str
    label: str

class LLMResult(BaseModel):
    intent: str = Field(description="'create_reminder' | 'ask_clarification' | 'chat'")
    text_original: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    timezone: Optional[str] = None
    fixed_datetime: Optional[str] = None
    need_confirmation: bool = False
    options: List[ReminderOption] = []

# =====================
# OpenAI
# =====================
async def transcribe_voice(file_bytes: bytes, filename: str = "audio.ogg") -> str:
    f = io.BytesIO(file_bytes)
    f.name = filename if filename.endswith(".ogg") else (filename + ".ogg")
    resp = client.audio.transcriptions.create(
        model=TRANSCRIBE_MODEL,
        file=f,
        response_format="text",
        temperature=0,
        language="ru"
    )
    return resp

import time
async def call_llm(text: str, user_tz: str) -> LLMResult:
    now = now_iso_for_tz(user_tz)
    messages = [
        {"role": "system", "content": f"NOW_ISO={now}  TZ_DEFAULT={user_tz}"},
        {"role": "system", "content": PROMPTS.system},
        *PROMPTS.fewshot,
        {"role": "user", "content": text}
    ]
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.2,
                response_format={"type": "json_object"},
                timeout=20
            )
            break
        except Exception as e:
            logging.warning("LLM call failed (attempt %d): %s", attempt+1, e)
            if attempt == 2:
                # возврат безопасного заготовка
                return LLMResult(intent="ask_clarification", need_confirmation=True, options=[])
            time.sleep(0.7 * (attempt+1))
    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
        return LLMResult(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        logging.exception("LLM JSON parse failed: %s\nRaw: %s", e, raw)
        return LLMResult(intent="ask_clarification", need_confirmation=True, options=[])

# =====================
# Local relative-time parser (one-shot)
# =====================
REL_MIN  = re.compile(r"через\s+(?:минуту|1\s*мин(?:\.|ут)?)\b", re.I)
REL_NSEC = re.compile(r"через\s+(\d+)\s*сек(?:унд|унды|ун|)?\b", re.I)
REL_NMIN = re.compile(r"через\s+(\d+)\s*мин(?:ут|ы)?\b", re.I)
REL_HALF = re.compile(r"через\s+полчаса\b", re.I)
REL_NH   = re.compile(r"через\s+(\d+)\s*час(?:а|ов)?\b", re.I)
REL_ND   = re.compile(r"через\s+(\d+)\s*д(ень|ня|ней)?\b", re.I)
REL_WEEK = re.compile(r"через\s+недел(?:ю|ю|юто)?\b", re.I)

def try_parse_relative_local(text: str, user_tz: str) -> Optional[str]:
    tz = tz_from_offset(user_tz)
    now = datetime.now(tz).replace(microsecond=0)

    m = REL_NSEC.search(text)
    if m:
        return (now + timedelta(seconds=int(m.group(1)))).isoformat()

    m = REL_NMIN.search(text)
    if m:
        return (now + timedelta(minutes=int(m.group(1)))).isoformat()

    if REL_HALF.search(text):
        return (now + timedelta(minutes=30)).isoformat()

    m = REL_NH.search(text)
    if m:
        return (now + timedelta(hours=int(m.group(1)))).isoformat()

    m = REL_ND.search(text)
    if m:
        return (now + timedelta(days=int(m.group(1)))).isoformat()

    if REL_WEEK.search(text):
        return (now + timedelta(days=7)).isoformat()

    if REL_MIN.search(text):
        return (now + timedelta(minutes=1)).isoformat()

    return None

# =====================
# Recurrence parsing (daily/weekly/monthly)
# =====================
EVERY_DAY_RX   = re.compile(r"\b(каждый\s*день|ежедневно)\b", re.I)
WEEKLY_RX      = re.compile(r"\b(раз\s+в\s+неделю|еженедел(?:ьно|ьник)?)\b", re.I)
WEEKDAY_RX     = re.compile(r"\b(понедельник|вторник|среда|среду|четверг|пятница|пятницу|суббота|субботу|воскресенье)\b", re.I)
MONTHDAY_RX    = re.compile(r"\bкажд(ый|ое|ого)\s+(\d{1,2})\s*(?:числа|число)\b", re.I)
AT_HH_RX       = re.compile(r"\bв\s+(\d{1,2})(?::(\d{2}))?\s*(?:час(а|ов)?)?\b", re.I)

_WEEKDAY_MAP = {
    "понедельник":"MO","вторник":"TU","среда":"WE","среду":"WE","четверг":"TH",
    "пятница":"FR","пятницу":"FR","суббота":"SA","субботу":"SA","воскресенье":"SU"
}

_DOW = {"MO":0,"TU":1,"WE":2,"TH":3,"FR":4,"SA":5,"SU":6}

def parse_rrule(rrule: str) -> dict:
    parts = {}
    for chunk in (rrule or "").split(";"):
        if not chunk: continue
        k, _, v = chunk.partition("=")
        parts[k.upper()] = v
    return parts

def next_due_from_rrule(base: datetime, rrule: str) -> Optional[str]:
    p = parse_rrule(rrule)
    freq = p.get("FREQ", "").upper()
    hh = int(p.get("BYHOUR", "9"))
    mm = int(p.get("BYMINUTE", "0"))
    tz = base.tzinfo

    def at_time(d: datetime) -> datetime:
        return d.replace(hour=hh, minute=mm, second=0, microsecond=0)

    now = base
    if freq == "DAILY":
        cand = at_time(now)
        if cand <= now:
            cand = at_time(now + timedelta(days=1))
        return cand.isoformat()

    if freq == "WEEKLY":
        byday = [d for d in p.get("BYDAY","").split(",") if d]
        if not byday:
            byday = [list(_DOW.keys())[now.weekday()]]
        best = None
        for code in byday:
            wd = _DOW.get(code.upper(), now.weekday())
            delta = (wd - now.weekday()) % 7
            cand = at_time(now + timedelta(days=delta))
            if cand <= now:
                cand = at_time(cand + timedelta(days=7))
            if best is None or cand < best:
                best = cand
        return best.isoformat()

    if freq == "MONTHLY":
        md = int(p.get("BYMONTHDAY", "1"))
        y, m = now.year, now.month
        try:
            cand = at_time(datetime(y, m, md, tzinfo=tz))
        except ValueError:
            md = monthrange(y, m)[1]
            cand = at_time(datetime(y, m, md, tzinfo=tz))
        if cand <= now:
            m2 = m + 1
            y2 = y + (1 if m2 == 13 else 0)
            m2 = 1 if m2 == 13 else m2
            md2 = int(p.get("BYMONTHDAY", "1"))
            last = monthrange(y2, m2)[1]
            md2 = min(md2, last)
            cand = at_time(datetime(y2, m2, md2, tzinfo=tz))
        return cand.isoformat()
    return None

def kb_pick_time() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("09:00", callback_data="pt|09:00"),
         InlineKeyboardButton("12:00", callback_data="pt|12:00"),
         InlineKeyboardButton("18:00", callback_data="pt|18:00"),
         InlineKeyboardButton("21:00", callback_data="pt|21:00")],
        [InlineKeyboardButton("Другое время", callback_data="pt|other")]
    ])

def kb_pick_weekday() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пн", callback_data="pwd|MO"),
         InlineKeyboardButton("Вт", callback_data="pwd|TU"),
         InlineKeyboardButton("Ср", callback_data="pwd|WE"),
         InlineKeyboardButton("Чт", callback_data="pwd|TH")],
        [InlineKeyboardButton("Пт", callback_data="pwd|FR"),
         InlineKeyboardButton("Сб", callback_data="pwd|SA"),
         InlineKeyboardButton("Вс", callback_data="pwd|SU")]
    ])

def try_parse_recurrence(text: str, user_tz: str):
    """
    Возвращает dict:
      {"mode":"daily|weekly|monthly","rrule":"...","need":{"time":bool,"weekday":bool},"title":"..."}
    или None, если это не периодическая команда.
    """
    title = extract_title_fallback(text)

    tm = AT_HH_RX.search(text)
    hh = mm = None
    if tm:
        hh = int(tm.group(1)); mm = int(tm.group(2) or 0)

    if EVERY_DAY_RX.search(text):
        need_time = (hh is None)
        rrule = "FREQ=DAILY" + (f";BYHOUR={hh};BYMINUTE={mm}" if hh is not None else "")
        return {"mode":"daily","rrule":rrule,"need":{"time":need_time,"weekday":False},"title":title}

    if WEEKLY_RX.search(text) or WEEKDAY_RX.search(text):
        mwd = WEEKDAY_RX.search(text)
        byday = _WEEKDAY_MAP[mwd.group(0).lower()] if mwd else None
        need_weekday = byday is None
        need_time = (hh is None)
        base = "FREQ=WEEKLY"
        if byday: base += f";BYDAY={byday}"
        if hh is not None: base += f";BYHOUR={hh};BYMINUTE={mm}"
        return {"mode":"weekly","rrule":base,"need":{"time":need_time,"weekday":need_weekday},"title":title}

    mmday = MONTHDAY_RX.search(text)
    if mmday:
        day = int(mmday.group(2))
        need_time = (hh is None)
        base = f"FREQ=MONTHLY;BYMONTHDAY={day}"
        if hh is not None: base += f";BYHOUR={hh};BYMINUTE={mm}"
        return {"mode":"monthly","rrule":base,"need":{"time":need_time,"weekday":False},"title":title}

    return None

# для fallback заголовка
RX_JUNK = [
    re.compile(r"\b(сегодня|завтра|послезавтра)\b", re.I),
    re.compile(r"\b(утра|утром|вечером|днём|ночи|ночью)\b", re.I),
    re.compile(r"\b(в|во)\s+\d{1,2}(:\d{2})?\b", re.I),
    re.compile(r"\bчерез\s+\d+\s*(минут|мин|час(а|ов)?|д(ень|ня|ней)?)\b", re.I),
    re.compile(r"\bчерез\s+полчаса\b", re.I),
    re.compile(r"\bв\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b", re.I),
    re.compile(r"[.,:;–—-]\s*$"),
]

def extract_title_fallback(text: str) -> str:
    t = text
    t = re.sub(r"\b(напомни(ть)?|пожалуйста)\b", "", t, flags=re.I)
    for rx in RX_JUNK:
        t = rx.sub("", t)
    for rx in (REL_MIN, REL_NSEC, REL_NMIN, REL_HALF, REL_NH, REL_ND, REL_WEEK):
        t = rx.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,.:;–—-")
    return t or "Напоминание"

def _clean_title_for_relative(text: str) -> str:
    t = extract_title_fallback(text)
    return t or "Напоминание"

# =====================
# UI
# =====================
MENU_BTN_LIST = "📝 Список напоминаний"
MENU_BTN_SETTINGS = "⚙️ Настройки"

def fire_kb(reminder_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Через 10 мин", callback_data=f"snz|10m|{reminder_id}"),
            InlineKeyboardButton("Через 30 мин", callback_data=f"snz|30m|{reminder_id}"),
            InlineKeyboardButton("Через 1 час", callback_data=f"snz|1h|{reminder_id}")
        ],
        [InlineKeyboardButton("✅", callback_data=f"done|{reminder_id}")]
    ])

def rrule_to_human(rrule: str) -> str:
    if not rrule: return ""
    p = parse_rrule(rrule)
    freq = p.get("FREQ","")
    hh = p.get("BYHOUR"); mm = p.get("BYMINUTE")
    tm = f" в {int(hh):02d}:{int(mm or 0):02d}" if hh else ""
    if freq=="DAILY":
        return f"каждый день{tm}"
    if freq=="WEEKLY":
        d = p.get("BYDAY")
        rus = {"MO":"по понедельникам","TU":"по вторникам","WE":"по средам","TH":"по четвергам","FR":"по пятницам","SA":"по субботам","SU":"по воскресеньям"}
        return f"{rus.get(d,'еженедельно')}{tm}"
    if freq=="MONTHLY":
        day = p.get("BYMONTHDAY","1")
        return f"каждое {day} число{tm}"
    return rrule

# ---- Список как набор «строка-кнопка» ----
def list_keyboard(items: List[sqlite3.Row], page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    for r in items:
        rid = r["id"]
        prefix = rrule_to_human(r["rrule"]) if r["rrule"] else fmt_dt(r["due_at"])
        label = f"🗑 {prefix} — {r['title']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ldel|{rid}|p{page}")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("← Назад", callback_data=f"lp|{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Вперёд →", callback_data=f"lp|{page+1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows) if rows else None

def render_list_text(items: List[sqlite3.Row], page: int, total_pages: int) -> str:
    if not items:
        return "Будущих напоминаний нет."
    return f"📋 Ближайшие напоминания — страница {page}/{total_pages}.\nНажми на строку, чтобы удалить."

# =====================
# Scheduling
# =====================
def cancel_job_if_exists(app: Application, rid: str):
    jobs = app.bot_data.setdefault("jobs", {})
    job = jobs.pop(rid, None)
    if job:
        try:
            job.schedule_removal()
        except Exception:
            pass

def schedule_job_for(app: Application, row: sqlite3.Row):
    rid = row["id"]
    due_iso = row["due_at"]
    if not due_iso:
        return
    try:
        due_dt = datetime.fromisoformat(due_iso)
        now = datetime.now(due_dt.tzinfo)
        when = due_dt if due_dt > now else now + timedelta(seconds=2)
    except Exception:
        return

    async def _fire(ctx: ContextTypes.DEFAULT_TYPE):
        ctx.application.bot_data.setdefault("jobs", {}).pop(rid, None)
        sent = await ctx.bot.send_message(
            chat_id=row["chat_id"],
            text=f"🔔 «{row['title']}»",
            reply_markup=fire_kb(rid)
        )
        db.set_last_msg_id(rid, sent.message_id)
        # пересчитать следующее срабатывание, если периодическое
        try:
            fresh = db.get(rid)
            if fresh and fresh["status"] == "active" and fresh["rrule"]:
                tz = tz_from_offset(fresh["tz"])
                base = datetime.now(tz)
                next_iso = next_due_from_rrule(base, fresh["rrule"])
                if next_iso:
                    db.update_due(rid, next_iso)
                    schedule_job_for(ctx.application, db.get(rid))
        except Exception as e:
            logging.exception("RRULE reschedule failed for %s: %s", rid, e)

    job = app.job_queue.run_once(_fire, when=when)
    app.bot_data.setdefault("jobs", {})[rid] = job
    logging.info("Scheduled job for %s at %s", rid, when.isoformat())

def schedule_all_on_start(app: Application):
    rows = db.active_to_schedule()
    for r in rows:
        try:
            tz = tz_from_offset(r["tz"])
            if r["rrule"]:
                nxt = next_due_from_rrule(datetime.now(tz), r["rrule"])
                if nxt:
                    db.update_due(r["id"], nxt)
                    r = dict(r); r["due_at"] = nxt
            else:
                due = datetime.fromisoformat(r["due_at"])
                now = datetime.now(due.tzinfo)
                if due <= now - timedelta(minutes=10):
                    new_iso = (now + timedelta(seconds=2)).replace(microsecond=0).isoformat()
                    db.update_due(r["id"], new_iso)
                    r = dict(r); r["due_at"] = new_iso
        except Exception:
            pass
        schedule_job_for(app, r)

# =====================
# TZ selection + Reply menu
# =====================
TZ_OPTIONS = [
    ("Калининград (+2)", "+02:00"),
    ("Москва (+3)", "+03:00"),
    ("Самара (+4)", "+04:00"),
    ("Екатеринбург (+5)", "+05:00"),
    ("Омск (+6)", "+06:00"),
    ("Новосибирск (+7)", "+07:00"),
    ("Иркутск (+8)", "+08:00"),
    ("Якутск (+9)", "+09:00"),
    ("Хабаровск (+10)", "+10:00"),
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz_buttons = [[InlineKeyboardButton(label, callback_data=f"tz|{offset}")]
                  for label, offset in TZ_OPTIONS]
    tz_buttons.append([InlineKeyboardButton("Другой", callback_data="tz|other")])
    tz_kb = InlineKeyboardMarkup(tz_buttons)
    await update.message.reply_text(
        "Для начала укажи свой часовой пояс.\n"
        "Выбери из списка или нажми «Другой», чтобы ввести вручную.\n\n"
        "Пример: +11 или -4:30",
        reply_markup=tz_kb
    )
    reply_kb = ReplyKeyboardMarkup(
        [[MENU_BTN_LIST, MENU_BTN_SETTINGS]],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await update.message.reply_text(
        "Кнопки меню снизу активированы. Можешь нажать или просто написать задачу 👇",
        reply_markup=reply_kb
    )

async def handle_tz_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "tz|other":
        context.user_data["tz_waiting"] = True
        await query.edit_message_text(
            "Введите свой часовой пояс от UTC в цифрах.\n"
            "Например: +3, +03:00 или -4:30"
        )
        return
    _, offset = data.split("|", 1)
    context.user_data["tz"] = offset
    await query.edit_message_text(f"Часовой пояс установлен: UTC{offset}\nТеперь напиши что и когда напомнить.")

async def handle_tz_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("tz_waiting"):
        return
    tz = update.message.text.strip()
    if re.fullmatch(r"[+-]\d{1,2}(:\d{2})?", tz):
        if re.fullmatch(r"[+-]\d{1,2}$", tz):
            sign = tz[0]; hh = int(tz[1:]); tz = f"{sign}{hh:02d}:00"
        context.user_data["tz"] = tz
        context.user_data["tz_waiting"] = False
        await update.message.reply_text(f"Часовой пояс установлен: UTC{tz}\nТеперь напиши что и когда напомнить.")
    else:
        await update.message.reply_text("Неверный формат. Введите, например: +3, +03:00 или -4:30")

# =====================
# Reply-menu buttons handler
# =====================
MENU_OR_TEXT = re.compile(r".*", re.S)

async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # ожидание ручного ввода времени для периодических
    if context.user_data.get("time_waiting"):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
        if not m:
            await update.message.reply_text("Формат времени HH:MM, например 09:00")
            return
        hh, mm = int(m.group(1)), int(m.group(2))
        parts = parse_rrule(context.user_data.get("pending_rrule","FREQ=DAILY"))
        parts["BYHOUR"]=str(hh); parts["BYMINUTE"]=str(mm)
        rrule = ";".join([f"{k}={v}" for k,v in parts.items()])
        context.user_data["pending_rrule"] = rrule
        context.user_data["time_waiting"] = False
        tz_str = context.user_data.get("pending_tz", DEFAULT_TZ)
        tz = tz_from_offset(tz_str)
        title = context.user_data.pop("pending_title","Напоминание")
        due_iso = next_due_from_rrule(datetime.now(tz), rrule)
        rid = db.add(update.effective_chat.id, title, tz_str, due_iso, rrule=rrule)
        await update.message.reply_text(_ack_periodic(title, rrule, due_iso))
        schedule_job_for(context.application, db.get(rid))
        return

    if text == MENU_BTN_LIST:
        await cmd_list(update, context)
        return
    if text == MENU_BTN_SETTINGS:
        await update.message.reply_text("Раздел «Настройки» в разработке.")
        return

# =====================
# Core
# =====================
async def reload_prompts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PROMPTS
    try:
        PROMPTS = load_prompts()
        await update.message.reply_text("Промты перезагружены ✅")
        logging.info("Prompts reloaded: system=%s... | fewshot=%d",
                     (PROMPTS.system or "")[:40].replace("\n", " "), len(PROMPTS.fewshot))
    except Exception as e:
        logging.exception("/reload error")
        await update.message.reply_text(f"Ошибка перезагрузки: {e}")

def _ack_text(title: str, iso: str) -> str:
    return f"📅 Окей, напомню «{title}» {fmt_dt(iso)}"

def _ack_periodic(title: str, rrule: str, next_iso: str) -> str:
    return f"📅 Окей, «{title}» — {rrule_to_human(rrule)}. Ближайшее: {fmt_dt(next_iso)}"

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tz = context.user_data.get("tz", DEFAULT_TZ)
    text = update.message.text.strip()

    # PERIODIC first
    rec = try_parse_recurrence(text, user_tz)
    if rec:
        context.user_data["pending_rrule"] = rec["rrule"]
        context.user_data["pending_title"] = rec["title"]
        context.user_data["pending_tz"] = user_tz
        if rec["need"]["weekday"]:
            await update.message.reply_text("Выбери день недели:", reply_markup=kb_pick_weekday())
            return
        if rec["need"]["time"]:
            await update.message.reply_text("Во сколько напоминать?", reply_markup=kb_pick_time())
            return
        tz = tz_from_offset(user_tz)
        due_iso = next_due_from_rrule(datetime.now(tz), rec["rrule"])
        rid = db.add(update.effective_chat.id, rec["title"], user_tz, due_iso, rrule=rec["rrule"], origin=None)
        await update.message.reply_text(_ack_periodic(rec["title"], rec["rrule"], due_iso))
        schedule_job_for(context.application, db.get(rid))
        return

    # one-shot relative shortcuts
    iso = try_parse_relative_local(text, user_tz)
    if iso:
        title = _clean_title_for_relative(text)
        iso = bump_to_future(iso)
        rid = db.add(update.effective_chat.id, title, user_tz, iso, origin=None)
        await update.message.reply_text(_ack_text(title, iso))
        schedule_job_for(context.application, db.get(rid))
        return

    # LLM flow
    result = await call_llm(text, user_tz)
    if result.intent == "create_reminder" and result.fixed_datetime:
        iso = bump_to_future(result.fixed_datetime)
        raw_title = (result.title or result.text_original or "").strip()
        title = raw_title if (raw_title and not raw_title.lower().startswith("напоминан")) else extract_title_fallback(text)
        rid = db.add(update.effective_chat.id, title, user_tz, iso, origin=json.dumps(result.model_dump()))
        await update.message.reply_text(_ack_text(title, iso))
        schedule_job_for(context.application, db.get(rid))
    elif result.intent == "ask_clarification" and result.options:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(opt.label, callback_data=f"pick|{opt.iso_datetime}")]
            for opt in result.options
        ])
        # сохраним контекст на случай выбора
        context.user_data["pending_title"] = extract_title_fallback(text)
        await update.message.reply_text("Уточни:", reply_markup=kb)
    else:
        await update.message.reply_text("Не понял. Скажи, например: «завтра в 15 позвонить маме» или «каждый день пить таблетки в 09:00».")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tz = context.user_data.get("tz", DEFAULT_TZ)
    file = await update.message.voice.get_file()
    file_bytes = await file.download_as_bytearray()
    text = await transcribe_voice(file_bytes, filename="telegram_voice.ogg")

    # PERIODIC first
    rec = try_parse_recurrence(text, user_tz)
    if rec:
        context.user_data["pending_rrule"] = rec["rrule"]
        context.user_data["pending_title"] = rec["title"]
        context.user_data["pending_tz"] = user_tz
        if rec["need"]["weekday"]:
            await update.message.reply_text("Выбери день недели:", reply_markup=kb_pick_weekday())
            return
        if rec["need"]["time"]:
            await update.message.reply_text("Во сколько напоминать?", reply_markup=kb_pick_time())
            return
        tz = tz_from_offset(user_tz)
        due_iso = next_due_from_rrule(datetime.now(tz), rec["rrule"])
        rid = db.add(update.effective_chat.id, rec["title"], user_tz, due_iso, rrule=rec["rrule"], origin=None)
        await update.message.reply_text(_ack_periodic(rec["title"], rec["rrule"], due_iso))
        schedule_job_for(context.application, db.get(rid))
        return

    # one-shot relative
    iso = try_parse_relative_local(text, user_tz)
    if iso:
        title = _clean_title_for_relative(text)
        iso = bump_to_future(iso)
        rid = db.add(update.effective_chat.id, title, user_tz, iso, origin=None)
        await update.message.reply_text(_ack_text(title, iso))
        schedule_job_for(context.application, db.get(rid))
        return

    # LLM
    result = await call_llm(text, user_tz)
    if result.intent == "create_reminder" and result.fixed_datetime:
        iso = bump_to_future(result.fixed_datetime)
        raw_title = (result.title or result.text_original or "").strip()
        title = raw_title if (raw_title and not raw_title.lower().startswith("напоминан")) else extract_title_fallback(text)
        rid = db.add(update.effective_chat.id, title, user_tz, iso, origin=json.dumps(result.model_dump()))
        await update.message.reply_text(_ack_text(title, iso))
        schedule_job_for(context.application, db.get(rid))
    elif result.intent == "ask_clarification" and result.options:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(opt.label, callback_data=f"pick|{opt.iso_datetime}")]
            for opt in result.options
        ])
        context.user_data["pending_title"] = extract_title_fallback(text)
        await update.message.reply_text("Уточни:", reply_markup=kb)
    else:
        await update.message.reply_text("Не понял. Скажи, например: «завтра в 15 позвонить маме» или «каждую субботу баня в 19:00».")

# =====================
# List / Pagination
# =====================
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_list_page(update, context, page=1)

async def render_list_page(update_or_query, context: ContextTypes.DEFAULT_TYPE, page: int):
    chat_id = update_or_query.effective_chat.id
    user_tz = context.user_data.get("tz", DEFAULT_TZ)
    now = now_iso_for_tz(user_tz)
    total = db.count_upcoming(chat_id, now)
    total_pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * LIST_PAGE_SIZE
    items = db.upcoming(chat_id, now, LIST_PAGE_SIZE, offset)
    text = render_list_text(items, page, total_pages)
    kb = list_keyboard(items, page, total_pages)
    if isinstance(update_or_query, Update) and update_or_query.message:
        await update_or_query.message.reply_text(text, reply_markup=kb)
    else:
        q = update_or_query.callback_query
        await q.edit_message_text(text, reply_markup=kb)

# =====================
# Callbacks
# =====================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    try:
        await query.answer()

        if data.startswith("pick|"):
            _, iso = data.split("|", 1)
            iso = bump_to_future(iso)
            title = (context.user_data.pop("pending_title", None) or extract_title_fallback("Напоминание"))
            user_tz = context.user_data.get("tz", DEFAULT_TZ)
            rid = db.add(query.message.chat_id, title, user_tz, iso)
            await query.edit_message_text(f"📅 Окей, напомню «{title}» {fmt_dt(iso)}")
            schedule_job_for(context.application, db.get(rid))
            return

        # weekly: pick weekday
        if data.startswith("pwd|"):
            _, byday = data.split("|", 1)
            rrule = (context.user_data.get("pending_rrule") or "FREQ=WEEKLY")
            parts = parse_rrule(rrule); parts["BYDAY"]=byday
            rrule = ";".join([f"{k}={v}" for k,v in parts.items()])
            context.user_data["pending_rrule"] = rrule
            if "BYHOUR" not in parts:
                await query.edit_message_text("Во сколько напоминать?", reply_markup=kb_pick_time())
                return
            tz_str = context.user_data.get("pending_tz", DEFAULT_TZ)
            tz = tz_from_offset(tz_str)
            title = context.user_data.pop("pending_title","Напоминание")
            due_iso = next_due_from_rrule(datetime.now(tz), rrule)
            rid = db.add(query.message.chat_id, title, tz_str, due_iso, rrule=rrule)
            await query.edit_message_text(_ack_periodic(title, rrule, due_iso))
            schedule_job_for(context.application, db.get(rid))
            return

        # pick time (presets / manual)
        if data.startswith("pt|"):
            _, val = data.split("|", 1)
            if val == "other":
                context.user_data["time_waiting"] = True
                await query.edit_message_text("Введи время в формате HH:MM (например, 09:30)")
                return
            hh, mm = val.split(":")
            rrule = context.user_data.get("pending_rrule") or "FREQ=DAILY"
            parts = parse_rrule(rrule); parts["BYHOUR"]=str(int(hh)); parts["BYMINUTE"]=str(int(mm))
            rrule = ";".join([f"{k}={v}" for k,v in parts.items()])
            context.user_data["pending_rrule"] = rrule
            tz_str = context.user_data.get("pending_tz", DEFAULT_TZ)
            tz = tz_from_offset(tz_str)
            title = context.user_data.pop("pending_title","Напоминание")
            due_iso = next_due_from_rrule(datetime.now(tz), rrule)
            rid = db.add(query.message.chat_id, title, tz_str, due_iso, rrule=rrule)
            await query.edit_message_text(_ack_periodic(title, rrule, due_iso))
            schedule_job_for(context.application, db.get(rid))
            return

        if data.startswith("snz|"):
            _, delta, rid = data.split("|", 2)
            row = db.get(rid)
            if not row or row["status"] != "active":
                await query.edit_message_text("⏰ Отложено (напоминание не найдено)")
                return
            user_tz = row["tz"]
            tz = tz_from_offset(user_tz)
            now = datetime.now(tz)
            if delta.endswith("m"):
                new_iso = (now + timedelta(minutes=int(delta[:-1]))).replace(microsecond=0).isoformat()
            elif delta.endswith("h"):
                new_iso = (now + timedelta(hours=int(delta[:-1]))).replace(microsecond=0).isoformat()
            else:
                new_iso = (now + timedelta(minutes=10)).replace(microsecond=0).isoformat()
            db.update_due(rid, new_iso)
            cancel_job_if_exists(context.application, rid)
            schedule_job_for(context.application, db.get(rid))
            await query.edit_message_text(f"⏰ Отложено «{row['title']}» до {fmt_dt(new_iso)}")
            return

        if data.startswith("done|"):
            _, rid = data.split("|", 1)
            row = db.get(rid)
            if row:
                db.set_status(rid, "done")
                cancel_job_if_exists(context.application, rid)
                await query.edit_message_text(f"✅ Выполнено: «{row['title']}»")
            else:
                await query.edit_message_text("✅ Выполнено")
            return

        if data.startswith("lp|"):
            _, p = data.split("|", 1)
            await render_list_page(update, context, page=int(p))
            return

        if data.startswith("ldel|"):
            _, rid, ptag = data.split("|", 2)
            page = int(ptag.lstrip("p")) if ptag.startswith("p") else 1
            row = db.get(rid)
            if row:
                db.set_status(rid, "canceled")
                cancel_job_if_exists(context.application, rid)
            await render_list_page(update, context, page=page)
            return

    except Exception as e:
        logging.exception("handle_callbacks failed: %s", e)
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="Что-то пошло не так.")
        except Exception:
            pass

# =====================
# Commands
# =====================
async def cmd_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_list(update, context)

async def cmd_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# =====================
# Main
# =====================
def main():
    app = Application.builder().token(TOKEN).build()

    schedule_all_on_start(app)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_tz_choice, pattern="^tz"))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^[+-]"), handle_tz_manual))

    menu_filter = (
        filters.Regex(f"^{re.escape(MENU_BTN_LIST)}$") |
        filters.Regex(f"^{re.escape(MENU_BTN_SETTINGS)}$") |
        filters.TEXT  # чтобы ловить ручной ввод времени
    )
    app.add_handler(MessageHandler(menu_filter, handle_menu_buttons))

    app.add_handler(CommandHandler("reload", reload_prompts))
    app.add_handler(CommandHandler("list", cmd_list_handler))
    app.add_handler(CommandHandler("tz", cmd_tz))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.add_handler(CallbackQueryHandler(handle_callbacks, pattern="^(pick|snz|done|lp|ldel|pwd|pt)"))

    async def on_error(update, context):
        logging.exception("PTB error: %s | update=%r", context.error, update)
    app.add_error_handler(on_error)

    logging.info("Bot starting… polling enabled")
    app.run_polling(
        allowed_updates=[UpdateType.MESSAGE, UpdateType.CALLBACK_QUERY],
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=10
    )

if __name__ == "__main__":
    main()
