import os
import json
import re
import asyncio
from datetime import datetime, timedelta, date
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.enums import ChatAction

import httpx
from dateutil import parser as dateparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ========= ОКРУЖЕНИЕ =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TZ = os.getenv("APP_TZ", "Europe/Moscow")
tz = pytz.timezone(TZ)

# ========= ИНИЦ =========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)

# PENDING[user_id] = {description, repeat, variants:[datetime], base_date:date}
PENDING: dict[int, dict] = {}
REMINDERS: list[dict] = []

# ========= УТИЛИТЫ =========
async def send_reminder(user_id: int, text: str):
    try:
        await bot.send_message(user_id, f"🔔 Напоминание: {text}")
    except Exception as e:
        print("Send reminder error:", e)

def schedule_one(reminder: dict):
    scheduler.add_job(send_reminder, "date",
                      run_date=reminder["remind_dt"],
                      args=[reminder["user_id"], reminder["text"]])

def as_local_iso(dt_like: str | None) -> datetime | None:
    if not dt_like:
        return None
    try:
        dt = dateparser.parse(dt_like)
        if not dt:
            return None
        dt = tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)
        return dt.replace(second=0, microsecond=0)
    except Exception:
        return None

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s, flags=re.UNICODE).strip()

def clean_description(desc: str) -> str:
    d = desc.strip()
    d = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^(о|про|насч[её]т)\s+", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^(сегодня|завтра|послезавтра)\b", "", d, flags=re.IGNORECASE).strip()
    return d or "Напоминание"

def _mk_dt(base_d: date, hh: int, mm: int) -> datetime:
    return tz.localize(datetime(base_d.year, base_d.month, base_d.day, hh % 24, mm % 60))

def _order_by_soonest(variants: list[datetime]) -> list[datetime]:
    return sorted(variants, key=lambda dt: dt)

# Человеко-читаемая подпись для кнопок: «сегодня/завтра/ДД.ММ в H[:MM] утра/дня/вечера/ночи»
def _human_label_for_variant(dt: datetime) -> str:
    now = datetime.now(tz)
    if dt.date() == now.date():
        dword = "Сегодня"
    elif dt.date() == (now + timedelta(days=1)).date():
        dword = "Завтра"
    else:
        dword = dt.strftime("%d.%m")

    h = dt.hour
    m = dt.minute
    if 0 <= h <= 4:
        mer = "ночи"
    elif 5 <= h <= 11:
        mer = "утра"
    elif 12 <= h <= 16:
        mer = "дня"
    else:
        mer = "вечера"

    h12 = h % 12
    if h12 == 0:
        h12 = 12

    tpart = f"{h12}:{m:02d}" if m else f"{h12}"
    return f"{dword} в {tpart} {mer}"


    h = dt.hour
    m = dt.minute
    if 0 <= h <= 4:
        mer = "ночи"
    elif 5 <= h <= 11:
        mer = "утра"
    elif 12 <= h <= 16:
        mer = "дня"
    else:
        mer = "вечера"

    h12 = h % 12
    if h12 == 0:
        h12 = 12

    tpart = f"{h12}:{m:02d}" if m else f"{h12}"
    return f"{dword} в {tpart} {mer}"

    h = dt.hour
    m = dt.minute
    # меридианы: 0–4 ночи, 5–11 утра, 12–16 дня, 17–23 вечера
    if 0 <= h <= 4:
        mer = "ночи"
    elif 5 <= h <= 11:
        mer = "утра"
    elif 12 <= h <= 16:
        mer = "дня"
    else:
        mer = "вечера"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    tpart = f"{h12}:{m:02d}" if m else f"{h12}"
    return f"{dword} в {tpart} {mer}"

