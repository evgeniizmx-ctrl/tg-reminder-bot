# bot.py
import os
import re
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from dateutil import parser as dparser

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# -------- OpenAI ------------
from openai import OpenAI
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ---------- ENV -------------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
PROMPTS_PATH = os.environ.get("PROMPTS_PATH", "prompts.yaml")

# ---------- DB --------------
DB_PATH = os.environ.get("DB_PATH", "reminders.db")

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
                when_iso text not null,
                status text default 'scheduled'
            )
        """)
        # слот для незавершённого уточнения LLM
        conn.execute("""
            create table if not exists pending (
                user_id integer primary key,
                text_original text,
                title text,
                expects text,
                question text
            )
        """)
        conn.commit()

# ---- pending helpers ----
def db_get_pending(user_id: int):
    with db() as conn:
        return conn.execute("select * from pending where user_id=?", (user_id,)).fetchone()

def db_set_pending(user_id: int, text_original: str, title: str | None, expects: str | None, question: str | None):
    with db() as conn:
        conn.execute("""
            insert into pending(user_id,text_original,title,expects,question)
            values(?,?,?,?,?)
            on conflict(user_id) do update set
              text_original=excluded.text_original,
              title=excluded.title,
              expects=excluded.expects,
              question=excluded.question
        """, (user_id, text_original, title, expects, question))
        conn.commit()

def db_clear_pending(user_id: int):
    with db() as conn:
        conn.execute("delete from pending where user_id=?", (user_id,))
        conn.commit()

# ---- users ----
def db_get_user_tz(user_id: int) -> str | None:
    with db() as conn:
        row = conn.execute("select tz from users where user_id=?", (user_id,)).fetchone()
        return row["tz"] if row and row["tz"] else None

def db_set_user_tz(user_id: int, tz: str):
    with db() as conn:
        conn.execute("insert into users(user_id, tz) values(?, ?) on conflict(user_id) do update set tz=excluded.tz",
                     (user_id, tz))
        conn.commit()

# ---- reminders ----
def db_add_reminder(user_id: int, title: str, body: str | None, when_iso: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "insert into reminders(user_id,title,body,when_iso) values(?,?,?,?)",
            (user_id, title, body, when_iso)
        )
        conn.commit()
        return cur.lastrowid

def db_mark_done(rem_id: int):
    with db() as conn:
        conn.execute("update reminders set status='done' where id=?", (rem_id,))
        conn.commit()

def db_snooze(rem_id: int, minutes: int):
    with db() as conn:
        row = conn.execute("select when_iso from reminders where id=?", (rem_id,)).fetchone()
        if not row:
            return None
        dt = parse_iso_flexible(row["when_iso"]) + timedelta(minutes=minutes)
        new_iso = iso_no_seconds(dt)
        conn.execute("update reminders set when_iso=?, status='scheduled' where id=?", (new_iso, rem_id))
        conn.commit()
        return dt

def db_delete(rem_id: int):
    with db() as conn:
        conn.execute("delete from reminders where id=?", (rem_id,))
        conn.commit()

def db_future(user_id: int):
    with db() as conn:
        rows = conn.execute(
            "select * from reminders where user_id=? and status='scheduled' order by when_iso asc",
            (user_id,)
        ).fetchall()
        return rows

# ---------- TZ utils --------
OFFSET_RE = re.compile(r'^([+-])(\d{1,2})(?::?(\d{2}))$')

def is_valid_offset(s: str) -> bool:
    return bool(OFFSET_RE.match(s.strip()))

def normalize_offset(s: str) -> str:
    m = OFFSET_RE.match(s.strip())
    if not m:
        return s
    sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    return f"{sign}{hh:02d}:{mm:02d}"

def is_valid_iana(s: str) -> bool:
    try:
        ZoneInfo(s.strip())
        return True
    except Exception:
        return False

def tzinfo_from_user(tz_str: str) -> timezone | ZoneInfo:
    if not tz_str:
        return timezone(timedelta(hours=3))
    tz_str = tz_str.strip()
    if tz_str and tz_str[0] in "+-" and is_valid_offset(tz_str):
        m = OFFSET_RE.match(tz_str)
        sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        delta = timedelta(hours=hh, minutes=mm)
        if sign == "-":
            delta = -delta
        return timezone(delta)
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return timezone(timedelta(hours=3))

def now_in_user_tz(tz_str: str) -> datetime:
    return datetime.now(tzinfo_from_user(tz_str))

def iso_no_seconds(dt: datetime) -> str:
    dt = dt.replace(microsecond=0)
    s = dt.isoformat()
    # ensure no seconds (if present)
    s = re.sub(r":\d{2}([+-Z])", r"\1", s) if re.search(r"T\d{2}:\d{2}:\d{2}", s) else s
    return s

def parse_iso_flexible(s: str) -> datetime:
    return dparser.isoparse(s)

# ---------- UI: Keyboards ---
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
TZ_KB = ReplyKeyboardMarkup([[KeyboardButton(x) for x in row] for row in _TZ_ROWS],
                            resize_keyboard=True, one_time_keyboard=True)

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

# ---------- Prompts ---------
import yaml
def load_prompts():
    with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return raw

PROMPTS = load_prompts()

# ---------- LLM ------------
async def call_llm(user_text: str, user_tz: str, clarify_ctx: dict | None = None) -> dict:
    """
    clarify_ctx optionally:
      {
        "text_original": "...",   # исходный запрос
        "expects": "time|weekday|date|periodicity",
        "question": "Во сколько?"
      }
    """
    now_local = now_in_user_tz(user_tz)
    now_iso = iso_no_seconds(now_local)
    header = f"NOW_ISO={now_iso}\nTZ_DEFAULT={user_tz or '+03:00'}"

    messages = [
        {"role": "system", "content": PROMPTS["system"]},
        {"role": "system", "content": header},
        {"role": "system", "content": PROMPTS["parse"]["system"]},
    ]

    few = PROMPTS.get("fewshot") or []
    messages.extend(few)

    if clarify_ctx:
        prev_marker = f"PREV_INTENT=ask_clarification EXPECTS={clarify_ctx.get('expects') or 'unknown'}"
        messages.append({"role": "system", "content": prev_marker})
        if clarify_ctx.get("question"):
            messages.append({"role": "assistant", "content": clarify_ctx["question"]})
        combined = f"{clarify_ctx.get('text_original','')}\nУточнение пользователя: {user_text}"
        messages.append({"role": "user", "content": combined})
    else:
        messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.2
    )
    txt = resp.choices[0].message.content.strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{[\s\S]+\}", txt)
        if m:
            return json.loads(m.group(0))
        raise

# ---------- Scheduler -------
scheduler = AsyncIOScheduler()

async def fire_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    chat_id = data["chat_id"]
    rem_id = data["rem_id"]
    title = data["title"]

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Через 10 мин", callback_data=f"snooze:10:{rem_id}"),
        InlineKeyboardButton("Через 1 час", callback_data=f"snooze:60:{rem_id}")
    ],[
        InlineKeyboardButton("✅", callback_data=f"done:{rem_id}")
    ]])

    await context.bot.send_message(chat_id, f"🔔 «{title}»", reply_markup=kb)

def schedule_job(app: Application, rem_id: int, user_id: int, when_iso: str, title: str):
    dt = parse_iso_flexible(when_iso)  # aware
    scheduler.add_job(
        fire_reminder,
        trigger=DateTrigger(run_date=dt),
        id=f"rem-{rem_id}",
        replace_existing=True,
        misfire_grace_time=60,
        coalesce=True,
        name=f"rem {rem_id}",
    )
    job = scheduler.get_job(f"rem-{rem_id}")
    if job:
        job.data = {"chat_id": user_id, "rem_id": rem_id, "title": title}

# ---------- Handlers --------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tz = db_get_user_tz(user_id)
    if not tz:
        await update.message.reply_text(
            "Для начала укажи свой часовой пояс.\n"
            "Можешь выбрать кнопкой или прислать:\n"
            "• смещение: +03:00\n"
            "• или IANA-зону: Europe/Moscow",
            reply_markup=TZ_KB
        )
        return
    await update.message.reply_text(
        f"Часовой пояс установлен: {tz}\nТеперь напиши что и когда напомнить.",
        reply_markup=MAIN_MENU_KB
    )

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Раздел «Настройки» в разработке.", reply_markup=MAIN_MENU_KB)

# STRICT TZ PARSER (не трогаем обычные сообщения)
async def try_handle_tz_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text:
        return False
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text in CITY_TO_OFFSET:
        tz = CITY_TO_OFFSET[text]
    elif text == "Другой…":
        await update.message.reply_text("Пришли смещение вида +03:00 или IANA зону (Europe/Moscow).")
        return True
    elif is_valid_offset(text):
        tz = normalize_offset(text)
    elif is_valid_iana(text):
        tz = text
    else:
        return False

    db_set_user_tz(user_id, tz)
    await update.message.reply_text(
        f"Часовой пояс установлен: {tz}\nТеперь напиши что и когда напомнить.",
        reply_markup=MAIN_MENU_KB
    )
    return True

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = db_future(user_id)
    if not rows:
        await update.message.reply_text("Будущих напоминаний нет.", reply_markup=MAIN_MENU_KB)
        return

    lines = ["🗓 Ближайшие напоминания —"]
    kb_rows = []
    tz = db_get_user_tz(user_id) or "+03:00"

    for r in rows:
        dt = parse_iso_flexible(r["when_iso"]).astimezone(tzinfo_from_user(tz))
        lines.append(f"• {dt.strftime('%d.%m в %H:%M')} — «{r['title']}»")
        kb_rows.append([InlineKeyboardButton(f"🗑 Удалить «{r['title']}»", callback_data=f"del:{r['id']}")])

    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows))

async def cb_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data.startswith("del:") or data.startswith("cancel:"):
        rem_id = int(data.split(":")[1])
        db_delete(rem_id)
        await q.edit_message_text("Удалено ✅")
        return

    if data.startswith("snooze:"):
        _, mins, rem_id = data.split(":")
        rem_id = int(rem_id); mins = int(mins)
        dt = db_snooze(rem_id, mins)
        if dt:
            row = db().execute("select user_id,title,when_iso from reminders where id=?", (rem_id,)).fetchone()
            if row:
                schedule_job(context.application, rem_id, row["user_id"], row["when_iso"], row["title"])
                await q.edit_message_text(f"⏲ Отложено на {mins} мин.")
        return

    if data.startswith("done:"):
        rem_id = int(data.split(":")[1])
        db_mark_done(rem_id)
        await q.edit_message_text("✅ Выполнено")
        return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await try_handle_tz_input(update, context):
        return

    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # нижнее меню
    if text == "📝 Список напоминаний" or text.lower() == "/list":
        return await cmd_list(update, context)
    if text == "⚙️ Настройки" or text.lower() == "/settings":
        return await cmd_settings(update, context)

    user_tz = db_get_user_tz(user_id)
    if not user_tz:
        await update.message.reply_text("Сначала укажи часовой пояс.", reply_markup=TZ_KB)
        return

    pending = db_get_pending(user_id)
    clarify_ctx = None
    if pending:
        clarify_ctx = {
            "text_original": pending["text_original"],
            "expects": pending["expects"],
            "question": pending["question"]
        }

    try:
        result = await call_llm(text, user_tz, clarify_ctx=clarify_ctx)
    except Exception:
        await update.message.reply_text("Что-то не понял. Скажи, например: «завтра в 15 позвонить маме».")
        return

    intent = result.get("intent")

    if intent == "ask_clarification":
        q = result.get("question") or "Уточни, пожалуйста."
        expects = result.get("expects")
        if pending:
            base = pending["text_original"]
            new_base = f"{base}. {text}"
        else:
            new_base = text
        db_set_pending(
            user_id,
            new_base,
            result.get("title") or (pending["title"] if pending else None),
            expects,
            q
        )
        variants = result.get("variants") or []
        rows = []
        for v in variants[:4]:
            if isinstance(v, dict) and v.get("iso_datetime"):
                rows.append([InlineKeyboardButton(v.get("label", v.get("iso_datetime")), callback_data=f"pick:{v.get('iso_datetime')}")])
            else:
                rows.append([InlineKeyboardButton(str(v), callback_data=f"pick:{str(v)}")])
        kb = InlineKeyboardMarkup(rows) if rows else None
        await update.message.reply_text(q, reply_markup=kb)
        return

    if intent == "create_reminder":
        db_clear_pending(user_id)

        title = result.get("title") or (pending["title"] if pending and pending["title"] else "Напоминание")
        body = result.get("description")
        dt_iso = result.get("fixed_datetime")
        recurrence = result.get("recurrence")

        if recurrence:
            tzinfo = tzinfo_from_user(user_tz)
            now = now_in_user_tz(user_tz)
            target_time = recurrence.get("time") or "09:00"
            hh, mm = map(int, target_time.split(":"))
            if recurrence["type"] == "daily":
                candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if candidate <= now:
                    candidate += timedelta(days=1)
                dt_iso = iso_no_seconds(candidate)
            elif recurrence["type"] == "weekly":
                weekday_map = {"mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6}
                wd = weekday_map.get(recurrence.get("weekday","mon"),0)
                candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                delta = (wd - candidate.weekday()) % 7
                if delta == 0 and candidate <= now:
                    delta = 7
                candidate = candidate + timedelta(days=delta)
                dt_iso = iso_no_seconds(candidate)
            elif recurrence["type"] == "monthly":
                day = int(recurrence.get("day", now.day))
                year, month = now.year, now.month
                try:
                    candidate = now.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
                    if candidate <= now:
                        month = month + 1 if month < 12 else 1
                        year = year + 1 if month == 1 else year
                        candidate = candidate.replace(year=year, month=month)
                except ValueError:
                    from calendar import monthrange
                    last = monthrange(year, month)[1]
                    candidate = now.replace(day=last, hour=hh, minute=mm, second=0, microsecond=0)
                dt_iso = iso_no_seconds(candidate)

        if not dt_iso:
            await update.message.reply_text("Не понял время. Напиши, например: «сегодня 18:30».")
            return

        dt_iso_clean = iso_no_seconds(parse_iso_flexible(dt_iso))
        rem_id = db_add_reminder(user_id, title, body, dt_iso_clean)
        schedule_job(context.application, rem_id, user_id, dt_iso_clean, title)

        tz = db_get_user_tz(user_id) or "+03:00"
        dt_local = parse_iso_flexible(dt_iso_clean).astimezone(tzinfo_from_user(tz))

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отменить", callback_data=f"cancel:{rem_id}")
        ]])
        await update.message.reply_text(
            f"🔔🔔 Окей, напомню «{title}»\n{dt_local.strftime('%d.%m в %H:%M')}",
            reply_markup=kb
        )
        return

    await update.message.reply_text("Я не понял, попробуй ещё раз.", reply_markup=MAIN_MENU_KB)

async def cb_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("pick:"):
        return
    value = data.split("pick:")[1]
    user_id = q.message.chat_id

    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        iso = value
        title = "Напоминание"
        rem_id = db_add_reminder(user_id, title, None, iso)
        schedule_job(context.application, rem_id, user_id, iso, title)
        tz = db_get_user_tz(user_id) or "+03:00"
        dt_local = parse_iso_flexible(iso).astimezone(tzinfo_from_user(tz))
        await q.edit_message_text(f"📅 Окей, напомню «{title}» {dt_local.strftime('%d.%m в %H:%M')}")
        db_clear_pending(user_id)
        return

    pending = db_get_pending(user_id)
    user_tz = db_get_user_tz(user_id) or "+03:00"
    if pending:
        result = await call_llm(value, user_tz, clarify_ctx={
            "text_original": pending["text_original"],
            "expects": pending["expects"],
            "question": pending["question"]
        })
        if result.get("intent") == "create_reminder":
            db_clear_pending(user_id)
            title = result.get("title") or pending["title"] or "Напоминание"
            dt_iso = result.get("fixed_datetime")
            if not dt_iso:
                await q.edit_message_text("Не понял время.")
                return
            dt_iso_clean = iso_no_seconds(parse_iso_flexible(dt_iso))
            rem_id = db_add_reminder(user_id, title, None, dt_iso_clean)
            schedule_job(context.application, rem_id, user_id, dt_iso_clean, title)
            tz = db_get_user_tz(user_id) or "+03:00"
            dt_local = parse_iso_flexible(dt_iso_clean).astimezone(tzinfo_from_user(tz))
            await q.edit_message_text(f"🔔🔔 Окей, напомню «{title}»\n{dt_local.strftime('%d.%м в %H:%M')}")
            return
        else:
            db_set_pending(user_id, pending["text_original"], pending["title"], result.get("expects"), result.get("question"))
            await q.edit_message_text(result.get("question") or "Уточни, пожалуйста.")
            return

# ---------- main -----------
def main():
    print("INFO planner-bot: Starting PlannerBot...")
    db_init()

    app = Application.builder().token(BOT_TOKEN).build()

    scheduler.start()
    print("INFO planner-bot: APScheduler started in PTB event loop")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("settings", cmd_settings))

    app.add_handler(CallbackQueryHandler(cb_inline, pattern=r"^(del:|done:|snooze:|cancel:)"))
    app.add_handler(CallbackQueryHandler(cb_pick, pattern=r"^pick:"))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
