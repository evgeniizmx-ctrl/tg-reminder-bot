import os
import re
import asyncio
from datetime import datetime, timedelta, date
import pytz

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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

# Планировщик: передаём tzinfo (не строку)
scheduler = AsyncIOScheduler(timezone=tz)

# в оперативной памяти —
REMINDERS: list[dict] = []
# PENDING[user_id] = {"description": str, "variants": [datetime], "base_date": date}
PENDING: dict[int, dict] = {}

# ========= HELPERS =========
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "", flags=re.UNICODE).strip()

def clean_desc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", s, flags=re.I)
    s = re.sub(r"^(о|про|насч[её]т)\s+", "", s, flags=re.I)
    return s.strip() or "Напоминание"

def mk_dt(d: date, h: int, m: int) -> datetime:
    return tz.localize(datetime(d.year, d.month, d.day, h % 24, m % 60, 0, 0))

def fmt_dt(dt: datetime) -> str:
    return f"{dt.strftime('%d.%m')} в {dt.strftime('%H:%M')} ({APP_TZ})"

def soonest(dts): 
    return sorted(dts, key=lambda x: x)

def human_label(dt: datetime) -> str:
    now = datetime.now(tz)
    if dt.date() == now.date():
        dword = "Сегодня"
    elif dt.date() == (now + timedelta(days=1)).date():
        dword = "Завтра"
    else:
        dword = dt.strftime("%d.%m")
    return f"{dword} в {dt.strftime('%H:%M')}"

def kb_variants(dts):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=human_label(dt), callback_data=f"time|{dt.isoformat()}")]
            for dt in soonest(dts)
        ]
    )

def plan(rem):
    scheduler.add_job(send_reminder, "date", run_date=rem["remind_dt"], args=[rem["user_id"], rem["text"]])

async def send_reminder(uid: int, text: str):
    try:
        await bot.send_message(uid, f"🔔 Напоминание: {text}")
    except Exception as e:
        print("send_reminder error:", e)

def hour_is_unambiguous(h: int) -> bool:
    return h >= 13 or h == 0  # 13..23 или 00

def text_looks_like_new_request(s: str) -> bool:
    s = norm(s).lower()
    if re.search(r"\bчерез\b", s): return True
    if re.search(r"\b(сегодня|завтра|послезавтра|послепослезавтра)\b", s): return True
    if re.search(r"\b\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?", s): return True
    if re.search(r"(?<![:\d])([01]?\d|2[0-3])([0-5]\d)(?!\d)", s): return True  # 1710
    if re.search(r"\bв\s*\d{1,2}(:\d{2})?\b", s): return True
    if re.search(r"\bв\s*\d{1,2}\s*час", s): return True
    return False

# ========= LEXICON / REGEX =========
MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12,
}
WEEKDAY_INDEX = {
    "понедельник":0,"вторник":1,"среда":2,"среду":2,"четверг":3,"пятница":4,"пятницу":4,"суббота":5,"субботу":5,"воскресенье":6
}

# для «полтретьего» → 2:30
ORD_GEN_TO_PREV_HOUR = {
    "первого":12, "второго":1, "третьего":2, "четвёртого":3, "четвертого":3, "пятого":4, "шестого":5,
    "седьмого":6, "восьмого":7, "девятого":8, "десятого":9, "одиннадцатого":10, "двенадцатого":11
}
# «без пятнадцати четыре»
HOUR_WORD_TO_NUM = {
    "час":1,"два":2,"три":3,"трёх":3,"трех":3,"четыре":4,"четырёх":4,"четырех":4,
    "пять":5,"шесть":6,"семь":7,"восемь":8,"девять":9,"десять":10,"одиннадцать":11,"двенадцать":12,
    "двух":2,"пяти":5,"шести":6,"семи":7,"восьми":8,"девяти":9,"десяти":10,"одиннадцати":11,"двенадцати":12
}
MIN_WORD_TO_NUM = {
    "пяти":5,"десяти":10,"пятнадцати":15,"двадцати":20,"двадцати пяти":25,"полу":30
}

