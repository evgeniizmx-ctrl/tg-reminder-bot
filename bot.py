# bot.py
import os
import re
import json
import shutil
import tempfile
import asyncio
import platform
from datetime import datetime, timedelta
import pytz

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ========= OpenAI (LLM + Whisper) =========
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")   # для парсинга текста
WHISPER_MODEL  = os.getenv("WHISPER_MODEL", "whisper-1")    # STT

# ========= Telegram / TZ =========
BOT_TOKEN    = os.getenv("BOT_TOKEN")
BASE_TZ_NAME = os.getenv("APP_TZ", "Europe/Moscow")
BASE_TZ      = pytz.timezone(BASE_TZ_NAME)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

# маршрутизаторы
router = Router()
voice_router = Router()
dp.include_router(router)
dp.include_router(voice_router)

scheduler = AsyncIOScheduler(timezone=BASE_TZ)

# ========= In-memory (MVP) =========
REMINDERS: list[dict] = []             # {"user_id","text","remind_dt","repeat"}
PENDING: dict[int, dict] = {}          # {"description","candidates":[iso,...]}
USER_TZS: dict[int, str] = {}          # user_id -> IANA | "UTC+<minutes>"

# ========= FFmpeg path resolve + smoke =========
def resolve_ffmpeg_path() -> str:
    """
    Ищем рабочий ffmpeg:
    1) если FFMPEG_PATH задан и существует — используем его (иначе игнорируем);
    2) which ffmpeg (в Railway/Nixpacks обычно /nix/store/.../bin/ffmpeg);
    3) стандартные пути.
    """
    candidates = []
    env = os.getenv("FFMPEG_PATH")
    if env:
        candidates.append(env)

    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)

    candidates += ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]

    for p in candidates:
        if p and os.path.exists(p) and os.access(p, os.X_OK):
            return os.path.realpath(p)

    raise FileNotFoundError(
        "ffmpeg not found. Установи ffmpeg (Railway: NIXPACKS_PKGS=ffmpeg) "
        "или добавь его в PATH."
    )

FFMPEG_PATH = resolve_ffmpeg_path()
print(f"[init] Using ffmpeg at: {FFMPEG_PATH}")

async def _smoke_ffmpeg():
    """Пробный запуск ffmpeg -version; если не вышло — падаем с понятной ошибкой."""
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_PATH, "-version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg smoke failed (code={proc.returncode})\n"
            f"{(err or b'').decode(errors='ignore')[:400]}"
        )
    print("[init] ffmpeg ok:", (out or b"").decode(errors="ignore").splitlines()[0])

# ========= TZ helpers =========
RU_TZ_CHOICES = [
    ("Калининград (+2)",  "Europe/Kaliningrad",  2),
    ("Москва (+3)",       "Europe/Moscow",       3),
    ("Самара (+4)",       "Europe/Samara",       4),
    ("Екатеринбург (+5)", "Asia/Yekaterinburg",  5),
    ("Омск (+6)",         "Asia/Omsk",           6),
    ("Новосибирск (+7)",  "Asia/Novosibirsk",    7),
    ("Иркутск (+8)",      "Asia/Irkutsk",        8),
    ("Якутск (+9)",       "Asia/Yakutsk",        9),
    ("Хабаровск (+10)",   "Asia/Vladivostok",   10),
]

