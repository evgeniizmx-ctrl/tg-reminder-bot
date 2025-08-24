# bot.py — умный NLU + аккуратные описания, кнопки только при реальной двусмысленности
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
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ========= OpenAI (LLM + Whisper) =========
try:
    from openai import OpenAI
    from openai import RateLimitError, APIStatusError, BadRequestError
except Exception:
    OpenAI = None
    RateLimitError = APIStatusError = BadRequestError = Exception

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
WHISPER_MODEL  = os.getenv("WHISPER_MODEL", "gpt-4o-mini-transcribe")

# ========= Telegram / TZ =========
BOT_TOKEN    = os.getenv("BOT_TOKEN")
BASE_TZ_NAME = os.getenv("APP_TZ", "Europe/Moscow")
BASE_TZ      = pytz.timezone(BASE_TZ_NAME)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(BOT_TOKEN)
dp  = Dispatcher()
router = Router()
voice_router = Router()
dp.include_router(router)
dp.include_router(voice_router)

scheduler = AsyncIOScheduler(timezone=BASE_TZ)

# ========= In-memory =========
REMINDERS: list[dict] = []
PENDING: dict[int, dict] = {}
USER_TZS: dict[int, str] = {}

# ========= ffmpeg =========
def try_resolve_ffmpeg() -> str | None:
    env = os.getenv("FFMPEG_PATH")
    if env and os.path.exists(env) and os.access(env, os.X_OK):
        return os.path.realpath(env)
    found = shutil.which("ffmpeg")
    if found and os.path.exists(found) and os.access(found, os.X_OK):
        return os.path.realpath(found)
    for p in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return os.path.realpath(p)
    return None

FFMPEG_PATH = try_resolve_ffmpeg()
if FFMPEG_PATH:
    print(f"[init] Using ffmpeg at: {FFMPEG_PATH}")
else:
    print("[init] ffmpeg not found — voice features disabled (text reminders still work).")

async def _smoke_ffmpeg():
    if not FFMPEG_PATH:
        return
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
    try:
        return pytz.timezone(s)
    except Exception:
        pass
    m = OFFSET_FLEX_RX.match(s)
    if not m: return None
    sign = -1 if s.strip().startswith("-") else +1
    hh = int(m.group(1)); mm = int(m.group(2) or 0)
    if hh > 23: return None
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
        if dt.date() == now.date(): d = "Сегодня"
        elif dt.date() == (now + timedelta(days=1)).date(): d = "Завтра"
        else: d = dt.strftime("%d.%m")
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

# ========= LLM-парсер (умный) =========
SYSTEM_PROMPT = """Ты — умный парсер напоминаний на русском. Верни строго JSON по схеме:
{
  "ok": true|false,
  "description": "string",           // краткое ДЕЛО без даты/времени/слов «сегодня/завтра» и без слова «Напоминание»
  "datetimes": ["ISO8601", ...],     // 1 время — когда всё однозначно, РОВНО 2 времени — только если двусмысленно (например «в 4»)
  "need_clarification": true|false,  // true если времени нет или дата/время неполные
  "clarify_type": "time|date|both|none",
  "reason": "string"                 // коротко почему нужна ясность
}
ВАЖНО:
- Не добавляй фразы вроде «Напоминание на…». Описание — только смысл задачи (например: «падел с Никитой», «позвонить маме»).
- Не пиши время/дату в description. Не переводись числа в слова: «11:30», а не «одиннадцать тридцать».
- Понимай разговорные формы, ошибки («щавтра»=«завтра», «падел»=допустимо как есть), «через 20 минут», «полтретьего», «без пяти пять».
- Если время с меридианом (утра/дня/вечера/ночи) — верни ОДНО время с соответствующим часом.
- Если 24-часовой формат («17:30», «1730», «08:05») — верни ОДНО время ровно так.
- Только «в H» (H 1..12) без меридиана — верни ДВА кандидата: H:00 и (H+12):00 одной даты.
- Используй now_local и user_tz из пользователя, чтобы вычислить дату (например «завтра»).
"""