def _variants_keyboard(variants: list[datetime]) -> InlineKeyboardMarkup:
    variants = _order_by_soonest(variants)
    rows = []
    for dt in variants:
        label = _human_label_for_variant(dt)
        rows.append([InlineKeyboardButton(text=label, callback_data=f"time|{dt.isoformat()}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ========= ПАРСЕРЫ =========
# --- «через …» ---
REL_NUM_PATTERNS = [
    (r"(через|спустя)\s+(\d+)\s*(секунд(?:у|ы)?|сек\.?)\b", "seconds"),
    (r"(через|спустя)\s+(\d+)\s*(минут(?:у|ы)?|мин\.?)\b", "minutes"),
    (r"(через|спустя)\s+(\d+)\s*(час(?:а|ов)?|ч\.?)\b",     "hours"),
    (r"(через|спустя)\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b","days"),
]
REL_NUM_REGEXES = [re.compile(p, re.IGNORECASE | re.UNICODE | re.DOTALL) for p, _ in REL_NUM_PATTERNS]
REL_SINGULAR = [
    (re.compile(r"(через|спустя)\s+секунд(?:у)\b", re.I), "seconds", 1),
    (re.compile(r"(через|спустя)\s+минут(?:у|ку)\b", re.I), "minutes", 1),
    (re.compile(r"(через|спустя)\s+час\b", re.I), "hours", 1),
    (re.compile(r"(через|спустя)\s+день\b", re.I), "days", 1),
]
REL_HALF_HOUR_RX = re.compile(r"(через)\s+пол\s*часа\b", re.IGNORECASE | re.UNICODE)

def parse_relative_phrase(raw_text: str):
    s = normalize_spaces(raw_text)
    now = datetime.now(tz).replace(second=0, microsecond=0)

    m = REL_HALF_HOUR_RX.search(s)
    if m:
        dt = now + timedelta(minutes=30)
        remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return dt, remainder

    for rx, kind, val in REL_SINGULAR:
        m = rx.search(s)
        if m:
            if kind == "seconds": dt = now + timedelta(seconds=val)
            elif kind == "minutes": dt = now + timedelta(minutes=val)
            elif kind == "hours":   dt = now + timedelta(hours=val)
            elif kind == "days":    dt = now + timedelta(days=val)
            remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")
            return dt, remainder

    for rx, (_, kind) in zip(REL_NUM_REGEXES, REL_NUM_PATTERNS):
        m = rx.search(s)
        if not m:
            continue
        amount = int(m.group(2))
        if kind == "seconds": dt = now + timedelta(seconds=amount)
        elif kind == "minutes": dt = now + timedelta(minutes=amount)
        elif kind == "hours":   dt = now + timedelta(hours=amount)
        elif kind == "days":    dt = now + timedelta(days=amount)
        remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return dt, remainder
    return None

# --- «в это же время» ---
SAME_TIME_RX = re.compile(r"\bв это же время\b", re.I | re.UNICODE)
TOMORROW_RX = re.compile(r"\bзавтра\b", re.I | re.UNICODE)
AFTER_TOMORROW_RX = re.compile(r"\bпослезавтра\b", re.I | re.UNICODE)
IN_N_DAYS_RX = re.compile(r"(через|спустя)\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b", re.I | re.UNICODE)

def parse_same_time_phrase(raw_text: str):
    s = normalize_spaces(raw_text)
    if not SAME_TIME_RX.search(s):
        return None
    now = datetime.now(tz).replace(second=0, microsecond=0)
    days = None
    if AFTER_TOMORROW_RX.search(s): days = 2
    elif TOMORROW_RX.search(s):     days = 1
    else:
        m = IN_N_DAYS_RX.search(s)
        if m:
            try: days = int(m.group(2))
            except: days = None
    if days is None:
        return None
    target = (now + timedelta(days=days)).replace(hour=now.hour, minute=now.minute)
    remainder = s
    for rx in (SAME_TIME_RX, TOMORROW_RX, AFTER_TOMORROW_RX, IN_N_DAYS_RX):
        remainder = rx.sub("", remainder)
    remainder = remainder.strip(" ,.-")
    return target, remainder

# --- «день (словами) + время» ---
DAYTIME_RX = re.compile(
    r"\b(сегодня|завтра|послезавтра)\b.*?\bв\s*(\d{1,2})(?::(\d{2}))?(?:\s*(утра|дня|вечера|ночи))?\b",
    re.IGNORECASE | re.UNICODE | re.DOTALL
)

def parse_daytime_phrase(raw_text: str):
    s = normalize_spaces(raw_text)
    m = DAYTIME_RX.search(s)
    if not m:
        return None

    day_word = m.group(1).lower()
    hour_raw = int(m.group(2))
    minute = int(m.group(3) or 0)
    mer = (m.group(4) or "").lower()

    now = datetime.now(tz).replace(second=0, microsecond=0)
    base = now if day_word == "сегодня" else (now + timedelta(days=1 if day_word == "завтра" else 2))
    remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")

    if mer in ("утра", "дня", "вечера", "ночи"):
        h = hour_raw
        if mer in ("дня", "вечера") and h < 12: h += 12
        if mer == "ночи" and h == 12: h = 0
        target = base.replace(hour=h % 24, minute=minute % 60)
        return ("ok", target, remainder)

    dt1 = base.replace(hour=hour_raw % 24, minute=minute % 60)
    h2 = 0 if hour_raw == 12 else (hour_raw + 12) % 24
    dt2 = base.replace(hour=h2, minute=minute % 60)

    if base.date() == now.date():
        if dt1 <= now: dt1 = dt1 + timedelta(days=1)
        if dt2 <= now: dt2 = dt2 + timedelta(days=1)

    return ("amb", remainder, _order_by_soonest([dt1, dt2]))

# --- «в HH[:MM]» без дня ---
ONLYTIME_RX = re.compile(r"\bв\s*(\d{1,2})(?::(\d{2}))?\b", re.I | re.UNICODE)

def parse_onlytime_phrase(raw_text: str):
    s = normalize_spaces(raw_text)
    m = ONLYTIME_RX.search(s)
    if not m:
        return None
    hour_raw = int(m.group(1))
    minute = int(m.group(2) or 0)
    now = datetime.now(tz).replace(second=0, microsecond=0)

    mer_m = re.search(r"(утра|вечера|дня|ночи)", s, re.IGNORECASE)
    if mer_m:
        mer = mer_m.group(1).lower()
        h = hour_raw
        if mer in ("дня", "вечера") and h < 12: h += 12
        if mer == "ночи" and h == 12: h = 0
        target = now.replace(hour=h % 24, minute=minute % 60)
        if target <= now: target += timedelta(days=1)
        remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("ok", target, remainder)

    cand = []
    for h in [hour_raw % 24, (hour_raw + 12) % 24]:
        dt = now.replace(hour=h, minute=minute % 60)
        if dt <= now: dt += timedelta(days=1)
        cand.append(dt)
    remainder = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("amb", remainder, _order_by_soonest(cand))

# --- «только дата без времени» (числом) ---
DATE_DOT_RX = re.compile(r"\b(\d{1,2})[.\-\/](\d{1,2})(?:[.\-\/](\d{2,4}))?\b(?!.*\bв\s*\d)", re.IGNORECASE | re.UNICODE)
DATE_NUM_RX = re.compile(r"(?:\bна\s+)?\b(\d{1,2})\b(?:\s*числа)?\b", re.IGNORECASE | re.UNICODE)

def nearest_future_day(day: int, now: datetime) -> date:
    y, m = now.year, now.month
    try:
        candidate = date(y, m, day)
    except ValueError:
        m2 = m + 1 if m < 12 else 1
        y2 = y if m < 12 else y + 1
        return date(y2, m2, min(day, 28))
    if candidate <= now.date():
        m2 = m + 1 if m < 12 else 1
        y2 = y if m < 12 else y + 1
        for dcap in (31, 30, 29, 28):
            try:
                return date(y2, m2, min(day, dcap))
            except ValueError:
                continue
    return candidate

def parse_numeric_date_only(raw_text: str):
    s = normalize_spaces(raw_text)
    # если есть «в \d» — это про время, не дата
    if re.search(r"\bв\s*\d", s, re.IGNORECASE):
        return None

    m = DATE_DOT_RX.search(s)
    if m:
        dd = int(m.group(1)); mm = int(m.group(2)); yy = m.group(3)
        now = datetime.now(tz)
        yyyy = (int(yy) + 2000) if yy and int(yy) < 100 else (int(yy) if yy else now.year)
        try:
            base = date(yyyy, mm, dd)
        except ValueError:
            return None
        desc = clean_description(DATE_DOT_RX.sub("", s))
        return ("day", base, desc)

    m2 = DATE_NUM_RX.search(s)
    if m2:
        dd = int(m2.group(1))
        now = datetime.now(tz)
        base = nearest_future_day(dd, now)
        desc = clean_description(DATE_NUM_RX.sub("", s))
        return ("day", base, desc)

    return None

# --- GPT fallback (редко) ---
OPENAI_BASE = "https://api.openai.com/v1"
async def gpt_parse(text: str) -> dict:
    system = ("Ты — ассистент-напоминалка. Верни СТРОГО JSON с ключами: "
              "description, event_time, remind_time, repeat(daily|weekly|none), "
              "needs_clarification, clarification_question. "
              "Даты/время в 'YYYY-MM-DD HH:MM' (24h). Язык — русский.")
    payload = {"model": "gpt-4o-mini",
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": text}],
               "temperature": 0}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{OPENAI_BASE}/chat/completions", headers=headers, json=payload)
            r.raise_for_status()
            answer = r.json()["choices"][0]["message"]["content"]
        return json.loads(answer)
    except Exception as e:
        print("GPT parse fail:", e)
        return {"description": text, "event_time": "", "remind_time": "",
                "repeat": "none", "needs_clarification": True,
                "clarification_question": "Уточните дату и время."}

# ========= КОМАНДЫ =========
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я бот-напоминалка.\n"
        "Понимаю: «Свадьба 25», «Свадьба в 6», «в 10», «через 3 минуты», «завтра в 5».\n"
        "Голос/скрин — можно. /list — список, /ping — проверка."
    )

@dp.message(Command("ping"))
async def ping(message: Message):
    await message.answer("pong ✅")

@dp.message(Command("list"))
async def list_cmd(message: Message):
    uid = message.from_user.id
    items = [r for r in REMINDERS if r["user_id"] == uid]
    if not items:
        await message.answer("Пока нет напоминаний (в этой сессии).")
        return
    items_sorted = sorted(items, key=lambda r: r["remind_dt"])
    lines = [f"• {r['text']} — {r['remind_dt'].strftime('%d.%m %H:%M')} ({TZ})"
             + (f" [{r['repeat']}]" if r['repeat'] != 'none' else "")
             for r in items_sorted]
    await message.answer("\n".join(lines))

# ========= ОСНОВНАЯ ЛОГИКА =========
@dp.message(F.text)
async def on_any_text(message: Message):
    uid = message.from_user.id
    text_raw = message.text or ""
    text = normalize_spaces(text_raw)

    # --- уже ждём уточнение ---
    if uid in PENDING:
        st = PENDING[uid]

        # ждём нажатия кнопок
        if st.get("variants"):
            await message.reply("Нажмите одну из кнопок ниже, чтобы выбрать время ⬇️")
            return

        # известен день — ждём ТОЛЬКО время
        if st.get("base_date"):
            m = re.search(r"(?:^|\bв\s*)(\d{1,2})(?::(\d{2}))?\s*(утра|дня|вечера|ночи)?\b",
                          text, re.IGNORECASE)
            if not m:
                await message.reply("Во сколько?")
                return
            hour = int(m.group(1)); minute = int(m.group(2) or 0)
            mer = (m.group(3) or "").lower()
            base_d: date = st["base_date"]

            if mer:
                h = hour
                if mer in ("дня", "вечера") and h < 12: h += 12
                if mer == "ночи" and h == 12: h = 0
                dt = _mk_dt(base_d, h, minute)
                desc = st.get("description", "Напоминание")
                PENDING.pop(uid, None)
                REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
                schedule_one(REMINDERS[-1])
                await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
                return

            # двусмысленно — покажем 2 варианта (сортировка по ближайшему)
            v1 = _mk_dt(base_d, hour, minute)
            v2 = _mk_dt(base_d, (hour + 12) % 24, minute)
            PENDING[uid]["variants"] = _order_by_soonest([v1, v2])
            await message.reply("Уточните время:", reply_markup=_variants_keyboard([v1, v2]))
            return

        # обычное уточнение (без base_date)
        for parser in (parse_daytime_phrase, parse_onlytime_phrase):
            pack = parser(text)
            if pack:
                tag = pack[0]
                if tag == "amb":
                    _, remainder, variants = pack
                    desc = clean_description(remainder or st.get("description", text))
                    PENDING[uid] = {"description": desc, "variants": _order_by_soonest(variants), "repeat": "none"}
                    await message.reply(f"Уточните, во сколько напомнить «{desc}»?",
                                        reply_markup=_variants_keyboard(variants))
                    return
                else:
                    _, dt, remainder = pack
                    desc = clean_description(remainder or st.get("description", text))
                    PENDING.pop(uid, None)
                    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": st.get("repeat","none")})
                    schedule_one(REMINDERS[-1])
                    await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
                    return

        rel = parse_relative_phrase(text)
        if rel:
            dt, remainder = rel
            desc = clean_description(remainder or st.get("description", text))
            PENDING.pop(uid, None)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": st.get("repeat","none")})
            schedule_one(REMINDERS[-1])
            await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
            return

        same = parse_same_time_phrase(text)
        if same:
            dt, remainder = same
            desc = clean_description(remainder or st.get("description", text))
            PENDING.pop(uid, None)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": st.get("repeat","none")})
            schedule_one(REMINDERS[-1])
            await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
            return

        dt = as_local_iso(text)
        if dt:
            desc = clean_description(st.get("description", text))
            PENDING.pop(uid, None)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": st.get("repeat","none")})
            schedule_one(REMINDERS[-1])
            await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
            return

        await message.reply("Не понял время.")
        return

    # --- новая сессия ---
    # 1) только день (словами)
    day_only_words = re.search(r"\b(сегодня|завтра|послезавтра)\b(?!.*\bв\s*\d)", text, re.IGNORECASE)
    if day_only_words:
        word = day_only_words.group(1).lower()
        now = datetime.now(tz)
        base = now.date() if word == "сегодня" else (now + timedelta(days=1 if word == "завтра" else 2)).date()
        desc = clean_description(re.sub(r"\b(сегодня|завтра|послезавтра)\b", "", text, flags=re.IGNORECASE))
        PENDING[uid] = {"description": desc, "base_date": base, "repeat": "none"}
        await message.reply(f"Окей, {base.strftime('%d.%m')}. В какое время?")
        return

    # 2) только день (числом)
    day_only_num = parse_numeric_date_only(text)
    if day_only_num:
        _, base, desc = day_only_num
        PENDING[uid] = {"description": desc, "base_date": base, "repeat": "none"}
        await message.reply(f"Окей, {base.strftime('%d.%m')}. В какое время?")
        return

    # 3) день+время, просто время, относительное, «в это же время»
    for parser in (parse_daytime_phrase, parse_onlytime_phrase):
        pack = parser(text)
        if pack:
            tag = pack[0]
            if tag == "amb":
                _, remainder, variants = pack
                desc = clean_description(remainder or text)
                PENDING[uid] = {"description": desc, "variants": _order_by_soonest(variants), "repeat": "none"}
                await message.reply(f"Уточните, во сколько напомнить «{desc}»?",
                                    reply_markup=_variants_keyboard(variants))
                return
            else:
                _, dt, remainder = pack
                desc = clean_description(remainder or text)
                REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
                schedule_one(REMINDERS[-1])
                await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
                return

    rel = parse_relative_phrase(text)
    if rel:
        dt, remainder = rel
        desc = clean_description(remainder or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
        schedule_one(REMINDERS[-1])
        await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
        return

    same = parse_same_time_phrase(text)
    if same:
        dt, remainder = same
        desc = clean_description(remainder or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
        schedule_one(REMINDERS[-1])
        await message.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
        return

    # 4) fallback — GPT
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    plan = await gpt_parse(text)
    desc = clean_description(plan.get("description") or "Напоминание")
    repeat = (plan.get("repeat") or "none").lower()
    remind_iso = plan.get("remind_time") or plan.get("event_time")
    remind_dt = as_local_iso(remind_iso)

    if plan.get("needs_clarification") or not remind_dt:
        PENDING[uid] = {"description": desc, "repeat": "none"}
        await message.reply("Уточните дату и время.")
        return

    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": remind_dt,
                      "repeat": "none" if repeat not in ("daily","weekly") else repeat})
    schedule_one(REMINDERS[-1])
    await message.reply(f"Готово. Напомню: «{desc}» в {remind_dt.strftime('%d.%m %H:%M')} ({TZ})")

# ========= КНОПКИ =========
@dp.callback_query(F.data.startswith("time|"))
async def on_time_choice(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in PENDING or not PENDING[uid].get("variants"):
        await cb.answer("Нет активного уточнения")
        return
    try:
        iso = cb.data.split("|", 1)[1]
        dt = datetime.fromisoformat(iso)
        dt = tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)
    except Exception as e:
        print("time| parse error:", e)
        await cb.answer("Ошибка выбора времени")
        return

    desc = PENDING[uid].get("description", "Напоминание")
    PENDING.pop(uid, None)

    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat": "none"})
    schedule_one(REMINDERS[-1])

    try:
        await cb.message.edit_text(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
    except Exception:
        await cb.message.answer(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({TZ})")
    await cb.answer("Установлено ✅")

# ========= ЗАПУСК =========
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback, time
        print("FATAL:", e)
        traceback.print_exc()
        time.sleep(10)
        raise
