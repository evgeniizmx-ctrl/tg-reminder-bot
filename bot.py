import os
import re
import json
import yaml
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo

import pytz  # noqa: F401  (оставлено как в исходнике)
from pydantic import BaseModel  # noqa: F401
from openai import OpenAI

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --------------------------- logging ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reminder-bot")

# --------------------------- env ---------------------------
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or ""
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_APIKEY") or ""
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
PROMPTS_PATH = os.getenv("PROMPTS_PATH", "prompts.yaml")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "+03:00")  # как и раньше

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing")
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY missing — LLM parsing will fail.")
if not BOT_TOKEN:
    raise RuntimeError("No BOT_TOKEN set")

client = OpenAI(api_key=OPENAI_API_KEY)
openai_client = client  # алиас для совместимости с исходником

# --------------------------- scheduler & db ---------------------------
scheduler = AsyncIOScheduler(timezone="UTC")

# --------------------------- DB ---------------------------
DB_PATH = os.getenv("DB_PATH", "reminders.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Приведён к валидному SQL: в исходнике было несколько перепутанных execute/кавычек.
cur.execute(
    """
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  chat_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  when_ts INTEGER,               -- unix ts для одноразовых и "время суток" старта для периодики
  tz TEXT,                       -- "+03:00"
  repeat TEXT DEFAULT 'none',    -- 'none'|'daily'|'weekly'|'monthly'
  day_of_week INTEGER,           -- 1..7 (Пн..Вс) для weekly
  day_of_month INTEGER,          -- 1..31 для monthly
  state TEXT DEFAULT 'active',   -- 'active'|'done'|'cancelled'
  iso TEXT,                      -- для one-shot в ISO
  recurrence TEXT,               -- json: {"type": "...", "weekday": "...", "day": 5, "time":"HH:MM", "tz":"+03:00"}
  created_at TEXT
);
"""
)
conn.commit()

# --------------------------- Меню ---------------------------
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📋 Список напоминаний")],
        [KeyboardButton("⚙️ Настройки")],
    ],
    resize_keyboard=True,
)

# --------------------------- PROMPTS ---------------------------
class PromptPack:
    def __init__(self, data: dict):
        # В исходнике было две разные версии; объединил безопасно
        self.system = data.get("system", "")
        parse = data.get("parse", {}) or {}
        self.parse_system = parse.get("system", "")
        self.fewshot = data.get("fewshot", []) or []
        self.parse = parse  # чтобы старые обращения PROMPTS.parse.get(...) не падали


def load_prompts() -> PromptPack:
    path = PROMPTS_PATH
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    pp = PromptPack(raw)
    log.info("Prompts loaded: system=%s... | fewshot=%d", (pp.system or "")[:30], len(pp.fewshot))
    return pp


PROMPTS = load_prompts()

# --------------------------- ДАТЫ/ВРЕМЯ ---------------------------
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


def now_iso_with_offset(offset_str: str) -> str:
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    sign = 1 if offset_str.startswith("+") else -1
    hh, mm = map(int, offset_str[1:].split(":"))
    delta = timedelta(hours=hh, minutes=mm)
    local = now_utc + sign * delta
    return local.isoformat(timespec="seconds")


def parse_weekday_to_cron(weekday: str) -> str:
    # APS CronTrigger поддерживает 'mon,tue,...'
    return weekday

# --------------------------- Отправка уведомления ---------------------------
async def send_reminder(bot, chat_id: int, title: str):
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Через 10 мин", callback_data="snz:10"),
                InlineKeyboardButton("Через 1 час", callback_data="snz:60"),
            ],
            [InlineKeyboardButton("✅", callback_data="done")],
        ]
    )
    await bot.send_message(chat_id, f"🔔 «{title}»", reply_markup=kb)