def tz_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"settz|{iana}")]
            for (label, iana, _off) in RU_TZ_CHOICES]
    rows.append([InlineKeyboardButton(text="Ввести смещение (+/-часы)", callback_data="settz|ASK_OFFSET")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

OFFSET_FLEX_RX = re.compile(r"^[+-]?\s*(\d{1,2})(?::\s*([0-5]\d))?$")

def parse_user_tz_string(s: str):
    s = (s or "").strip()
    # IANA?
    try:
        return pytz.timezone(s)
    except Exception:
        pass
    # +HH[:MM]
    m = OFFSET_FLEX_RX.match(s)
    if not m:
        return None
    sign = -1 if s.strip().startswith("-") else +1
    hh = int(m.group(1)); mm = int(m.group(2) or 0)
    if hh > 23:
        return None
    return pytz.FixedOffset(sign * (hh * 60 + mm))

def get_user_tz(uid: int):
    name = USER_TZS.get(uid)
    if not name:
        return BASE_TZ
    if name.startswith("UTC+"):
        return pytz.FixedOffset(int(name[4:]))
    return pytz.timezone(name)

def store_user_tz(uid: int, tzobj):
    zone = getattr(tzobj, "zone", None)
    if isinstance(zone, str):
        USER_TZS[uid] = zone
    else:
        ofs_min = int(tzobj.utcoffset(datetime.utcnow()).total_seconds() // 60)
        USER_TZS[uid] = f"UTC+{ofs_min}"

def need_tz(uid: int) -> bool:
    return uid not in USER_TZS

async def ask_tz(m: Message):
    await m.answer(
        "Для начала укажи свой часовой пояс.\n"
        "Выбери из списка или введи либо смещение формата +03:00.",
        reply_markup=tz_kb()
    )

# ========= Общие helpers =========
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "", flags=re.UNICODE).strip()

def clean_desc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", s, flags=re.I)
    s = re.sub(r"^(о|про|насч[её]т)\s+", "", s, flags=re.I)
    return s.strip() or "Напоминание"

def fmt_dt_local(dt: datetime) -> str:
    return f"{dt.strftime('%d.%m')} в {dt.strftime('%H:%M')}"

def as_local_for(uid: int, dt_iso: str) -> datetime:
    user_tz = get_user_tz(uid)
    dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = user_tz.localize(dt)
    else:
        dt = dt.astimezone(user_tz)
    return dt

def kb_variants_for(uid: int, dt_isos: list[str]) -> InlineKeyboardMarkup:
    dts = sorted(as_local_for(uid, x) for x in dt_isos)
    def label(dt: datetime) -> str:
        now = datetime.now(get_user_tz(uid))
        if dt.date() == now.date():
            d = "Сегодня"
        elif dt.date() == (now + timedelta(days=1)).date():
            d = "Завтра"
        else:
            d = dt.strftime("%d.%m")
        return f"{d} в {dt.strftime('%H:%M')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label(dt), callback_data=f"time|{dt.isoformat()}")] for dt in dts]
    )

def plan(rem):
    scheduler.add_job(send_reminder, "date", run_date=rem["remind_dt"], args=[rem["user_id"], rem["text"]])

async def send_reminder(uid: int, text: str):
    try:
        await bot.send_message(uid, f"🔔🔔 {text}")
    except Exception as e:
        print("send_reminder error:", e)

# ========= LLM-парсер =========
SYSTEM_PROMPT = """Ты — интеллектуальный парсер напоминаний на русском языке.
Возвращай строго JSON:
{
  "ok": true|false,
  "description": "строка",
  "datetimes": ["ISO8601", ...],
  "need_clarification": true|false,
  "clarify_type": "time|date|both|none",
  "reason": "строка"
}
Понимай разговорные формы; если часы двусмысленны — верни 2 кандидата (06:00 и 18:00).
Если только дата — попроси время. Если только время — поставь на ближайшее будущее. Описание очисти от вводных слов.
"""

def build_user_prompt(uid: int, text: str) -> str:
    tz = get_user_tz(uid)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    return json.dumps({
        "user_text": text,
        "now_local": now,
        "user_tz": getattr(tz, "zone", None) or f"UTC{tz.utcoffset(datetime.utcnow())}",
        "locale": "ru-RU"
    }, ensure_ascii=False)

async def ai_parse(uid: int, text: str) -> dict:
    if not (OpenAI and OPENAI_API_KEY):
        return {"ok": False, "description": clean_desc(text), "datetimes": [], "need_clarification": True, "clarify_type": "time", "reason": "LLM disabled"}
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        rsp = await asyncio.to_thread(
            client.chat.completions.create,
            model=OPENAI_MODEL,
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(uid, text)}
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(rsp.choices[0].message.content)
        data.setdefault("ok", False)
        data.setdefault("description", clean_desc(text))
        data.setdefault("datetimes", [])
        data.setdefault("need_clarification", not data.get("ok"))
        data.setdefault("clarify_type", "time" if not data.get("ok") else "none")
        return data
    except Exception as e:
        print("ai_parse error:", e)
        return {"ok": False, "description": clean_desc(text), "datetimes": [], "need_clarification": True, "clarify_type": "time", "reason": "LLM error"}

# ========= Команды =========
@router.message(Command("start"))
async def cmd_start(m: Message):
    if need_tz(m.from_user.id):
        await ask_tz(m)
    else:
        await m.answer(
            "Готов работать. Пиши: «завтра в полтретьего падел», «через 2 часа чай», «сегодня в 1710 отчёт».\n"
            "/tz — сменить пояс, /list — список, /cancel — отменить уточнение."
        )

@router.message(Command("tz"))
async def cmd_tz(m: Message):
    await ask_tz(m)

@router.message(Command("list"))
async def cmd_list(m: Message):
    uid = m.from_user.id
    items = [r for r in REMINDERS if r["user_id"] == uid]
    if not items:
        await m.answer("Пока нет напоминаний (в этой сессии)."); return
    items = sorted(items, key=lambda r: r["remind_dt"])
    lines = [f"• {r['text']} — {fmt_dt_local(r['remind_dt'])}" for r in items]
    await m.answer("\n".join(lines))

@router.message(Command("cancel"))
async def cmd_cancel(m: Message):
    uid = m.from_user.id
    if uid in PENDING:
        PENDING.pop(uid, None)
        await m.reply("Ок, отменил уточнение. Пиши новое напоминание.")
    else:
        await m.reply("Нечего отменять.")

@router.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong ✅")

@router.message(Command("debug"))
async def cmd_debug(m: Message):
    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_PATH, "-version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        ff_line = (out or b"").decode(errors="ignore").splitlines()[0] if proc.returncode == 0 else (err or b"").decode(errors="ignore")[:120]
    except Exception as e:
        ff_line = f"error: {e}"

    await m.answer(
        "🔎 DEBUG\n"
        f"TZ(default): {BASE_TZ.zone}\n"
        f"FFMPEG_PATH: {FFMPEG_PATH}\n"
        f"ffmpeg: {ff_line}\n"
        f"OPENAI_API_KEY: {'set' if OPENAI_API_KEY else 'MISSING'}\n"
        f"Python: {platform.python_version()}"
    )

# ========= Текст =========
@router.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    text = norm(m.text)

    if need_tz(uid):
        tz_obj = parse_user_tz_string(text)
        if tz_obj:
            store_user_tz(uid, tz_obj)
            await m.reply("Часовой пояс сохранён. Пиши напоминание, например: «завтра в 19 отчёт».")
            return
        await ask_tz(m); return

    data = await ai_parse(uid, text)
    desc = clean_desc(data.get("description") or text)

    if data.get("ok") and data.get("datetimes"):
        dt = as_local_for(uid, data["datetimes"][0])
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
        plan(REMINDERS[-1])
        await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}")
        return

    cands = data.get("datetimes", [])
    if len(cands) >= 2:
        PENDING[uid] = {"description": desc, "candidates": cands}
        await m.reply(f"Уточни время для «{desc}»", reply_markup=kb_variants_for(uid, cands))
        return

    if data.get("need_clarification", True):
        PENDING[uid] = {"description": desc}
        await m.reply(f"Окей, «{desc}». Уточни дату/время.")
        return

    await m.reply("Не понял. Скажи, когда напомнить (например: «завтра в 19 отчёт»).")

