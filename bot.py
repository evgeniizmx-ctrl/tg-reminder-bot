import os
import io
import re
import json
import yaml
import logging
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, ValidationError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import UpdateType
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from openai import OpenAI

# =====================
# Config & Logging
# =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def _extract_token(raw: str | None) -> str:
    if not raw:
        return ""
    raw = raw.strip().replace("\u200b", "").replace("\u200c", "").replace("\uFEFF", "")
    raw = raw.strip(" '\"")
    m = re.search(r"[0-9]+:[A-Za-z0-9_-]{30,}", raw)
    return m.group(0) if m else raw

RAW_TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
RAW_BOT_TOKEN = os.getenv("BOT_TOKEN")

TOKEN = _extract_token(RAW_TELEGRAM_TOKEN) or _extract_token(RAW_BOT_TOKEN)
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
PROMPTS_PATH = os.getenv("PROMPTS_PATH", "prompts.yaml")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
TRANSCRIBE_MODEL = os.getenv("ASR_MODEL", "whisper-1")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "+03:00")

def _valid_token(t: str) -> bool:
    return bool(re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}", t))

logging.info("Env debug: TELEGRAM_TOKEN=%r BOT_TOKEN=%r | picked=%r",
             RAW_TELEGRAM_TOKEN, RAW_BOT_TOKEN, TOKEN)

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
    # allow "+3" / "+03:00" / "-4:30"
    off = off.strip()
    if re.fullmatch(r"[+-]\d{1,2}$", off):
        sign = off[0]
        hh = int(off[1:])
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

async def schedule_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: str, iso_when: str):
    try:
        when = datetime.fromisoformat(iso_when)
        now = datetime.now(when.tzinfo)
        if when <= now:
            when = now + timedelta(seconds=2)
        async def _fire(ctx: ContextTypes.DEFAULT_TYPE):
            await ctx.bot.send_message(chat_id=chat_id, text=f"🔔 {title or 'Напоминание'}")
        context.job_queue.run_once(_fire, when=when)
        logging.info("Scheduled reminder at %s for chat %s", when.isoformat(), chat_id)
    except Exception as e:
        logging.exception("schedule_reminder failed: %s", e)

# ---- Local relative-time parser ("через ...") ----
REL_MIN = re.compile(r"через\s+(?:минуту|1\s*мин(?:ут)?)(?:\b|$)", re.I)
REL_NMIN = re.compile(r"через\s+(\d+)\s*мин(?:ут|ы)?(?:\b|$)", re.I)
REL_HALF = re.compile(r"через\s+полчаса(?:\b|$)", re.I)
REL_NH = re.compile(r"через\s+(\d+)\s*час(?:а|ов)?(?:\b|$)", re.I)
REL_ND = re.compile(r"через\s+(\d+)\s*д(ень|ня|ней)?(?:\b|$)", re.I)
REL_WEEK = re.compile(r"через\s+недел(?:ю|ю)(?:\b|$)", re.I)

def _clean_title(text: str) -> str:
    t = re.sub(r"\b(напомни(ть)?|пожалуйста)\b", "", text, flags=re.I).strip()
    for rx in (REL_MIN, REL_NMIN, REL_HALF, REL_NH, REL_ND, REL_WEEK):
        t = rx.sub("", t).strip()
    return t or "Напоминание"

def try_parse_relative_local(text: str, user_tz: str) -> Optional[str]:
    """Вернёт ISO-строку, если нашли «через …», иначе None."""
    tz = tz_from_offset(user_tz)
    now = datetime.now(tz).replace(microsecond=0)
    if REL_MIN.search(text):
        return (now + timedelta(minutes=1)).isoformat()
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
    return None

def bump_to_future(iso_when: str) -> str:
    """Если время в прошлом — подними до ближайшего будущего (+2с)."""
    try:
        when = datetime.fromisoformat(iso_when)
        now = datetime.now(when.tzinfo)
        if when <= now:
            when = now + timedelta(seconds=2)
        return when.replace(microsecond=0).isoformat()
    except Exception:
        return iso_when

# =====================
# Prompt store
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
# Output schema from LLM
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
# OpenAI calls
# =====================
async def transcribe_voice(file_bytes: bytes, filename: str = "audio.ogg") -> str:
    f = io.BytesIO(file_bytes)
    f.name = filename if filename.endswith(".ogg") else (filename + ".ogg")
    resp = client.audio.transcriptions.create(
        model=TRANSCRIBE_MODEL,
        file=f,
        response_format="text"
    )
    return resp

