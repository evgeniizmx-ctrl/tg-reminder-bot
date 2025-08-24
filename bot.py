import os
import io
import re
import json
import yaml
import logging
from typing import List, Optional
from datetime import datetime

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
# Helpers
# =====================
def fmt_dt(iso: str) -> str:
    try:
        # fromisoformat принимает "+03:00"; если придёт Z — можно расширить
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m в %H:%M")
    except Exception:
        return iso

async def transcribe_voice(file_bytes: bytes, filename: str = "audio.ogg") -> str:
    f = io.BytesIO(file_bytes)
    f.name = filename if filename.endswith(".ogg") else (filename + ".ogg")
    resp = client.audio.transcriptions.create(
        model=TRANSCRIBE_MODEL,
        file=f,
        response_format="text"
    )
    return resp

async def call_llm(text: str) -> LLMResult:
    messages = [{"role": "system", "content": PROMPTS.system}] + PROMPTS.fewshot + [
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
    buttons = [
        InlineKeyboardButton(opt.label, callback_data=f"pick|{opt.iso_datetime}")
        for opt in options
    ]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)

# =====================
# Handlers
# =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я тут. Напиши что и когда напомнить.")

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
    text = update.message.text.strip()
    result = await call_llm(text)
    await route_llm_result(update, context, result)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.voice.get_file()
    file_bytes = await file.download_as_bytearray()
    text = await transcribe_voice(file_bytes, filename="telegram_voice.ogg")
    result = await call_llm(text)
    await route_llm_result(update, context, result)

async def route_llm_result(update: Update, context: ContextTypes.DEFAULT_TYPE, result: LLMResult):
    if result.intent == "create_reminder" and result.fixed_datetime:
        await update.message.reply_text(f"Окей, напомню {fmt_dt(result.fixed_datetime)}")
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
        await query.answer()  # убрать спиннер
        if "|" in data:
            _, iso = data.split("|", 1)
        elif "::" in data:
            _, iso = data.split("::", 1)
        else:
            iso = data
        await query.edit_message_text(f"Окей, напомню {fmt_dt(iso)}")
        # TODO: здесь — сохранение и постановка планировки
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reload", reload_prompts))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_pick))  # ловим все callback_query

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