# ========= Callback'и =========
@router.callback_query(F.data.startswith("settz|"))
async def cb_settz(cb: CallbackQuery):
    uid = cb.from_user.id
    _, payload = cb.data.split("|", 1)
    if payload == "ASK_OFFSET":
        await cb.message.answer("Введи смещение: +03:00, +3:00, +3, 3, 03 или IANA (Europe/Moscow).")
        await cb.answer(); return
    tz_obj = parse_user_tz_string(payload)
    if tz_obj is None:
        await cb.answer("Не понял часовой пояс", show_alert=True); return
    store_user_tz(uid, tz_obj)
    try:
        await cb.message.edit_text("Часовой пояс сохранён. Пиши напоминание ✍️")
    except Exception:
        await cb.message.answer("Часовой пояс сохранён. Пиши напоминание ✍️")
    await cb.answer("OK")

@router.callback_query(F.data.startswith("time|"))
async def cb_time(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in PENDING or not PENDING[uid].get("candidates"):
        await cb.answer("Нет активного уточнения"); return
    iso = cb.data.split("|", 1)[1]
    dt = as_local_for(uid, iso)
    desc = PENDING[uid].get("description","Напоминание")
    PENDING.pop(uid, None)
    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
    plan(REMINDERS[-1])
    try:
        await cb.message.edit_text(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}")
    except Exception:
        await cb.message.answer(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}")
    await cb.answer("Установлено ✅")

# ========= Голос / Аудио (Whisper) =========
oa_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY")) if OpenAI else None

async def ogg_to_wav(src_ogg: str, dst_wav: str) -> None:
    """OGG/OPUS -> WAV 16kHz mono с подробным stderr."""
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_PATH, "-nostdin", "-loglevel", "error",
        "-y", "-i", src_ogg, "-ac", "1", "-ar", "16000", dst_wav,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exit={proc.returncode}\n{(err or b'').decode(errors='ignore')[:800]}")

async def transcribe_file_to_text(path: str, lang: str = "ru") -> str:
    if not oa_client:
        raise RuntimeError("OpenAI client not initialized")
    loop = asyncio.get_running_loop()
    def _run():
        with open(path, "rb") as f:
            r = oa_client.audio.transcriptions.create(
                model=WHISPER_MODEL, file=f, language=lang
            )
        return (r.text or "").strip()
    return await loop.run_in_executor(None, _run)

@voice_router.message(F.voice)
async def on_voice(m: Message):
    uid = m.from_user.id
    if need_tz(uid):
        await ask_tz(m); return

    tg_file = await m.bot.get_file(m.voice.file_id)
    with tempfile.TemporaryDirectory() as tmpd:
        ogg_path = f"{tmpd}/in.ogg"
        wav_path = f"{tmpd}/in.wav"

        await m.bot.download(tg_file, destination=ogg_path)

        size = os.path.getsize(ogg_path)
        print(f"[voice] downloaded OGG size={size} bytes")
        if size == 0:
            await m.reply("Файл скачался пустым (0 байт). Отправь голосовое ещё раз."); return

        try:
            await ogg_to_wav(ogg_path, wav_path)
        except Exception as e:
            err = str(e)
            print("[FFMPEG ERROR]", err)
            snippet = "\n".join(err.splitlines()[:4])
            await m.reply("Не смог обработать аудио (конвертация).\n" + snippet)
            return

        await m.chat.do("typing")
        try:
            text = await transcribe_file_to_text(wav_path, lang="ru")
        except Exception as e:
            print("[WHISPER ERROR]", e)
            await m.reply("Whisper не принял файл. Попробуй ещё раз."); return

    if not text:
        await m.reply("Пустая расшифровка — повтори, пожалуйста."); return

    data = await ai_parse(uid, text)
    desc = clean_desc(data.get("description") or text)

    if data.get("ok") and data.get("datetimes"):
        dt = as_local_for(uid, data["datetimes"][0])
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
        plan(REMINDERS[-1])
        await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}")
        return

    await m.reply(f"Окей, «{desc}». Уточни дату и время.")

