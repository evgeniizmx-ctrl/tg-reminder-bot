import os
import re
import asyncio
import json
from datetime import datetime, timedelta
import pytz

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ====== LLM client (OpenAI-style) ======
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ========= ENV / BASE TZ (fallback) =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_TZ_NAME = os.getenv("APP_TZ", "Europe/Moscow")  # дефолт только на самый старт
BASE_TZ = pytz.timezone(BASE_TZ_NAME)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Планировщик: глобальный TZ роли не играет, мы передаём aware run_date в TZ пользователя
scheduler = AsyncIOScheduler(timezone=BASE_TZ)

# ====== In-memory хранилища (MVP) ======
REMINDERS: list[dict] = []  # {"user_id", "text", "remind_dt", "repeat"}
PENDING: dict[int, dict] = {}  # уточнение: {"description", "candidates":[iso,...]}
USER_TZS: dict[int, str] = {}  # user_id -> "Europe/Moscow" | "UTC+180" (FixedOffset)

# ========= TZ helpers =========
IANA_SUGGESTIONS = [
    "Europe/Moscow", "Europe/Kyiv", "Asia/Almaty", "Asia/Dubai", "Europe/Berlin"
]

def tz_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"settz|{name}")] for name in IANA_SUGGESTIONS]
    rows.append([InlineKeyboardButton(text="Указать смещение (+03:00)", callback_data="settz|ASK_OFFSET")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

OFFSET_RX = re.compile(r"^[+-](?:0?\d|1\d|2[0-3]):[0-5]\d$")  # +03:00, -05:30

def parse_user_tz_string(s: str) -> pytz.tzinfo.BaseTzInfo | None:
    s = (s or "").strip()
    # IANA?
    try:
        return pytz.timezone(s)
    except Exception:
        pass
    # +HH:MM ?
    if OFFSET_RX.match(s):
        sign = 1 if s[0] == "+" else -1
        hh, mm = map(int, s[1:].split(":"))
        minutes = sign * (hh * 60 + mm)
        return pytz.FixedOffset(minutes)
    return None

def get_user_tz(uid: int) -> pytz.tzinfo.BaseTzInfo:
    name = USER_TZS.get(uid)
    if not name:
        return BASE_TZ
    # FixedOffset сохраняем как 'UTC+180' (минуты) или IANA имя
    if name.startswith("UTC+") or name.startswith("UTC-"):
        minutes = int(name[3:])
        return pytz.FixedOffset(minutes)
    return pytz.timezone(name)

def store_user_tz(uid: int, tzobj: pytz.tzinfo.BaseTzInfo):
    # если FixedOffset — храним минуты; если IANA — имя
    if isinstance(tzobj, pytz.tzinfo.StaticTzInfo) and getattr(tzobj, "_minutes", None) is not None:
        USER_TZS[uid] = f"UTC+{tzobj._minutes}"  # не всегда есть _minutes; fallback через utcoffset
    else:
        # попытка извлечь offset у FixedOffset
        ofs = tzobj.utcoffset(datetime.utcnow()).total_seconds() // 60
        if isinstance(tzobj, pytz.tzinfo.DstTzInfo) or isinstance(tzobj, pytz.tzinfo.BaseTzInfo) and hasattr(tzobj, "zone"):
            USER_TZS[uid] = getattr(tzobj, "zone", BASE_TZ_NAME)
        else:
            USER_TZS[uid] = f"UTC+{int(ofs)}"

# ========= Common helpers =========
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "", flags=re.UNICODE).strip()

def clean_desc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", s, flags=re.I)
    s = re.sub(r"^(о|про|насч[её]т)\s+", "", s, flags=re.I)
    return s.strip() or "Напоминание"

def fmt_dt_local(dt: datetime) -> str:
    return f"{dt.strftime('%d.%m')} в {dt.strftime('%H:%M')}"  # без часового пояса

def as_local_for(uid: int, dt_iso: str) -> datetime:
    user_tz = get_user_tz(uid)
    # допускаем ISO с 'Z' или с оффсетом
    dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = user_tz.localize(dt)
    else:
        dt = dt.astimezone(user_tz)
    return dt

def kb_variants_for(uid: int, dt_isos: list[str]) -> InlineKeyboardMarkup:
    dts = sorted(as_local_for(uid, x) for x in dt_isos)
    def human_label(dt: datetime) -> str:
        now = datetime.now(get_user_tz(uid))
        if dt.date() == now.date():
            d = "Сегодня"
        elif dt.date() == (now + timedelta(days=1)).date():
            d = "Завтра"
        else:
            d = dt.strftime("%d.%m")
        return f"{d} в {dt.strftime('%H:%M')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=human_label(dt), callback_data=f"time|{dt.isoformat()}")] for dt in dts]
    )

def plan(rem):
    # run_date — aware в TZ пользователя
    scheduler.add_job(send_reminder, "date", run_date=rem["remind_dt"], args=[rem["user_id"], rem["text"]])

async def send_reminder(uid: int, text: str):
    try:
        await bot.send_message(uid, f"🔔🔔 {text}")  # только два колокольчика и текст
    except Exception as e:
        print("send_reminder error:", e)

