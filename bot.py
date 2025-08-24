import os
import io
import json
import asyncio
import tempfile
import subprocess
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command

import aiohttp

# ---- OpenAI SDK (новый клиент)
try:
    from openai import OpenAI
except ImportError:
    # если у тебя старый пакет openai, напомни себе: pip install --upgrade openai
    from openai import OpenAI  # type: ignore


# =============== ENV ===============
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

PROMPTS_URL = os.getenv("PROMPTS_URL", "").strip()  # raw-ссылка на prompts.yaml (или .json)
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "")  # можно не указывать, попробуем найти сами

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

client = OpenAI(api_key=OPENAI_API_KEY)

# =============== ПРОМТЫ ===============
PROMPTS: Dict[str, Any] = {
    "parse_system": "PARSE PROMPT NOT LOADED. Use /reload or set PROMPTS_URL.",
    "critique_system": "CRITIQUE PROMPT NOT LOADED. Use /reload or set PROMPTS_URL.",
    "fewshot": []
}

async def fetch_text(url: str, timeout: int = 10) -> str:
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, timeout=timeout) as r:
            r.raise_for_status()
            return await r.text()

def _yaml_or_json_load(text: str) -> Dict[str, Any]:
    # Пробуем YAML, затем JSON
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception:
        return json.loads(text)

async def load_prompts() -> None:
    """Грузим промты из внешнего файла (yaml/json) по ссылке PROMPTS_URL."""
    global PROMPTS
    if not PROMPTS_URL:
        return
    try:
        raw = await fetch_text(PROMPTS_URL)
        data = _yaml_or_json_load(raw) or {}
        parse_sys = (
            data.get("parse", {}).get("system") or
            data.get("parse_system") or PROMPTS["parse_system"]
        )
        critique_sys = (
            data.get("critique", {}).get("system") or
            data.get("critique_system") or PROMPTS["critique_system"]
        )
        fewshot = data.get("parse", {}).get("fewshot", []) or data.get("fewshot", [])
        PROMPTS.update({
            "parse_system": parse_sys,
            "critique_system": critique_sys,
            "fewshot": fewshot
        })
        print("[prompts] loaded OK")
    except Exception as e:
        print("[prompts] load failed:", e)

@router.message(Command("reload"))
async def cmd_reload(m: Message):
    await load_prompts()
    await m.answer("Промты перезагружены ✅")

# =============== FFMPEG/VOICE ===============
def resolve_ffmpeg_path() -> Optional[str]:
    if FFMPEG_PATH and os.path.isfile(FFMPEG_PATH):
        return FFMPEG_PATH
    for p in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"):
        if os.path.isfile(p):
            return p
    return None

FFMPEG_PATH = resolve_ffmpeg_path()
if FFMPEG_PATH:
    print(f"[init] Using ffmpeg at: {FFMPEG_PATH}")
else:
    print("[init] ffmpeg not found — voice will still try, but conversion may fail.")

async def download_oga_to_wav(message: Message) -> Optional[bytes]:
    """Скачиваем .oga(.ogg) из Телеграма и при необходимости конвертируем в WAV."""
    if not message.voice and not message.audio:
        return None
    file_obj = message.voice or message.audio
    tg_file = await bot.get_file(file_obj.file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"

    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as r:
            r.raise_for_status()
            ogg_bytes = await r.read()

    # Попробуем напрямую отдать whisper-у .ogg — некоторые клиенты его принимают.
    # Если не взлетит — переконвертим через ffmpeg в wav.
    try:
        return ogg_bytes  # попробуем без конвертации; если API не примет — перейдем к ffmpeg
    except Exception:
        pass

    if not FFMPEG_PATH:
        return None

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as fin, \
         tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as fout:
        fin.write(ogg_bytes)
        fin.flush()
        cmd = [
            FFMPEG_PATH, "-y", "-i", fin.name,
            "-ac", "1", "-ar", "16000", "-f", "wav", fout.name
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        if os.path.isfile(fout.name):
            return open(fout.name, "rb").read()
    return None

# =============== OPENAI HELPERS ===============
async def transcribe_bytes(audio_bytes: bytes, filename: str = "audio.ogg") -> Optional[str]:
    """
    Пробуем отправить в OpenAI ASR. Сначала gpt-4o-mini-transcribe, если нет — whisper-1.
    """
    try:
        # gpt-4o-mini-transcribe
        fileobj = io.BytesIO(audio_bytes); fileobj.name = filename
        resp = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=fileobj
        )
        txt = resp.text.strip()
        if txt:
            return txt
    except Exception:
        pass
    try:
        # whisper-1
        fileobj = io.BytesIO(audio_bytes); fileobj.name = filename
        resp = client.audio.transcriptions.create(
            model="whisper-1",
            file=fileobj
        )
        txt = resp.text.strip()
        if txt:
            return txt
    except Exception as e:
        print("[whisper] error:", e)
    return None

def openai_json(messages, response_format: str = "json_object") -> Dict[str, Any]:
    """
    Вызов чата с требованием JSON-ответа.
    """
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.1,
        response_format={"type": response_format},
        messages=messages
    )
    content = resp.choices[0].message.content
    try:
        return json.loads(content)
    except Exception:
        return {"ok": False, "error": "bad_json", "raw": content}