FEW_SHOTS = [
    {
        "user_text": "завтра падел в 11:30",
        "now_local": "2025-08-24 12:00:00",
        "user_tz": "Europe/Moscow",
        "expect": {
            "ok": True, "description": "падел", "need_clarification": False,
            "datetimes": ["2025-08-25T11:30:00+03:00"]
        }
    },
    {
        "user_text": "в 1730 щавтра падел",
        "now_local": "2025-08-24 12:00:00",
        "user_tz": "Europe/Moscow",
        "expect": {
            "ok": True, "description": "падел",
            "datetimes": ["2025-08-25T17:30:00+03:00"]
        }
    },
    {
        "user_text": "завтра в 4 встреча",
        "now_local": "2025-08-24 12:00:00",
        "user_tz": "Europe/Moscow",
        "expect": {
            "ok": True, "description": "встреча",
            "datetimes": ["2025-08-25T04:00:00+03:00","2025-08-25T16:00:00+03:00"]
        }
    },
    {
        "user_text": "через 20 минут кол",
        "now_local": "2025-08-24 12:00:00",
        "user_tz": "Europe/Moscow",
        "expect": {
            "ok": True, "description": "кол",
            "datetimes": ["2025-08-24T12:20:00+03:00"]
        }
    }
]

def build_user_prompt(uid: int, text: str) -> list[dict]:
    tz = get_user_tz(uid)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    base = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({
            "user_text": text,
            "now_local": now,
            "user_tz": getattr(tz, "zone", None) or "UTC",
            "locale": "ru-RU"
        }, ensure_ascii=False)}
    ]
    # мини few-shot для стабилизации поведения
    for ex in FEW_SHOTS:
        base.append({"role": "user", "content": json.dumps(ex, ensure_ascii=False)})
        base.append({"role": "assistant", "content": json.dumps(ex["expect"], ensure_ascii=False)})
    return base

async def ai_parse(uid: int, text: str) -> dict:
    if not (OpenAI and OPENAI_API_KEY):
        return {"ok": False, "description": text, "datetimes": [], "need_clarification": True, "clarify_type": "time", "reason": "LLM disabled"}
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        rsp = await asyncio.to_thread(
            client.chat.completions.create,
            model=OPENAI_MODEL,
            temperature=0.1,
            messages=build_user_prompt(uid, text),
            response_format={"type": "json_object"},
        )
        data = json.loads(rsp.choices[0].message.content)
        data.setdefault("ok", False)
        data.setdefault("description", text)
        data.setdefault("datetimes", [])
        data.setdefault("need_clarification", not data.get("ok"))
        data.setdefault("clarify_type", "time" if not data.get("ok") else "none")
        return data
    except Exception as e:
        print("ai_parse error:", e)
        return {"ok": False, "description": text, "datetimes": [], "need_clarification": True, "clarify_type": "time", "reason": "LLM error"}

# ========= Сжатие двусмысленности (страховки) =========
MERIDIEM_RX = re.compile(
    r"\b(?P<h>\d{1,2})\s*(?:час(?:а|ов)?)?\s*(?P<mer>утра|утром|дня|днём|днем|вечера|вечером|ночи|ночью)\b",
    re.I | re.U
)
def _meridiem_target_hour(h: int, mer: str) -> int:
    m = mer.lower()
    if m.startswith("утр"):  return 0 if h == 12 else h % 12
    if m.startswith("дн"):   return (h % 12) + 12
    if m.startswith("веч"):  return (h % 12) + 12
    return 0 if h == 12 else h % 12  # ночь

def collapse_by_meridiem(uid: int, text: str, dt_isos: list[str]) -> list[str]:
    m = MERIDIEM_RX.search(text or "")
    if not m or not dt_isos: return dt_isos
    try: h = int(m.group("h"))
    except Exception: return dt_isos
    target_h = _meridiem_target_hour(h, m.group("mer"))
    for iso in dt_isos:
        dt = as_local_for(uid, iso)
        if dt.hour == target_h:
            return [iso]
    base = as_local_for(uid, dt_isos[0])
    fixed = base.replace(hour=target_h, minute=0, second=0, microsecond=0)
    return [fixed.isoformat()]

COMPACT_24H_RX = re.compile(
    r"(?<!\d)(?P<h>[01]?\d|2[0-3])(?:[:.\s]?(?P<m>[0-5]\d))\b",
    re.I | re.U,
)
def collapse_by_24h(uid: int, text: str, dt_isos: list[str]) -> list[str]:
    m = COMPACT_24H_RX.search(text or "")
    if not m or not dt_isos: return dt_isos
    h = int(m.group("h")); mm = int(m.group("m") or 0)
    for iso in dt_isos:
        dt = as_local_for(uid, iso)
        if dt.hour == h and dt.minute == mm:
            return [iso]
    base = as_local_for(uid, dt_isos[0])
    fixed = base.replace(hour=h, minute=mm, second=0, microsecond=0)
    return [fixed.isoformat()]

