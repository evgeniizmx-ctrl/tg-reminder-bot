import os
import re
import asyncio
from datetime import datetime, timedelta
import pytz
import json

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ====== LLM client (OpenAI-style) ======
# pip install openai==1.*  (новый SDK)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # можно сменить

# ========= ENV / TZ =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_TZ = os.getenv("APP_TZ", "Europe/Moscow")
tz = pytz.timezone(APP_TZ)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler(timezone=tz)

# В оперативной памяти (MVP)
REMINDERS: list[dict] = []
PENDING: dict[int, dict] = {}  # {"description":..., "candidates":[iso,...]}

# ========= HELPERS =========
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "", flags=re.UNICODE).strip()

def clean_desc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", s, flags=re.I)
    s = re.sub(r"^(о|про|насч[её]т)\s+", "", s, flags=re.I)
    return s.strip() or "Напоминание"

def fmt_dt(dt: datetime) -> str:
    return f"{dt.strftime('%d.%m')} в {dt.strftime('%H:%M')} ({APP_TZ})"

def as_local(dt_iso: str) -> datetime:
    dt = datetime.fromisoformat(dt_iso.replace("Z","+00:00"))
    if dt.tzinfo is None:
        dt = tz.localize(dt)  # считаем локальным
    else:
        dt = dt.astimezone(tz)
    return dt

def kb_variants(dt_isos: list[str]) -> InlineKeyboardMarkup:
    dts = [as_local(x) for x in dt_isos]
    dts = sorted(dts)
    def human_label(dt: datetime) -> str:
        now = datetime.now(tz)
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
    scheduler.add_job(send_reminder, "date", run_date=rem["remind_dt"], args=[rem["user_id"], rem["text"]])

async def send_reminder(uid: int, text: str):
    try:
        await bot.send_message(uid, f"🔔 Напоминание: {text}")
    except Exception as e:
        print("send_reminder error:", e)

# ========= LLM PARSER =========
SYSTEM_PROMPT = """Ты — интеллектуальный парсер напоминаний на русском языке.
Задача: из произвольной фразы заранее определить напоминание.

Всегда отвечай строго JSON без комментариев и лишнего текста со схемой:
{
  "ok": true|false,                      // удалось ли определить конкретное время
  "description": "строка",               // что напоминать (кратко)
  "datetimes": ["ISO8601", ...],         // список кандидатов времени (локальная зона пользователя, если не указано иное)
  "need_clarification": false|true,      // нужна ли уточнялка
  "clarify_type": "time|date|both|none", // что уточнить
  "reason": "короткое объяснение"
}

Правила:
- Понимай разговорные формы: "завтра в полтретьего", "без пятнадцати четыре", "в пн утром", "через 2 часа", "сегодня в 1710".
- Если указаны часы двусмысленно (например "в 6"), верни два кандидата: 06:00 и 18:00 (или более подходящие по контексту).
- Если сказано только время без даты — ставь на ближайшее будущее.
- Если есть дата без времени — ok=false, need_clarification=true, clarify_type="time", datetimes пустой массив.
- Если всё понятно (один точный вариант) — ok=true, need_clarification=false и datetimes содержит одну дату-время.
- Описание выделяй из фразы, убирая вводные "напомни", "пожалуйста", предлоги и т.п.
- Всегда возвращай локальное время пользователя (user_tz).
"""

def build_user_prompt(text: str) -> str:
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    return json.dumps({
        "user_text": text,
        "now_local": now,
        "user_tz": APP_TZ,
        "locale": "ru-RU"
    }, ensure_ascii=False)