RX_TODAY  = re.compile(r"\bсегодня\b", re.I)
RX_TMR    = re.compile(r"\bзавтра\b", re.I)
RX_ATMR   = re.compile(r"\bпослезавтра\b", re.I)
RX_A3     = re.compile(r"\bпослепослезавтра\b", re.I)
RX_DAY_ONLY = re.compile(r"\b(сегодня|завтра|послезавтра|послепослезавтра)\b", re.I)

RX_ANY_MER = re.compile(r"\b(утром|дн[её]м|дня|вечером|ночью|ночи)\b", re.I)

RX_DAY_WORD_TIME = re.compile(
    r"\b(сегодня|завтра|послезавтра|послепослезавтра)\b.*?\bв\s*(\d{1,2})(?::(\d{2}))?"
    r"(?:\s*(утра|дн[её]м|дня|вечера|ночью|ночи))?\b",
    re.I | re.DOTALL
)
RX_DAY_WORD_ONLY = re.compile(
    r"\b(сегодня|завтра|послезавтра|послепослезавтра)\b.*?\b(утром|дн[её]м|дня|вечером|ночью|ночи)\b",
    re.I | re.DOTALL
)
RX_DAY_WORD_COMPACT = re.compile(
    r"\b(сегодня|завтра|послезавтра|послепослезавтра)\b.*?\bв\s*([01]?\d|2[0-3])([0-5]\d)\b",
    re.I | re.DOTALL
)
RX_DAY_WORD_HALF = re.compile(
    r"\b(сегодня|завтра|послезавтра|послепослезавтра)\b.*?\bпол\s*([А-Яа-яё]+|\d+)\b(?:\s*(утром|дн[её]м|дня|вечером|ночью|ночи))?",
    re.I | re.DOTALL
)
RX_DAY_WORD_BEZ = re.compile(
    r"\b(сегодня|завтра|послезавтра|послепослезавтра)\b.*?\bбез\s+([А-Яа-яё]+|\d+)\s+([А-Яа-яё]+|\d+)\b(?:\s*(утром|дн[её]м|дня|вечером|ночью|ночи))?",
    re.I | re.DOTALL
)

RX_ONLY_TIME = re.compile(r"\bв\s*(\d{1,2})(?::(\d{2}))?\b", re.I)
RX_EXACT_HOUR = re.compile(r"\bв\s*(\d{1,2})\s*час(ов|а)?\b", re.I)
RX_BARE_TIME_WITH_MER = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(утром|дн[её]м|дня|вечером|ночью|ночи)\b", re.I)

RX_DOT_DATE = re.compile(
    r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?"
    r"(?:\s*в\s*(\d{1,2})(?::(\d{2}))?(?:\s*(утра|дн[её]м|дня|вечера|ночью|ночи))?)?",
    re.I
)
RX_MONTH_DATE = re.compile(
    r"\b(\d{1,2})\s+([А-Яа-яёЁ]+)\b"
    r"(?:\s*в\s*(\d{1,2})(?::(\d{2}))?(?:\s*(утра|дн[её]м|дня|вечера|ночью|ночи))?)?",
    re.I
)
RX_DAY_OF_MONTH = re.compile(
    r"\b(\d{1,2})\s*числ[ао]\b"
    r"(?:\s*в\s*(\d{1,2})(?::(\d{2}))?(?:\s*(утра|дн[её]м|дня|вечера|ночью|ночи))?)?",
    re.I
)