# ========= LLM PARSER =========
SYSTEM_PROMPT = """Ты — интеллектуальный парсер напоминаний на русском языке.
Возвращай строго JSON:
{
  "ok": true|false,
  "description": "строка",
  "datetimes": ["ISO8601", ...],   // локальная зона user_tz
  "need_clarification": true|false,
  "clarify_type": "time|date|both|none",
  "reason": "строка"
}
Правила: понимаешь разговорные формы; двусмысленные часы -> два кандидата (06:00 и 18:00).
Если только дата — попроси время. Если только время — ставь ближайшее будущее.
Описание очисти от вводных слов.
"""

def build_user_prompt(text: str, user_tz_name: str) -> str:
    now = datetime.now(getattr(pytz, 'timezone')(user_tz_name) if "/" in user_tz_name else BASE_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return json.dumps({
        "user_text": text,
        "now_local": now,
        "user_tz": user_tz_name,
        "locale": "ru-RU"
    }, ensure_ascii=False)

async def ai_parse(uid: int, text: str) -> dict:
    """LLM → JSON. При ошибке возвращаем запрос на уточнение времени."""
    user_tz_name = USER_TZS.get(uid, BASE_TZ_NAME)
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
                {"role": "user", "content": build_user_prompt(text, user_tz_name)}
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

# ========= Onboarding TZ =========
def need_tz(uid: int) -> bool:
    return uid not in USER_TZS

async def ask_tz(m: Message):
    await m.answer(
        "Прежде чем ставить напоминания, укажи свой часовой пояс.\n"
        "Выбери из списка или введи название IANA (например, Europe/Moscow) либо смещение формата +03:00.",
        reply_markup=tz_kb()
    )

# ========= COMMANDS =========
@router.message(Command("start"))
async def cmd_start(m: Message):
    if need_tz(m.from_user.id):
        await ask_tz(m)
    else:
        await m.answer(
            "Готов работать. Пиши: «завтра в полтретьего падел», «через 2 часа чай», «сегодня в 1710 отчёт».\n"
            "/tz — сменить часовой пояс, /list — список, /cancel — отменить уточнение."
        )

@router.message(Command("tz"))
async def cmd_tz(m: Message):
    await m.answer(
        "Выбери часовой пояс или введи IANA (Europe/Paris) или смещение (+03:00).",
        reply_markup=tz_kb()
    )

@router.message(Command("list"))
async def cmd_list(m: Message):
    uid = m.from_user.id
    items = [r for r in REMINDERS if r["user_id"] == uid]
    if not items:
        await m.answer("Пока нет напоминаний (в этой сессии).")
        return
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

# ========= MAIN HANDLER =========
@router.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    text = norm(m.text)

    # Если TZ ещё не задан — пробуем принять как ввод TZ
    if need_tz(uid):
        tz_obj = parse_user_tz_string(text)
        if tz_obj:
            store_user_tz(uid, tz_obj)
            await m.reply("Принял часовой пояс. Теперь пиши напоминание, например: «завтра в 19 отчёт».")
            return
        await ask_tz(m)
        return

    # Этап уточнения (если ждем выбор времени)
    if uid in PENDING:
        st = PENDING[uid]
        enriched = f"{text}. Контекст: {st.get('description','')}"
        data = await ai_parse(uid, enriched)
        desc = st.get("description") or data.get("description") or clean_desc(text)

        if data.get("ok") and data.get("datetimes"):
            dt = as_local_for(uid, data["datetimes"][0])
            PENDING.pop(uid, None)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
            plan(REMINDERS[-1])
            await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt_local(dt)}")
            return

        cands = data.get("datetimes", []) or st.get("candidates", [])
        if len(cands) >= 2:
            PENDING[uid] = {"description": desc, "candidates": cands}
            await m.reply("Уточни время:", reply_markup=kb_variants_for(uid, cands))
            return

        await m.reply("Нужно уточнить время. Примеры: 10, 10:30, 1710.")
        return

    # Новое сообщение → в LLM
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
        ct = data.get("clarify_type", "time")
        if ct == "time":
            await m.reply(f"Окей, «{desc}». Во сколько? (например: 10, 10:30, 1710)")
        elif ct == "date":
            await m.reply(f"Окей, «{desc}». На какой день?")
        else:
            await m.reply(f"Окей, «{desc}». Уточни дату и время.")
        return

    await m.reply("Не понял. Скажи, когда напомнить (например: «завтра в 19 отчёт»).")

# ========= CALLBACKS =========
@router.callback_query(F.data.startswith("settz|"))
async def cb_settz(cb: CallbackQuery):
    uid = cb.from_user.id
    _, payload = cb.data.split("|", 1)
    if payload == "ASK_OFFSET":
        await cb.message.answer("Введи смещение в формате +03:00 или укажи IANA (например, Europe/Moscow).")
        await cb.answer()
        return
    tz_obj = parse_user_tz_string(payload)
    if tz_obj is None:
        await cb.answer("Не понял таймзону", show_alert=True)
        return
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

# ========= RUN =========
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