async def ai_parse(text: str) -> dict:
    """Вызывает LLM и возвращает дикт по схеме выше. В случае ошибки — безопасный фолбэк."""
    if not (OpenAI and OPENAI_API_KEY):
        # Фолбэк: ничего не поняли — просим время
        return {"ok": False, "description": clean_desc(text), "datetimes": [], "need_clarification": True, "clarify_type": "time", "reason": "LLM disabled"}
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        rsp = await asyncio.to_thread(
            client.chat.completions.create,
            model=OPENAI_MODEL,
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(text)}
            ],
            response_format={"type": "json_object"},
        )
        content = rsp.choices[0].message.content
        data = json.loads(content)
        # Санитайз
        data.setdefault("ok", False)
        data.setdefault("description", clean_desc(text))
        data.setdefault("datetimes", [])
        data.setdefault("need_clarification", not data.get("ok"))
        data.setdefault("clarify_type", "time" if not data.get("ok") else "none")
        return data
    except Exception as e:
        print("ai_parse error:", e)
        return {"ok": False, "description": clean_desc(text), "datetimes": [], "need_clarification": True, "clarify_type": "time", "reason": "LLM error"}

# ========= COMMANDS =========
@router.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет! Я ИИ-напоминалка. Понимаю: «завтра в полтретьего падел», «в пн утром позвонить Васе», "
        "«через 2 часа чай», «сегодня в 1710 отчёт».\n"
        "Если что-то двусмысленно — спрошу коротко и предложу варианты.\n"
        "/list — список, /cancel — отменить уточнение."
    )

@router.message(Command("list"))
async def cmd_list(m: Message):
    uid = m.from_user.id
    items = [r for r in REMINDERS if r["user_id"] == uid]
    if not items:
        await m.answer("Пока нет напоминаний (в этой сессии).")
        return
    items = sorted(items, key=lambda r: r["remind_dt"])
    lines = [f"• {r['text']} — {fmt_dt(r['remind_dt'])}" for r in items]
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

    # Этап уточнения: если ждём выбор из кандидатов — предложим кнопки ещё раз
    if uid in PENDING:
        st = PENDING[uid]
        # Если пользователь прислал свободный текст — попробуем ещё раз через ИИ,
        # но с уже известным description (для контекста)
        enriched = f"{text}. Контекст: {st.get('description','')}"
        data = await ai_parse(enriched)
        desc = st.get("description") or data.get("description") or clean_desc(text)

        if data.get("ok") and data.get("datetimes"):
            dt = as_local(data["datetimes"][0])
            PENDING.pop(uid, None)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
            plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
            return

        # Если прислал число/время — может получиться несколько кандидатов
        cands = data.get("datetimes", []) or st.get("candidates", [])
        if len(cands) >= 2:
            PENDING[uid] = {"description": desc, "candidates": cands}
            await m.reply("Уточните время:", reply_markup=kb_variants(cands))
            return
        else:
            await m.reply("Нужно уточнить время. Примеры: 10, 10:30, 1710.")
            return

    # Новое сообщение → отправляем в ИИ
    data = await ai_parse(text)
    desc = clean_desc(data.get("description") or text)

    # 1) Однозначно распознали
    if data.get("ok") and data.get("datetimes"):
        dt = as_local(data["datetimes"][0])
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
        plan(REMINDERS[-1])
        await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt(dt)}")
        return

    # 2) Есть несколько кандидатов → предложить кнопки
    cands = data.get("datetimes", [])
    if len(cands) >= 2:
        PENDING[uid] = {"description": desc, "candidates": cands}
        await m.reply(f"Уточните время для «{desc}»", reply_markup=kb_variants(cands))
        return

    # 3) Нужна уточнялка (нет времени/даты)
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

    # Фолбэк
    await m.reply("Не понял. Скажи, когда напомнить (например: «завтра в 19 отчёт»).")

# ========= CALLBACK =========
@router.callback_query(F.data.startswith("time|"))
async def cb_time(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in PENDING or not PENDING[uid].get("candidates"):
        await cb.answer("Нет активного уточнения"); return
    iso = cb.data.split("|", 1)[1]
    dt = as_local(iso)
    desc = PENDING[uid].get("description","Напоминание")
    PENDING.pop(uid, None)
    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
    plan(REMINDERS[-1])
    try:
        await cb.message.edit_text(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
    except Exception:
        await cb.message.answer(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
    await cb.answer("Установлено ✅")

# ========= RUN =========
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