async def run_parse_and_critique(user_text: str, now_local: str, user_tz: str) -> Dict[str, Any]:
    """
    2 шага: PARSE -> CRITIQUE. Промты грузим извне.
    """
    # few-shot (если есть)
    few = PROMPTS.get("fewshot") or []
    parse_msgs = [{"role": "system", "content": PROMPTS["parse_system"]}]
    for shot in few:
        u = shot.get("user", "")
        a = shot.get("assistant", "")
        if u and a:
            parse_msgs.append({"role": "user", "content": u})
            parse_msgs.append({"role": "assistant", "content": a})

    parse_msgs.append({"role": "user", "content": json.dumps({
        "user_text": user_text,
        "now_local": now_local,
        "user_tz": user_tz,
        "locale": "ru-RU"
    }, ensure_ascii=False)})

    draft = openai_json(parse_msgs)

    critique_msgs = [
        {"role": "system", "content": PROMPTS["critique_system"]},
        {"role": "user", "content": json.dumps({
            "user_text": user_text,
            "now_local": now_local,
            "user_tz": user_tz,
            "draft": draft
        }, ensure_ascii=False)}
    ]
    final = openai_json(critique_msgs)
    return final

# =============== HANDLERS ===============
@router.message(Command("start"))
async def cmd_start(m: Message):
    await load_prompts()
    await m.answer(
        "Привет! Я принимаю текст и голосовые, пересылаю их ИИ и возвращаю результат (JSON).\n"
        "Промты лежат отдельно и подгружаются по /reload.\n\n"
        "Отправь фразу вида: «падел в следующий понедельник в 15»."
    )

@router.message(F.voice | F.audio)
async def on_voice(m: Message):
    await m.reply("Голосовое сообщение")
    audio = await download_oga_to_wav(m)
    if not audio:
        await m.answer("Не смог обработать аудио (конвертация). Проверь ffmpeg и попробуй ещё раз.")
        return
    txt = await transcribe_bytes(audio, filename="voice.ogg")
    if not txt:
        await m.answer("Whisper не принял файл. Попробуй ещё раз.")
        return

    # now_local и user_tz — упростим для демо:
    # ты можешь хранить TZ юзера отдельно; сейчас — дефолт Europe/Moscow
    final = await run_parse_and_critique(txt, now_local="NOW_LOCAL", user_tz="Europe/Moscow")
    pretty = json.dumps(final, ensure_ascii=False, indent=2)
    await m.answer(f"🗣 Распознал: {txt}\n\n```json\n{pretty}\n```", parse_mode="Markdown")

@router.message(F.text)
async def on_text(m: Message):
    text = (m.text or "").strip()
    if not text:
        return
    final = await run_parse_and_critique(text, now_local="NOW_LOCAL", user_tz="Europe/Moscow")
    pretty = json.dumps(final, ensure_ascii=False, indent=2)
    await m.answer(f"```json\n{pretty}\n```", parse_mode="Markdown")

async def main():
    await load_prompts()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