# --------------------------- Планировщик добавления задач ---------------------------
def add_one_shot_job(app: Application, chat_id: int, title: str, iso: str):
    dt = datetime.fromisoformat(iso)
    trigger = DateTrigger(run_date=dt)
    scheduler.add_job(
        send_reminder,
        trigger=trigger,
        args=[app.bot, chat_id, title],
        id=f"one:{chat_id}:{iso}:{title}",
        replace_existing=True,
        misfire_grace_time=60,
    )

def add_recurrence_job(app: Application, chat_id: int, title: str, rec: dict, tz: str):
    t = rec.get("type")
    time_str = rec.get("time")
    hh, mm = map(int, time_str.split(":"))
    if t == "daily":
        trig = CronTrigger(hour=hh, minute=mm)
    elif t == "weekly":
        dow = parse_weekday_to_cron(rec.get("weekday"))
        trig = CronTrigger(day_of_week=dow, hour=hh, minute=mm)
    elif t == "monthly":
        day = int(rec.get("day"))
        trig = CronTrigger(day=day, hour=hh, minute=mm)
    else:
        return

    scheduler.add_job(
        send_reminder,
        trigger=trig,
        args=[app.bot, chat_id, title],
        id=f"rec:{chat_id}:{title}:{json.dumps(rec, ensure_ascii=False)}",
        replace_existing=False,
        misfire_grace_time=300,
    )

# --------------------------- LLM ---------------------------
async def call_llm(user_text: str, tz: str, followup: bool = False) -> dict:
    tz = ensure_tz_offset(tz)
    now_iso = now_iso_with_offset(tz)

    messages = []
    if PROMPTS.system:
        messages.append({"role": "system", "content": PROMPTS.system})
    messages.append({"role": "system", "content": f"NOW_ISO={now_iso}  TZ_DEFAULT={tz}"})
    if PROMPTS.parse_system:
        messages.append({"role": "system", "content": PROMPTS.parse_system})
    for ex in PROMPTS.fewshot:
        messages.append(ex)

    if followup:
        messages.append(
            {
                "role": "system",
                "content": "Это продолжение с ответом на уточняющий вопрос. Верни чистый JSON.",
            }
        )

    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(resp.choices[0].message.content)
        return data
    except Exception as e:
        log.exception("LLM parse error: %s", e)
        return {
            "intent": "chat",
            "title": "Напоминание",
            "fixed_datetime": None,
            "recurrence": None,
        }

# --------------------------- Уточнения ---------------------------
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

