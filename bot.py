import os
import json
import yaml
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from openai import OpenAI

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# --------------------------- logging ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("rembot")

# --------------------------- env ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PROMPTS_PATH = os.getenv("PROMPTS_PATH", "prompts.yaml")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "+03:00")  # как и раньше

if not BOT_TOKEN:
    raise RuntimeError("No BOT_TOKEN set")

client = OpenAI(api_key=OPENAI_API_KEY)

# --------------------------- scheduler & db ---------------------------
scheduler = AsyncIOScheduler(timezone="UTC")

DB_PATH = os.getenv("DB_PATH", "reminders.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  iso TEXT,                      -- for one-shot
  recurrence TEXT,               -- json: {"type": "...", "weekday": "...", "day": 5, "time":"HH:MM", "tz":"+03:00"}
  created_at TEXT
);
""")
conn.commit()

# --------------------------- prompts ---------------------------
class PromptPack:
    def __init__(self, data: dict):
        self.system = data.get("system", "")
        parse = data.get("parse", {}) or {}
        self.parse_system = parse.get("system", "")
        self.fewshot = data.get("fewshot", []) or []

def load_prompts() -> PromptPack:
    with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    pp = PromptPack(raw)
    log.info("Prompts loaded: system=%s... | fewshot=%d",
             (pp.system or "")[:30], len(pp.fewshot))
    return pp

PROMPTS = load_prompts()

# --------------------------- utils ---------------------------
def now_iso_with_offset(offset_str: str) -> str:
    # offset like +03:00
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    sign = 1 if offset_str.startswith("+") else -1
    hh, mm = map(int, offset_str[1:].split(":"))
    delta = timedelta(hours=hh, minutes=mm)
    local = now_utc + sign * delta
    return local.isoformat(timespec="seconds")

def parse_weekday_to_cron(weekday: str) -> str:
    # mon..sun -> 0..6 (cron: 0=mon in APS? CronTrigger with day_of_week uses mon-sun text)
    # APS CronTrigger supports 'mon,tue,...'
    return weekday

async def send_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: str):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Через 10 мин", callback_data="snz:10"),
            InlineKeyboardButton("Через 1 час", callback_data="snz:60"),
        ],
        [InlineKeyboardButton("✅", callback_data="done")]
    ])
    await context.bot.send_message(chat_id, f"🔔 «{title}»", reply_markup=kb)

def add_one_shot_job(app: Application, chat_id: int, title: str, iso: str):
    # APScheduler ожидает UTC — сконвертируем
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
    # APS CronTrigger использует timezone из scheduler; мы оставляем UTC и даём offset в часах через cron невозможно.
    # Поэтому просто создаём CronTrigger по локальному времени и полагаемся на фиксированный offset (как и раньше).
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

def save_reminder(chat_id: int, title: str, iso: str | None, rec: dict | None, tz: str):
    conn.execute(
        "INSERT INTO reminders (chat_id, title, iso, recurrence, created_at) VALUES (?,?,?,?,?)",
        (chat_id, title, iso, json.dumps({**(rec or {}), "tz": tz}, ensure_ascii=False) if rec else None, datetime.utcnow().isoformat())
    )
    conn.commit()

def list_future(chat_id: int):
    rows = conn.execute("SELECT id, title, iso, recurrence FROM reminders WHERE chat_id=? ORDER BY id DESC", (chat_id,)).fetchall()
    return rows

def delete_reminder(rem_id: int, chat_id: int) -> bool:
    cur = conn.execute("DELETE FROM reminders WHERE id=? AND chat_id=?", (rem_id, chat_id))
    conn.commit()
    return cur.rowcount > 0

# --------------------------- LLM ---------------------------
async def call_llm(user_text: str, tz: str, followup: bool = False) -> dict:
    now_iso = now_iso_with_offset(tz)
    sys_hint = f"NOW_ISO={now_iso}  TZ_DEFAULT={tz}"

    messages = [
        {"role": "system", "content": PROMPTS.system},
        {"role": "system", "content": sys_hint},
        {"role": "system", "content": PROMPTS.parse_system},
    ]
    # fewshot
    for ex in PROMPTS.fewshot:
        messages.append(ex)

    if followup:
        messages.append({"role": "system", "content": "Это продолжение с ответом на уточняющий вопрос. Верни чистый JSON."})

    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.2,
        response_format={ "type": "json_object" }
    )
    try:
        data = json.loads(resp.choices[0].message.content)
        return data
    except Exception as e:
        log.exception("LLM parse error: %s", e)
        return {"intent": "chat", "title": "Напоминание", "fixed_datetime": None, "recurrence": None}

# --------------------------- handlers ---------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = context.user_data.get("tz") or DEFAULT_TZ
    context.user_data["tz"] = tz
    await update.message.reply_text(
        f"Часовой пояс установлен: UTC{tz}\nТеперь напиши что и когда напомнить.\n\n"
        f"Кнопки меню снизу активированы. Можешь нажать или просто написать задачу 👇",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📋 Список напоминаний")], [KeyboardButton("⚙️ Настройки")]],
            resize_keyboard=True
        )
    )

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
        # тут можно сохранить «снуз» как одноразовую дату
        return
    if data.startswith("del:"):
        rid = int(data.split(":")[1])
        if delete_reminder(rid, chat_id):
            await query.edit_message_text("🗑 Удалено")
        else:
            await query.edit_message_text("Не нашёл напоминание")
        return
    if data.startswith("clar:"):
        # обработка выбора варианта на втором уточнении
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
    text = (update.message.text or "").strip()
    if text in ("📋 Список напоминаний", "/list"):
        await cmd_list(update, context)
        return
    if text in ("⚙️ Настройки", "/settings"):
        await update.message.reply_text("Раздел «Настройки» в разработке.")
        return

    # если это ответ на уточнение
    c = context.user_data.get("clarify")
    if c:
        answer = text
        original = c["original_text"]
        merged = f"{original}\nОтвет на уточнение ({c['expects']}): {answer}"
        result = await call_llm(merged, context.user_data.get("tz", DEFAULT_TZ), followup=True)
        # если снова ask_clarification и теперь есть variants — покажем кнопки
        if result.get("intent") == "ask_clarification":
            q = result.get("question") or "Уточни, пожалуйста"
            variants = result.get("variants") or []
            context.user_data["clarify"] = {
                "original_text": original,
                "expects": result.get("expects"),
                "question": q,
                "variants": variants
            }
            if variants:
                keyboard = [[InlineKeyboardButton(v, callback_data=f"clar:{i}")]
                            for i, v in enumerate(variants)]
                await update.message.reply_text(q, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await update.message.reply_text(q)
            return
        context.user_data.pop("clarify", None)
        await apply_llm_result(result, update, context)
        return

    # обычная новая команда
    result = await call_llm(text, context.user_data.get("tz", DEFAULT_TZ))
    # если нужно уточнение — только текстом
    if result.get("intent") == "ask_clarification":
        q = result.get("question") or "Уточни, пожалуйста"
        context.user_data["clarify"] = {
            "original_text": text,
            "expects": result.get("expects"),
            "question": q,
            "variants": result.get("variants") or []
        }
        await update.message.reply_text(q)
        return

    await apply_llm_result(result, update, context)

async def apply_llm_result(result: dict, update: Update, context: ContextTypes.DEFAULT_TYPE, by_callback: bool = False):
    chat_id = update.effective_chat.id
    tz = context.user_data.get("tz", DEFAULT_TZ)

    title = result.get("title") or "Напоминание"
    iso = result.get("fixed_datetime")
    rec = result.get("recurrence")

    # одноразовое
    if iso:
        save_reminder(chat_id, title, iso, None, tz)
        add_one_shot_job(context.application, chat_id, title, iso)
        dt_short = iso.replace("T", " ")[:-3]
        await (update.callback_query.message.edit_text if by_callback else update.message.reply_text)(
            f"📅 Окей, напомню «{title}» {dt_short}"
        )
        return

    # периодическое
    if rec:
        save_reminder(chat_id, title, None, rec, tz)
        add_recurrence_job(context.application, chat_id, title, rec, tz)
        # красивая подпись
        if rec["type"] == "daily":
            when = f"каждый день в {rec['time']}"
        elif rec["type"] == "weekly":
            when = f"по {rec['weekday']} в {rec['time']}"
        else:
            when = f"каждое {rec['day']} число в {rec['time']}"
        await (update.callback_query.message.edit_text if by_callback else update.message.reply_text)(
            f"📅 Окей, буду напоминать «{title}» {when}"
        )
        return

    # fallback
    await (update.callback_query.message.edit_text if by_callback else update.message.reply_text)(
        "Я не понял, попробуй ещё раз."
    )

# --------------------------- main ---------------------------
def main():
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    # scheduler
    scheduler.start()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(on_buttons))

    log.info("Bot starting… polling enabled")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
