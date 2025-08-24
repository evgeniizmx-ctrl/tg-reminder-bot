import os
import io
import json
import yaml
import logging
from typing import List, Optional

from pydantic import BaseModel, Field, ValidationError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from openai import OpenAI

# =====================
# Config & Logging
# =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PROMPTS_PATH = os.getenv("PROMPTS_PATH", "prompts.yaml")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
TRANSCRIBE_MODEL = os.getenv("ASR_MODEL", "whisper-1")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
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
async def transcribe_voice(file_bytes: bytes, filename: str = "audio.ogg") -> str:
    f = io.BytesIO(file_bytes)
    f.name = filename
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
    buttons = [InlineKeyboardButton(opt.label, callback_data=f"pick::{opt.iso_datetime}") for opt in options]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)

# =====================
# Handlers
# =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли текст или войс с задачей, а я поставлю напоминание.")

async def reload_prompts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PROMPTS
    try:
        PROMPTS = load_prompts()
        await update.message.reply_text("Промты перезагружены ✅")
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
    text = await transcribe_voice(file_bytes)
    result = await call_llm(text)
    await route_llm_result(update, context, result)

async def route_llm_result(update: Update, context: ContextTypes.DEFAULT_TYPE, result: LLMResult):
    if result.intent == "create_reminder" and result.fixed_datetime:
        await update.message.reply_text(f"Напоминание создано: {result.title or result.text_original}\n⏰ {result.fixed_datetime}")
    elif result.intent == "ask_clarification" and result.options:
        kb = build_time_keyboard(result.options)
        await update.message.reply_text("Уточни время:", reply_markup=kb)
    else:
        await update.message.reply_text("Я не понял, попробуй ещё раз.")

async def handle_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, iso = query.data.split("::", 1)
    await query.edit_message_text(f"Напоминание создано на {iso}")

# =====================
# Main
# =====================
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reload", reload_prompts))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_pick, pattern=r"^pick::"))
    logging.info("Bot starting… polling enabled")
    app.run_polling()

if __name__ == "__main__":
    main()