async def call_llm(text: str, user_tz: str) -> LLMResult:
    now = now_iso_for_tz(user_tz)
    messages = [
        {"role": "system", "content": f"NOW_ISO={now}  TZ_DEFAULT={user_tz}"},
        {"role": "system", "content": PROMPTS.system},
        *PROMPTS.fewshot,
        {"role": "user", "content": text}
    ]
    resp = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.2,
        response_format={"type": "json_object"}
    )
    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
        return LLMResult(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        logging.exception("LLM JSON parse failed: %s\nRaw: %s", e, raw)
        return LLMResult(intent="ask_clarification", need_confirmation=True, options=[])

def build_time_keyboard(options: List[ReminderOption]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(opt.label, callback_data=f"pick|{opt.iso_datetime}") for opt in options]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)

# =====================
# Timezone selection UI
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
    buttons = [[InlineKeyboardButton(label, callback_data=f"tz|{offset}")]
               for label, offset in TZ_OPTIONS]
    buttons.append([InlineKeyboardButton("Другой", callback_data="tz|other")])
    kb = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        "Для начала укажи свой часовой пояс.\n"
        "Выбери из списка или нажми «Другой», чтобы ввести вручную.\n\n"
        "Пример: +11 или -4:30",
        reply_markup=kb
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
            sign = tz[0]
            hh = int(tz[1:])
            tz = f"{sign}{hh:02d}:00"
        context.user_data["tz"] = tz
        context.user_data["tz_waiting"] = False
        await update.message.reply_text(f"Часовой пояс установлен: UTC{tz}\nТеперь напиши что и когда напомнить.")
    else:
        await update.message.reply_text("Неверный формат. Введите, например: +3, +03:00 или -4:30")

# =====================
# Core handlers
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

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tz = context.user_data.get("tz", DEFAULT_TZ)
    text = update.message.text.strip()

    # Локальный быстрый путь для "через ..."
    iso = try_parse_relative_local(text, user_tz)
    if iso:
        title = _clean_title(text)
        iso = bump_to_future(iso)
        await update.message.reply_text(f"Окей, напомню {fmt_dt(iso)}")
        await schedule_reminder(context, update.effective_chat.id, title, iso)
        return

    # Иначе — LLM
    result = await call_llm(text, user_tz)
    if result.fixed_datetime:
        result.fixed_datetime = bump_to_future(result.fixed_datetime)
    await route_llm_result(update, context, result, user_tz)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tz = context.user_data.get("tz", DEFAULT_TZ)
    file = await update.message.voice.get_file()
    file_bytes = await file.download_as_bytearray()
    text = await transcribe_voice(file_bytes, filename="telegram_voice.ogg")

    iso = try_parse_relative_local(text, user_tz)
    if iso:
        title = _clean_title(text)
        iso = bump_to_future(iso)
        await update.message.reply_text(f"Окей, напомню {fmt_dt(iso)}")
        await schedule_reminder(context, update.effective_chat.id, title, iso)
        return

    result = await call_llm(text, user_tz)
    if result.fixed_datetime:
        result.fixed_datetime = bump_to_future(result.fixed_datetime)
    await route_llm_result(update, context, result, user_tz)

async def route_llm_result(update: Update, context: ContextTypes.DEFAULT_TYPE, result: LLMResult, user_tz: str):
    chat_id = update.effective_chat.id
    if result.intent == "create_reminder" and result.fixed_datetime:
        await update.message.reply_text(f"Окей, напомню {fmt_dt(result.fixed_datetime)}")
        await schedule_reminder(context, chat_id, result.title or result.text_original or "Напоминание", result.fixed_datetime)
    elif result.intent == "ask_clarification" and result.options:
        kb = build_time_keyboard(result.options)
        await update.message.reply_text("Уточни:", reply_markup=kb)
    else:
        await update.message.reply_text("Не понял. Скажи, например: «завтра в 15 позвонить маме».")

async def handle_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    try:
        logging.info("CallbackQuery data=%r", data)
        await query.answer()
        if "|" in data:
            _, iso = data.split("|", 1)
        elif "::" in data:
            _, iso = data.split("::", 1)
        else:
            iso = data
        iso = bump_to_future(iso)
        await query.edit_message_text(f"Окей, напомню {fmt_dt(iso)}")
        await schedule_reminder(context, query.message.chat_id, "Напоминание", iso)
    except Exception as e:
        logging.exception("handle_pick failed: %s", e)
        try:
            chat_id = (query.message.chat_id if query.message else update.effective_chat.id)
            await context.bot.send_message(chat_id=chat_id, text=f"Окей, напомню {fmt_dt(locals().get('iso','?'))}")
        except Exception:
            logging.exception("fallback send_message failed")

# =====================
# Main
# =====================
def main():
    app = Application.builder().token(TOKEN).build()

    # TZ selection
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_tz_choice, pattern="^tz"))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^[+-]"), handle_tz_manual))

    # Core
    app.add_handler(CommandHandler("reload", reload_prompts))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_pick))  # кнопки времени

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