@voice_router.message(F.audio)
async def on_audio(m: Message):
    """Обычные аудио (mp3/m4a/webm) — без ffmpeg, для проверки канала до Whisper."""
    uid = m.from_user.id
    if need_tz(uid):
        await ask_tz(m); return

    file = await m.bot.get_file(m.audio.file_id)
    with tempfile.TemporaryDirectory() as tmpd:
        path = f"{tmpd}/{m.audio.file_unique_id}"
        await m.bot.download(file, destination=path)

        size = os.path.getsize(path)
        print(f"[audio] downloaded size={size} bytes")
        if size == 0:
            await m.reply("Аудио скачалось пустым (0 байт)."); return

        await m.chat.do("typing")
        try:
            text = await transcribe_file_to_text(path, lang="ru")
        except Exception as e:
            print("[WHISPER AUDIO ERROR]", e)
            await m.reply("Whisper не принял файл."); return

    if not text:
        await m.reply("Пустая расшифровка."); return

    data = await ai_parse(uid, text)
    desc = clean_desc(data.get("description") or text)

    if data.get("ok") and data.get("datetimes"):
        dt = as_local_for(uid, data["datetimes"][0])
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
        plan(REMINDERS[-1])
        await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}")
        return

    await m.reply(f"Окей, «{desc}». Уточни дату и время.")

# ========= RUN =========
async def main():
    await _smoke_ffmpeg()   # проверим ffmpeg до запуска бота
    scheduler.start()
    print("✅ bot is polling")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