RX_HALF_HOUR = re.compile(r"\bчерез\s+пол\s*часа\b", re.I)
RX_REL = [
    (re.compile(r"\bчерез\s+(\d+)\s*(секунд(?:у|ы)?|сек\.?)\b", re.I), "seconds"),
    (re.compile(r"\bчерез\s+(\d+)\s*(минут(?:у|ы)?|мин\.?)\b", re.I), "minutes"),
    (re.compile(r"\bчерез\s+(\d+)\s*(час(?:а|ов)?|ч\.?)\b", re.I), "hours"),
    (re.compile(r"\bчерез\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b", re.I), "days"),
]
RX_IN_WEEKS = re.compile(r"\bчерез\s*(\d+)?\s*недел[юи]\b(?:\s*в\s*(\d{1,2})(?::(\d{2}))?)?", re.I)
RX_SAME_TIME = re.compile(r"\bв это же время\b", re.I)
RX_IN_N_DAYS = re.compile(r"\bчерез\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b", re.I)

RX_COMPACT_HHMM = re.compile(r"(?<![:\d])([01]?\d|2[0-3])([0-5]\d)(?!\d)", re.I)
RX_HALF_OF_NEXT = re.compile(r"\bпол\s*([А-Яа-яё]+|\d+)\b", re.I)
RX_BEZ = re.compile(r"\bбез\s+([А-Яа-яё]+|\d+)\s+([А-Яа-яё]+|\d+)\b", re.I)

# Будние
RX_WEEKDAY = re.compile(
    r"\b(в\s+)?(понедельник|вторник|сред(?:а|у)|четверг|пятниц(?:а|у)|суббот(?:а|у)|воскресенье)\b",
    re.I
)

# ========= PARSE CORE =========
def dayword_to_base(word: str, now: datetime) -> date:
    w = word.lower()
    if w == "сегодня": return now.date()
    if w == "завтра": return (now + timedelta(days=1)).date()
    if w == "послезавтра": return (now + timedelta(days=2)).date()
    if w == "послепослезавтра": return (now + timedelta(days=3)).date()
    return now.date()

def parse_day_only(text: str):
    """Только «сегодня/завтра/…» без времени — спросить время для этого дня"""
    s = norm(text)
    if RX_DAY_WORD_TIME.search(s) or RX_DAY_WORD_ONLY.search(s) or RX_DAY_WORD_COMPACT.search(s):
        return None
    m = RX_DAY_ONLY.search(s)
    if not m: return None
    now = datetime.now(tz)
    base = dayword_to_base(m.group(1), now)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("need_time", base, rest)

def parse_dayword_part_only(text: str):
    """день + часть суток (утром/вечером/…) без цифр — спросить время"""
    s = norm(text)
    m = RX_DAY_WORD_ONLY.search(s)
    if not m: return None
    now = datetime.now(tz)
    base = dayword_to_base(m.group(1), now)
    rest = RX_DAY_WORD_ONLY.sub("", s, count=1).strip(" ,.-")
    return ("need_time", base, rest)

def parse_dayword_time(text: str):
    """сегодня/завтра… в HH[:MM] (+меридиан)"""
    s = norm(text)
    m = RX_DAY_WORD_TIME.search(s)
    if not m: return None
    now = datetime.now(tz)
    base = dayword_to_base(m.group(1), now)
    h = int(m.group(2)); mm = int(m.group(3) or 0)
    mer = (m.group(4) or "").lower()

    if mer:
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        dt = mk_dt(base, h % 24, mm)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    if hour_is_unambiguous(h):
        dt = mk_dt(base, h % 24, mm)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    v1 = mk_dt(base, h % 24, mm)
    v2 = mk_dt(base, (h + 12) % 24, mm)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("amb", rest, soonest([v1, v2]))

def parse_dayword_compact(text: str):
    """сегодня/завтра в 1540"""
    s = norm(text)
    m = RX_DAY_WORD_COMPACT.search(s)
    if not m: return None
    now = datetime.now(tz)
    base = dayword_to_base(m.group(1), now)
    h = int(m.group(2)); mm = int(m.group(3))
    dt = mk_dt(base, h % 24, mm)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("ok", dt, rest)

def _half_core(word: str) -> int | None:
    if word.isdigit():
        prev = max(0, int(word) - 1)
        return 12 if prev == 0 else prev
    return ORD_GEN_TO_PREV_HOUR.get(word)