# --------------------------- CRUD ---------------------------
def save_reminder_row(
    user_id: Optional[int],
    chat_id: int,
    title: str,
    when_iso: Optional[str],
    tz_offset: str,
    repeat: str = "none",
    day_of_week: Optional[int] = None,
    day_of_month: Optional[int] = None,
    iso: Optional[str] = None,
    recurrence: Optional[dict] = None,
) -> int:
    when_ts = iso_to_unix(when_iso) if when_iso else None
    cur.execute(
        """
        INSERT INTO reminders (user_id, chat_id, title, when_ts, tz, repeat, day_of_week, day_of_month, state, iso, recurrence, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
            iso,
            json.dumps(recurrence, ensure_ascii=False) if recurrence else None,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def save_reminder(chat_id: int, title: str, iso: Optional[str], rec: Optional[dict], tz: str) -> bool:
    conn.execute(
        "INSERT INTO reminders (chat_id, title, iso, recurrence, created_at) VALUES (?,?,?,?,?)",
        (
            chat_id,
            title,
            iso,
            json.dumps({**(rec or {}), "tz": tz}, ensure_ascii=False) if rec else None,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    return True


def snooze_reminder(rem_id: int, minutes: int) -> Optional[int]:
    cur.execute("SELECT * FROM reminders WHERE id=? AND state='active'", (rem_id,))
    row = cur.fetchone()
    if not row:
        return None
    new_ts = (row["when_ts"] or int(datetime.utcnow().timestamp())) + minutes * 60
    cur.execute("UPDATE reminders SET when_ts=? WHERE id=?", (new_ts, rem_id))
    conn.commit()
    return new_ts


def delete_reminder_row(rem_id: int) -> bool:
    cur.execute("UPDATE reminders SET state='cancelled' WHERE id=?", (rem_id,))
    conn.commit()
    return True


def delete_reminder(rem_id: int, chat_id: int) -> bool:
    c = conn.execute("DELETE FROM reminders WHERE id=? AND chat_id=?", (rem_id, chat_id))
    conn.commit()
    return c.rowcount > 0


def list_future(chat_id: int):
    rows = conn.execute(
        "SELECT id, title, iso, recurrence FROM reminders WHERE chat_id=? ORDER BY id DESC",
        (chat_id,),
    ).fetchall()
    return rows

# --------------------------- Периодика: вычисление next ---------------------------
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

# --------------------------- UI helpers ---------------------------
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

# --------------------------- HANDLERS ---------------------------
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = context.user_data.get("tz") or DEFAULT_TZ
    context.user_data["tz"] = tz
    await update.message.reply_text(
        "Примеры:\n"
        "• завтра в 11 падел\n"
        "• через 10 минут позвонить\n"
        "• каждый день в 9 пить таблетки\n"
        "• раз в неделю в среду в 19 — зал\n"
        "• каждое 5 число месяца в 18 — баня\n\n"
        f"Часовой пояс установлен: UTC{tz}\nТеперь напиши что и когда напомнить.\n\n"
        f"Кнопки меню снизу активированы. Можешь нажать или просто написать задачу 👇",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📋 Список напоминаний")], [KeyboardButton("⚙️ Настройки")]],
            resize_keyboard=True,
        ),
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

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = list_future(chat_id)
    if not rows:
        await update.message.reply_text("Будущих напоминаний нет.")
        return

    lines = ["🗒 Ближайшие напоминания:"]
    kb_rows = []
    for rid, title, iso, rec_json in rows:
        if iso:
            lines.append(f"• {iso} — «{title}»")
        else:
            r = json.loads(rec_json)
            t = r.get("time")
            typ = r.get("type")
            if typ == "daily":
                lines.append(f"• каждый день в {t} — «{title}»")
            elif typ == "weekly":
                lines.append(f"• по {r.get('weekday')} в {t} — «{title}»")
            else:
                lines.append(f"• каждое {r.get('day')} в {t} — «{title}»")
        kb_rows.append([InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{rid}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows))

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.startswith("📋"):
        await cmd_list(update, context)
    elif txt.startswith("⚙️"):
        await update.message.reply_text("Настройки пока в разработке.", reply_markup=MAIN_MENU)
    else:
        await handle_text(update, context)

async def on_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat_id
    await query.answer()
    if data == "done":
        await query.edit_message_text("✅ Выполнено")
        return
    if data.startswith("snz:"):
        mins = int(data.split(":")[1])
        await query.edit_message_text(f"⏰ Отложено на {mins} мин")
        asyncio.create_task(context.bot.send_message(chat_id, f"Напомню через {mins} мин."))
        return
    if data.startswith("del:"):
        rid = int(data.split(":")[1])
        if delete_reminder(rid, chat_id):
            await query.edit_message_text("🗑 Удалено")
        else:
            await query.edit_message_text("Не нашёл напоминание")
        return
    if data.startswith("clar:"):
        idx = int(data.split(":")[1])
        c = context.user_data.get("clarify")
        if not c:
            return
        variants = c.get("variants") or []
        picked = variants[idx]
        original = c["original_text"]
        merged = f"{original}\nОтвет на уточнение ({c['expects']}): {picked}"
        result = await call_llm(merged, context.user_data.get("tz", DEFAULT_TZ), followup=True)
        context.user_data.pop("clarify", None)
        await apply_llm_result(result, update, context, by_callback=True)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    tz_offset = ensure_tz_offset(context.user_data.get("tz_offset") or DEFAULT_TZ)

    # быстрые команды меню
    if text in ("📋 Список напоминаний", "/list"):
        await cmd_list(update, context)
        return
    if text in ("⚙️ Настройки", "/settings"):
        await update.message.reply_text("Раздел «Настройки» в разработке.")
        return

    data = await call_llm(text, tz_offset)
    if not data:
        await update.message.reply_text("Не понял. Пример: «завтра в 6 падел».")
        return

    # если это ответ на предыдущее уточнение
    c = context.user_data.get("clarify")
    if c:
        answer = text
        original = c["original_text"]
        merged = f"{original}\nОтвет на уточнение ({c['expects']}): {answer}"
        result = await call_llm(merged, context.user_data.get("tz", DEFAULT_TZ), followup=True)
        if result.get("intent") == "ask_clarification":
            q = result.get("question") or "Уточни, пожалуйста"
            variants = result.get("variants") or []
            context.user_data["clarify"] = {
                "original_text": original,
                "expects": result.get("expects"),
                "question": q,
                "variants": variants,
            }
            if variants:
                keyboard = [[InlineKeyboardButton(v, callback_data=f"clar:{i}")] for i, v in enumerate(variants)]
                await update.message.reply_text(q, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await update.message.reply_text(q)
            return
        context.user_data.pop("clarify", None)
        await apply_llm_result(result, update, context)
        return

    intent = data.get("intent", "create_reminder")
    title = data.get("title") or "Напоминание"
    fixed = data.get("fixed_datetime")
    repeat = data.get("repeat", "none")
    day_of_week = data.get("day_of_week")
    day_of_month = data.get("day_of_month")
    timezone_str = ensure_tz_offset(data.get("timezone") or tz_offset)

    if intent == "ask_clarification":
        upsert_pending(user.id, data)
        d = PENDING.get(user.id, {"clarify_count": 0})
        if d["clarify_count"] >= MAX_CLARIFY:
            pen = PENDING[user.id]["pending"]
            rem_id = save_reminder_row(
                user.id,
                chat_id,
                pen.get("title", title),
                pen.get("fixed_datetime"),
                ensure_tz_offset(pen.get("timezone") or timezone_str),
                pen.get("repeat", "none"),
                pen.get("day_of_week"),
                pen.get("day_of_month"),
                iso=pen.get("fixed_datetime"),
            )
            PENDING.pop(user.id, None)
            pretty = (
                unix_to_local_str(iso_to_unix(pen["fixed_datetime"]), ensure_tz_offset(pen.get("timezone") or timezone_str))
                if pen.get("fixed_datetime")
                else "—"
            )
            await update.message.reply_text(
                f"📅 Окей, записал: {pen.get('title','Напоминание')} — {pretty}", reply_markup=MAIN_MENU
            )
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

    # обычная новая команда
    result = data
    await apply_llm_result(result, update, context)

async def apply_llm_result(result: dict, update: Update, context: ContextTypes.DEFAULT_TYPE, by_callback: bool = False):
    chat_id = update.effective_chat.id if not by_callback else update.callback_query.message.chat_id
    tz = context.user_data.get("tz", DEFAULT_TZ)

    title = result.get("title") or "Напоминание"
    iso = result.get("fixed_datetime")
    rec = result.get("recurrence")

    # одноразовое
    if iso:
        save_reminder(chat_id, title, iso, None, tz)
        add_one_shot_job(context.application, chat_id, title, iso)
        dt_short = iso.replace("T", " ")[:-3]
        send_fn = update.callback_query.message.edit_text if by_callback else update.message.reply_text
        await send_fn(f"📅 Окей, напомню «{title}» {dt_short}")
        return

    # периодическое
    if rec:
        save_reminder(chat_id, title, None, rec, tz)
        add_recurrence_job(context.application, chat_id, title, rec, tz)
        if rec["type"] == "daily":
            when = f"каждый день в {rec['time']}"
        elif rec["type"] == "weekly":
            when = f"по {rec['weekday']} в {rec['time']}"
        else:
            when = f"каждое {rec['day']} число в {rec['time']}"
        send_fn = update.callback_query.message.edit_text if by_callback else update.message.reply_text
        await send_fn(f"📅 Окей, буду напоминать «{title}» {when}")
        return

    # fallback
    send_fn = update.callback_query.message.edit_text if by_callback else update.message.reply_text
    await send_fn("Я не понял, попробуй ещё раз.")

# --------------------------- Callback (snooze/done/del/clarify) ---------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    if data.startswith("snooze:"):
        try:
            _, mins, rem_id = data.split(":", 2)
            mins = int(mins)
            rem_id = int(rem_id)
        except Exception:
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
        except Exception:
            return
        cur.execute("UPDATE reminders SET state='done' WHERE id=?", (rem_id,))
        conn.commit()
        await q.edit_message_text("✅ Выполнено")
    elif data.startswith("del:"):
        try:
            _, rem_id = data.split(":", 1)
            rem_id = int(rem_id)
        except Exception:
            return
        ok = delete_reminder_row(rem_id)
        if ok:
            await q.edit_message_text("🗑 Удалено")
    elif data.startswith("clarify:"):
        try:
            _, iso, dow, dom = data.split(":", 3)
        except Exception:
            return
        user_id = q.from_user.id
        d = PENDING.get(user_id, {"pending": {}, "clarify_count": 0})
        pen = d["pending"]
        if iso:
            pen["fixed_datetime"] = iso
        if dow:
            try:
                pen["day_of_week"] = int(dow)
            except Exception:
                pass
        if dom:
            try:
                pen["day_of_month"] = int(dom)
            except Exception:
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
            save_reminder_row(
                user_id,
                q.message.chat_id,
                pen.get("title", "Напоминание"),
                pen.get("fixed_datetime"),
                ensure_tz_offset(pen.get("timezone") or "+03:00"),
                pen.get("repeat", "none"),
                pen.get("day_of_week"),
                pen.get("day_of_month"),
                iso=pen.get("fixed_datetime"),
            )
            PENDING.pop(user_id, None)
            pretty = (
                unix_to_local_str(iso_to_unix(pen["fixed_datetime"]), ensure_tz_offset(pen.get("timezone") or "+03:00"))
                if pen.get("fixed_datetime")
                else "-"
            )
            await q.edit_message_text(f"✅ Записал: {pen.get('title','Напоминание')} — {pretty}")
            return

        # Если ещё не готовы все поля — задаём кнопки-уточнения
        if not pen.get("fixed_datetime"):
            opts = []
            for hh in ["08:00", "12:00", "19:00"]:
                iso_x = build_today_time_iso(ensure_tz_offset(pen.get("timezone") or "+03:00"), hh)
                opts.append({"iso_datetime": iso_x, "label": hh, "day_of_week": None, "day_of_month": None})
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

# --------------------------- TICKER (через JobQueue) ---------------------------
async def scheduler_tick(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
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
            [
                [
                    InlineKeyboardButton("⏰ через 10 мин", callback_data=f"snooze:10:{row['id']}"),
                    InlineKeyboardButton("🕒 через 1 час", callback_data=f"snooze:60:{row['id']}"),
                    InlineKeyboardButton("✅ Готово", callback_data=f"done:{row['id']}"),
                ]
            ]
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

# --------------------------- APP ---------------------------
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

# --------------------------- main ---------------------------
def main():
    # запускаем отдельный AsyncIOScheduler (используем только для cron/date — задачи добавляем функциями выше)
    scheduler.start()

    # PTB v20+: run_polling()
    app = build_app()
    log.info("Bot starting… polling enabled")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