# ========= Команды =========
@router.message(Command("start"))
async def cmd_start(m: Message):
    if need_tz(m.from_user.id):
        await ask_tz(m)
    else:
        await m.answer(
            "Готов. Пиши: «завтра в 11:30 падел», «через 20 минут созвон», «завтра в 4».\n"
            "/tz — сменить пояс, /list — список, /cancel — отменить уточнение."
        )

@router.message(Command("tz"))
async def cmd_tz(m: Message): await ask_tz(m)

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
async def cmd_ping(m: Message): await m.answer("pong ✅")

@router.message(Command("debug"))
async def cmd_debug(m: Message):
    try:
        if FFMPEG_PATH:
            proc = await asyncio.create_subprocess_exec(
                FFMPEG_PATH, "-version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await proc.communicate()
            ff_line = (out or b"").decode(errors="ignore").splitlines()[0] if proc.returncode == 0 else (err or b"").decode(errors="ignore")[:120]
        else:
            ff_line = "not found"
    except Exception as e:
        ff_line = f"error: {e}"

    await m.answer(
        "🔎 DEBUG\n"
        f"TZ(default): {BASE_TZ.zone}\n"
        f"FFMPEG_PATH: {FFMPEG_PATH or 'None'}\n"
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
    desc = (data.get("description") or "").strip() or text.strip()
    cands = data.get("datetimes", [])

    # «страховки» — уменьшаем лишние вопросы
    cands = collapse_by_24h(uid, text, cands)
    cands = collapse_by_meridiem(uid, text, cands)

    if data.get("ok") and len(cands) >= 2:
        PENDING[uid] = {"description": desc, "candidates": cands}
        await m.reply(f"Уточни время для «{desc}»", reply_markup=kb_variants_for(uid, cands))
        return

    if data.get("ok") and len(cands) == 1:
        dt = as_local_for(uid, cands[0])
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
        plan(REMINDERS[-1])
        await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}")
        return

    if data.get("need_clarification", True):
        PENDING[uid] = {"description": desc}
        await m.reply(f"Окей, «{desc}». Уточни дату/время.")
        return

    await m.reply("Не понял. Скажи, когда напомнить (например: «завтра в 19 отчёт»).")

# ========= Callback’и =========
@router.callback_query(F.data.startswith("settz|"))
async def cb_settz(cb: CallbackQuery):
    uid = cb.from_user.id
    _, payload = cb.data.split("|", 1)
    if payload == "ASK_OFFSET":
        try:
            await cb.message.answer("Введи смещение: +03:00, +3:00, +3, 3, 03 или IANA (Europe/Moscow).")
            await cb.answer()
        except TelegramBadRequest:
            pass
        return
    tz_obj = parse_user_tz_string(payload)
    if tz_obj is None:
        try: await cb.answer("Не понял часовой пояс", show_alert=True)
        except TelegramBadRequest: pass
        return
    store_user_tz(uid, tz_obj)
    try:    await cb.message.edit_text("Часовой пояс сохранён. Пиши напоминание ✍️")
    except TelegramBadRequest:
        try: await cb.message.answer("Часовой пояс сохранён. Пиши напоминание ✍️")
        except TelegramBadRequest: pass
    try: await cb.answer("OK")
    except TelegramBadRequest: pass

@router.callback_query(F.data.startswith("time|"))
async def cb_time(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in PENDING or not PENDING[uid].get("candidates"):
        try: await cb.answer("Нет активного уточнения")
        except TelegramBadRequest: pass
        return
    iso = cb.data.split("|", 1)[1]
    dt = as_local_for(uid, iso)
    desc = PENDING[uid].get("description","Напоминание")
    PENDING.pop(uid, None)
    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
    plan(REMINDERS[-1])

    try:    await cb.message.edit_text(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}")
    except TelegramBadRequest:
        try: await cb.message.answer(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}")
        except TelegramBadRequest: pass
    try: await cb.answer("Установлено ✅")
    except TelegramBadRequest: pass

# ========= Голос / Аудио (Whisper) =========
oa_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY")) if OpenAI else None

async def ogg_to_wav(src_ogg: str, dst_wav: str) -> None:
    if not FFMPEG_PATH: raise RuntimeError("ffmpeg not available")
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_PATH, "-nostdin", "-loglevel", "error",
        "-y", "-i", src_ogg, "-ac", "1", "-ar", "16000", dst_wav,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exit={proc.returncode}\n{(err or b'').decode(errors='ignore')[:800]}")

async def transcribe_file_to_text(path: str, lang: str = "ru") -> str:
    if not oa_client: raise RuntimeError("OpenAI client not initialized")
    loop = asyncio.get_running_loop()
    def _run():
        with open(path, "rb") as f:
            return oa_client.audio.transcriptions.create(
                model=WHISPER_MODEL, file=f, language=lang
            )
    try:
        r = await loop.run_in_executor(None, _run)
        return (r.text or "").strip()
    except RateLimitError:
        raise RuntimeError("QUOTA_EXCEEDED")
    except APIStatusError as e:
        raise RuntimeError(f"API_STATUS_{getattr(e, 'status', 'NA')}")
    except BadRequestError as e:
        raise RuntimeError(f"BAD_REQUEST_{getattr(e, 'message', 'unknown')}")

@voice_router.message(F.voice)
async def on_voice(m: Message):
    if not FFMPEG_PATH:
        await m.reply("Голосовые временно недоступны (ffmpeg не установлен на сервере). Текст — работает.")
        return
    uid = m.from_user.id
    if need_tz(uid): await ask_tz(m); return

    file = await m.bot.get_file(m.voice.file_id)
    with tempfile.TemporaryDirectory() as tmpd:
        ogg_path = f"{tmpd}/in.ogg"; wav_path = f"{tmpd}/in.wav"
        await m.bot.download(file, destination=ogg_path)
        if os.path.getsize(ogg_path) == 0:
            await m.reply("Файл скачался пустым. Отправь голосовое ещё раз."); return
        try:    await ogg_to_wav(ogg_path, wav_path)
        except Exception:
            await m.reply("Не смог обработать аудио (конвертация)."); return
        await m.chat.do("typing")
        try:    text = await transcribe_file_to_text(wav_path, lang="ru")
        except RuntimeError:
            await m.reply("Whisper не принял файл или квота исчерпана."); return

    if not text:
        await m.reply("Пустая расшифровка — повтори, пожалуйста."); return

    data = await ai_parse(uid, text)
    desc = (data.get("description") or "").strip() or text.strip()
    cands = collapse_by_24h(uid, text, data.get("datetimes", []))
    cands = collapse_by_meridiem(uid, text, cands)

    if data.get("ok") and len(cands) >= 2:
        PENDING[uid] = {"description": desc, "candidates": cands}
        await m.reply(f"Уточни время для «{desc}»", reply_markup=kb_variants_for(uid, cands)); return

    if data.get("ok") and len(cands) == 1:
        dt = as_local_for(uid, cands[0])
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
        await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}"); return

    await m.reply(f"Окей, «{desc}». Уточни дату и время.")

@voice_router.message(F.audio)
async def on_audio(m: Message):
    if not FFMPEG_PATH:
        await m.reply("Аудио временно недоступны (ffmpeg не установлен на сервере). Текст — работает.")
        return
    uid = m.from_user.id
    if need_tz(uid): await ask_tz(m); return

    file = await m.bot.get_file(m.audio.file_id)
    with tempfile.TemporaryDirectory() as tmpd:
        path = f"{tmpd}/{m.audio.file_unique_id}"
        await m.bot.download(file, destination=path)
        if os.path.getsize(path) == 0:
            await m.reply("Аудио скачалось пустым."); return
        await m.chat.do("typing")
        try:    text = await transcribe_file_to_text(path, lang="ru")
        except RuntimeError:
            await m.reply("Whisper не принял файл или квота исчерпана."); return

    if not text:
        await m.reply("Пустая расшифровка."); return

    data = await ai_parse(uid, text)
    desc = (data.get("description") or "").strip() or text.strip()
    cands = collapse_by_24h(uid, text, data.get("datetimes", []))
    cands = collapse_by_meridiem(uid, text, cands)

    if data.get("ok") and len(cands) >= 2:
        PENDING[uid] = {"description": desc, "candidates": cands}
        await m.reply(f"Уточни время для «{desc}»", reply_markup=kb_variants_for(uid, cands)); return

    if data.get("ok") and len(cands) == 1:
        dt = as_local_for(uid, cands[0])
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
        await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}"); return

    await m.reply(f"Окей, «{desc}». Уточни дату и время.")

# ========= RUN =========
async def main():
    await _smoke_ffmpeg()
    scheduler.start()
    print("✅ bot is polling")
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    asyncio.run(main())