def parse_dayword_half(text: str):
    """сегодня/завтра … полтретьего [утром/вечером]"""
    s = norm(text)
    m = RX_DAY_WORD_HALF.search(s)
    if not m: return None
    now = datetime.now(tz)
    base = dayword_to_base(m.group(1), now)
    token = m.group(2).lower()
    base_hour = _half_core(token)
    if base_hour is None: return None
    mer = (m.group(3) or "").lower()

    if mer:
        h = base_hour
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        dt = mk_dt(base, h % 24, 30)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    v1 = mk_dt(base, base_hour % 24, 30)
    v2 = mk_dt(base, (base_hour + 12) % 24, 30)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("amb", rest, soonest([v1, v2]))

def word_or_digit_to_int(token: str) -> int | None:
    t = token.lower()
    if t.isdigit(): return int(t)
    if t in MIN_WORD_TO_NUM: return MIN_WORD_TO_NUM[t]
    if t in HOUR_WORD_TO_NUM: return HOUR_WORD_TO_NUM[t]
    return None

def parse_dayword_bez(text: str):
    """сегодня/завтра … без пяти пять [утром/вечером]"""
    s = norm(text)
    m = RX_DAY_WORD_BEZ.search(s)
    if not m: return None
    now = datetime.now(tz)
    base = dayword_to_base(m.group(1), now)
    mins = word_or_digit_to_int(m.group(2))
    hour = word_or_digit_to_int(m.group(3))
    if mins is None or hour is None: return None
    h = (hour - 1) % 12
    if h == 0: h = 12
    mm = 60 - mins
    mer = (m.group(4) or "").lower()

    if mer:
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        dt = mk_dt(base, h % 24, mm)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    v1 = mk_dt(base, h % 24, mm)
    v2 = mk_dt(base, (h + 12) % 24, mm)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("amb", rest, soonest([v1, v2]))

def parse_only_time(text: str):
    """время без даты"""
    s = norm(text)
    now = datetime.now(tz).replace(second=0, microsecond=0)

    mb = RX_BARE_TIME_WITH_MER.search(s)
    if mb:
        h = int(mb.group(1)); mm = int(mb.group(2) or 0); mer = mb.group(3).lower()
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        dt = now.replace(hour=h % 24, minute=mm)
        if dt <= now: dt += timedelta(days=1)
        rest = (s[:mb.start()] + s[mb.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    m = RX_ONLY_TIME.search(s)
    if m:
        h = int(m.group(1)); mm = int(m.group(2) or 0)
        if hour_is_unambiguous(h):
            dt = now.replace(hour=h % 24, minute=mm)
            if dt <= now: dt += timedelta(days=1)
            rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
            return ("ok", dt, rest)
        v1 = now.replace(hour=h % 24, minute=mm)
        v2 = now.replace(hour=(h + 12) % 24, minute=mm)
        if v1 <= now: v1 += timedelta(days=1)
        if v2 <= now: v2 += timedelta(days=1)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("amb", rest, soonest([v1, v2]))

    mc = RX_COMPACT_HHMM.search(s)
    if mc:
        h = int(mc.group(1)); mm = int(mc.group(2))
        dt = now.replace(hour=h, minute=mm)
        if dt <= now: dt += timedelta(days=1)
        rest = (s[:mc.start()] + s[mc.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    return None

def parse_exact_hour(text: str):
    s = norm(text); m = RX_EXACT_HOUR.search(s)
    if not m: return None
    h = int(m.group(1))
    now = datetime.now(tz).replace(second=0, microsecond=0)
    dt = now.replace(hour=h % 24, minute=0)
    if dt <= now: dt += timedelta(days=1)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return dt, rest

def parse_relative(text: str):
    s = norm(text); now = datetime.now(tz).replace(second=0, microsecond=0)
    if RX_HALF_HOUR.search(s):
        dt = now + timedelta(minutes=30)
        return dt, RX_HALF_HOUR.sub("", s).strip(" ,.-")
    for rx, kind in RX_REL:
        m = rx.search(s)
        if m:
            n = int(m.group(1))
            if kind == "seconds": dt = now + timedelta(seconds=n)
            elif kind == "minutes": dt = now + timedelta(minutes=n)
            elif kind == "hours": dt = now + timedelta(hours=n)
            else: dt = now + timedelta(days=n)
            rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
            return dt, rest
    return None

def parse_in_weeks(text: str):
    s = norm(text)
    m = RX_IN_WEEKS.search(s)
    if not m: return None
    n = int(m.group(1) or 1)
    hh = m.group(2); mm = m.group(3)
    now = datetime.now(tz)
    base = (now + timedelta(days=7*n)).date()
    rest = RX_IN_WEEKS.sub("", s, count=1).strip(" ,.-")
    if hh:
        dt = mk_dt(base, int(hh), int(mm or 0))
        return ("ok", dt, rest)
    else:
        return ("need_time", base, rest)

# ======= TIME-ONLY PARSING FOR PENDING (base_date) =======
def parse_time_for_base(text: str, base_date: date):
    """Понимаем время для заранее выбранной даты (этап уточнения)"""
    s = norm(text)

    # 1) явное HH[:MM]
    m = RX_ONLY_TIME.search(s)
    if m:
        h = int(m.group(1)); mm = int(m.group(2) or 0)
        # двусмысленные часы -> amb
        if hour_is_unambiguous(h):
            return ("ok", mk_dt(base_date, h % 24, mm), None)
        v1 = mk_dt(base_date, h % 24, mm)
        v2 = mk_dt(base_date, (h + 12) % 24, mm)
        return ("amb", None, soonest([v1, v2]))

    # 2) компактное HHMM
    mc = RX_COMPACT_HHMM.search(s)
    if mc:
        h = int(mc.group(1)); mm = int(mc.group(2))
        return ("ok", mk_dt(base_date, h % 24, mm), None)

    # 3) полтретьего
    mh = RX_HALF_OF_NEXT.search(s)
    if mh:
        token = mh.group(1).lower()
        base_h = _half_core(token)
        if base_h is not None:
            mer = RX_ANY_MER.search(s)
            if mer:
                h = base_h
                word = mer.group(1).lower()
                if word.startswith("дн") or word.startswith("веч"): h = h + 12 if h < 12 else h
                if word.startswith("ноч"): h = 0 if h == 12 else h
                return ("ok", mk_dt(base_date, h % 24, 30), None)
            # варианты 02:30 и 14:30
            v1 = mk_dt(base_date, base_h % 24, 30)
            v2 = mk_dt(base_date, (base_h + 12) % 24, 30)
            return ("amb", None, soonest([v1, v2]))

    # 4) без пяти пять / без 15 четыре
    mb = RX_BEZ.search(s)
    if mb:
        mins = word_or_digit_to_int(mb.group(1))
        hour = word_or_digit_to_int(mb.group(2))
        if mins is not None and hour is not None:
            h = (hour - 1) % 12
            if h == 0: h = 12
            mm = 60 - mins
            mer = RX_ANY_MER.search(s)
            if mer:
                word = mer.group(1).lower()
                if word.startswith("дн") or word.startswith("веч"): h = h + 12 if h < 12 else h
                if word.startswith("ноч"): h = 0 if h == 12 else h
                return ("ok", mk_dt(base_date, h % 24, mm), None)
            v1 = mk_dt(base_date, h % 24, mm)
            v2 = mk_dt(base_date, (h + 12) % 24, mm)
            return ("amb", None, soonest([v1, v2]))

    return None

# ========= КАЛЕНДАРНЫЕ ДАТЫ (были отсутствующие) =========
def _apply_meridian(h: int, mer: str | None) -> int:
    if not mer:
        return h
    m = mer.lower()
    if m.startswith("дн") or m.startswith("дня") or m.startswith("веч"):
        return h + 12 if h < 12 else h
    if m.startswith("ноч") or m.startswith("ночи"):
        return 0 if h == 12 else h
    return h

def parse_dot_date(text: str):
    s = norm(text)
    m = RX_DOT_DATE.search(s)
    if not m: return None
    dd, mm, yyyy, hh, mi, mer = m.groups()
    dd = int(dd); mm = int(mm); yyyy = int(yyyy or datetime.now(tz).year)
    base = date(yyyy, mm, dd)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")

    if hh:
        h = int(hh); mi = int(mi or 0)
        if mer:
            h = _apply_meridian(h, mer)
            return ("ok", mk_dt(base, h % 24, mi), rest)
        if hour_is_unambiguous(h):
            return ("ok", mk_dt(base, h % 24, mi), rest)
        # двусмысленно — AM/PM
        v1 = mk_dt(base, h % 24, mi)
        v2 = mk_dt(base, (h + 12) % 24, mi)
        return ("amb", rest, soonest([v1, v2]))

    return ("need_time", base, rest)

def parse_month_date(text: str):
    s = norm(text)
    m = RX_MONTH_DATE.search(s)
    if not m: return None
    dd, mon_word, hh, mi, mer = m.groups()
    dd = int(dd); mon_word = mon_word.lower()
    if mon_word not in MONTHS: return None
    mm = MONTHS[mon_word]
    yyyy = datetime.now(tz).year
    base = date(yyyy, mm, dd)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")

    if hh:
        h = int(hh); mi = int(mi or 0)
        if mer:
            h = _apply_meridian(h, mer)
            return ("ok", mk_dt(base, h % 24, mi), rest)
        if hour_is_unambiguous(h):
            return ("ok", mk_dt(base, h % 24, mi), rest)
        v1 = mk_dt(base, h % 24, mi)
        v2 = mk_dt(base, (h + 12) % 24, mi)
        return ("amb", rest, soonest([v1, v2]))

    return ("need_time", base, rest)

def parse_day_of_month(text: str):
    s = norm(text)
    m = RX_DAY_OF_MONTH.search(s)
    if not m: return None
    dd, hh, mi, mer = m.groups()
    dd = int(dd)
    now = datetime.now(tz)
    base = date(now.year, now.month, dd)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")

    if hh:
        h = int(hh); mi = int(mi or 0)
        if mer:
            h = _apply_meridian(h, mer)
            return ("ok", mk_dt(base, h % 24, mi), rest)
        if hour_is_unambiguous(h):
            return ("ok", mk_dt(base, h % 24, mi), rest)
        v1 = mk_dt(base, h % 24, mi)
        v2 = mk_dt(base, (h + 12) % 24, mi)
        return ("amb", rest, soonest([v1, v2]))

    return ("need_time", base, rest)

# ========= Будние (в понедельник …) =========
def next_weekday(base_dt: datetime, weekday_idx: int) -> date:
    # 0=Mon ... 6=Sun — следующая указанная неделя (не сегодня)
    days_ahead = (weekday_idx - base_dt.weekday()) % 7
    days_ahead = days_ahead or 7
    return (base_dt + timedelta(days=days_ahead)).date()

def parse_weekday(text: str):
    s = norm(text)
    m = RX_WEEKDAY.search(s)
    if not m: return None
    wd_word = m.group(2).lower()
    wd_word = {"среду":"среда","пятницу":"пятница"}.get(wd_word, wd_word)
    if wd_word not in WEEKDAY_INDEX: return None
    now = datetime.now(tz)
    base = next_weekday(now, WEEKDAY_INDEX[wd_word])

    # Если в той же строке есть время — попытаемся разобрать его на эту дату
    parsed_time = parse_time_for_base(s, base)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")

    if parsed_time:
        tag = parsed_time[0]
        if tag == "ok":
            _, dt, _ = parsed_time
            return ("ok", dt, rest)
        else:
            _, _, variants = parsed_time
            return ("amb", rest, variants)

    # Есть только «в понедельник (утром/вечером)» или вообще без времени — спросим время
    return ("need_time", base, rest)

# ========= COMMANDS =========
@router.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет! Я бот-напоминалка.\n"
        "Понимаю: «завтра в 19», «24.05 21:30», «завтра в 1540», «через неделю в 15», "
        "«полтретьего», «без пятнадцати четыре», «в понедельник утром» (попрошу время).\n"
        "Если только «сегодня/завтра/…» — спрошу время для этого дня.\n"
        "Если указал только время типа «в 6» — уточню 06:00 или 18:00.\n"
        "/list — список, /ping — проверка, /cancel — отменить уточнение."
    )

@router.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong ✅")

@router.message(Command("cancel"))
async def cmd_cancel(m: Message):
    uid = m.from_user.id
    if uid in PENDING:
        PENDING.pop(uid, None)
        await m.reply("Ок, отменил уточнение. Пиши новое напоминание.")
    else:
        await m.reply("Нечего отменять.")

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

# ========= ROUTER =========
@router.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    text = norm(m.text)

    # — этап уточнения —
    if uid in PENDING:
        st = PENDING[uid]

        if text.lower() in ("отмена","/cancel","cancel"):
            PENDING.pop(uid, None)
            await m.reply("Ок, отменил уточнение.")
            return

        if st.get("variants"):
            if text_looks_like_new_request(text):
                PENDING.pop(uid, None)
            else:
                await m.reply("Нажмите кнопку ниже ⬇️", reply_markup=kb_variants(st["variants"]))
                return

        elif st.get("base_date"):
            parsed = parse_time_for_base(text, st["base_date"])
            if parsed:
                tag = parsed[0]
                if tag == "ok":
                    _, dt, _ = parsed
                    desc = st.get("description","Напоминание")
                    PENDING.pop(uid, None)
                    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
                    plan(REMINDERS[-1])
                    await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
                    return
                else:
                    _, _, variants = parsed
                    desc = st.get("description","Напоминание")
                    PENDING[uid] = {"description": desc, "variants": variants}
                    await m.reply("Уточните, какое именно время:", reply_markup=kb_variants(variants))
                    return

            await m.reply("Нужно время. Примеры: 19, 19:30, 1710, «полтретьего», «без пяти пять».")
            return
        # если было variants — выше return; иначе продолжаем как новое

    # 1) только «сегодня/…»
    r = parse_day_only(text)
    if r:
        _, base, rest = r
        desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "base_date": base}
        await m.reply(f"Окей, {base.strftime('%d.%m')}. Во сколько? (например: 10, 10:30, 1710)")
        return

    # 2) день + часть суток
    r = parse_dayword_part_only(text)
    if r:
        _, base, rest = r
        desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "base_date": base}
        await m.reply(f"Окей, {base.strftime('%d.%m')}. Во сколько? (например: 19, 19:30)")
        return

    # 3) через неделю
    r = parse_in_weeks(text)
    if r:
        tag = r[0]
        if tag == "ok":
            _, dt, rest = r; desc = clean_desc(rest or text)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return
        _, base, rest = r; desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "base_date": base}
        await m.reply(f"Окей, {base.strftime('%d.%m')}. Во сколько?"); return

    # 4) относительное «через …»
    r = parse_relative(text)
    if r:
        dt, rest = r; desc = clean_desc(rest or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
        await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return

    # 5) «в это же время через N дней»
    if RX_SAME_TIME.search(text):
        now = datetime.now(tz).replace(second=0, microsecond=0)
        m_nd = RX_IN_N_DAYS.search(text)
        if m_nd:
            n = int(m_nd.group(1))
            dt = now + timedelta(days=n)
            desc = clean_desc(RX_IN_N_DAYS.sub("", RX_SAME_TIME.sub("", text)).strip(" ,.-"))
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return

    # 6) «сегодня/завтра в HH:MM»
    r = parse_dayword_time(text)
    if r:
        tag = r[0]
        if tag == "ok":
            _, dt, rest = r; desc = clean_desc(rest or text)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return
        _, rest, variants = r; desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "variants": variants}
        await m.reply(f"Уточните время для «{desc}»", reply_markup=kb_variants(variants)); return

    # 7) «сегодня в 1540»
    r = parse_dayword_compact(text)
    if r:
        _, dt, rest = r; desc = clean_desc(rest or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
        await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return

    # 8) «сегодня полтретьего»
    r = parse_dayword_half(text)
    if r:
        tag = r[0]
        if tag == "ok":
            _, dt, rest = r; desc = clean_desc(rest or text)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return
        _, rest, variants = r; desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "variants": variants}
        await m.reply(f"Уточните время для «{desc}»", reply_markup=kb_variants(variants)); return

    # 9) «сегодня без пяти пять»
    r = parse_dayword_bez(text)
    if r:
        tag = r[0]
        if tag == "ok":
            _, dt, rest = r; desc = clean_desc(rest or text)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return
        _, rest, variants = r; desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "variants": variants}
        await m.reply(f"Уточните время для «{desc}»", reply_markup=kb_variants(variants)); return

    # 10) конкретные календарные даты
    for parser in (parse_dot_date, parse_month_date, parse_day_of_month):
        r = parser(text)
        if r:
            tag = r[0]
            if tag == "ok":
                _, dt, rest = r; desc = clean_desc(rest or text)
                REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
                await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt(dt)}"); return
            if tag == "amb":
                _, rest, variants = r; desc = clean_desc(rest or text)
                PENDING[uid] = {"description": desc, "variants": variants}
                await m.reply(f"Уточните время для «{desc}»", reply_markup=kb_variants(variants)); return
            if tag == "need_time":
                _, base, rest = r; desc = clean_desc(rest or text)
                PENDING[uid] = {"description": desc, "base_date": base}
                await m.reply(f"Окей, {base.strftime('%d.%m')}. Во сколько?"); return

    # 10.5) Будний (в понедельник/…)
    r = parse_weekday(text)
    if r:
        tag = r[0]
        if tag == "ok":
            _, dt, rest = r; desc = clean_desc(rest or text)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
            await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt(dt)}"); return
        if tag == "amb":
            _, rest, variants = r; desc = clean_desc(rest or text)
            PENDING[uid] = {"description": desc, "variants": variants}
            await m.reply(f"Уточните время для «{desc}»", reply_markup=kb_variants(variants)); return
        if tag == "need_time":
            _, base, rest = r; desc = clean_desc(rest or text)
            PENDING[uid] = {"description": desc, "base_date": base}
            await m.reply(f"Окей, {base.strftime('%d.%m')}. Во сколько? (например: 10, 10:30)"); return

    # 11) «в 17 часов»
    r = parse_exact_hour(text)
    if r:
        dt, rest = r; desc = clean_desc(rest or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
        await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return

    # 12) «1710» / «в 17:10» / «10 утра»
    r = parse_only_time(text)
    if r:
        tag = r[0]
        if tag == "ok":
            _, dt, rest = r; desc = clean_desc(rest or text)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return
        _, rest, variants = r; desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "variants": variants}
        await m.reply(f"Уточните время для «{desc}»", reply_markup=kb_variants(variants)); return

    await m.reply(
        "Не понял дату/время. Примеры: «завтра в 19», «сегодня в 1540», «через неделю в 15», "
        "«полтретьего», «без пяти пять», «в понедельник утром» (потом время)."
    )

# ========= CALLBACK =========
@router.callback_query(F.data.startswith("time|"))
async def cb_time(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in PENDING or not PENDING[uid].get("variants"):
        await cb.answer("Нет активного уточнения"); return
    iso = cb.data.split("|", 1)[1]
    dt = datetime.fromisoformat(iso)
    # Нормализуем в pytz
    dt = tz.localize(dt.replace(tzinfo=None)) if dt.tzinfo is None else dt.astimezone(tz)
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
