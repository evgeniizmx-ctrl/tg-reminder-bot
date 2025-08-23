import os
import re
import asyncio
from datetime import datetime, timedelta, date
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ========= ENV / TZ =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_TZ = os.getenv("APP_TZ", "Europe/Moscow")
tz = pytz.timezone(APP_TZ)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=APP_TZ)

# В этой демо-памяти держим только текущую сессию
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

def soonest(dts): return sorted(dts, key=lambda x: x)

def kb_variants(dts):
    rows = [[InlineKeyboardButton(text=human_label(dt), callback_data=f"time|{dt.isoformat()}")] for dt in soonest(dts)]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def human_label(dt: datetime) -> str:
    now = datetime.now(tz)
    if dt.date() == now.date():
        dword = "Сегодня"
    elif dt.date() == (now + timedelta(days=1)).date():
        dword = "Завтра"
    else:
        dword = dt.strftime("%d.%m")
    return f"{dword} в {dt.strftime('%H:%M')}"

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
    if re.search(r"\b\d{4}\b", s): return True  # 1710
    if re.search(r"\bв\s*\d{1,2}(:\d{2})?\b", s): return True
    if re.search(r"\bв\s*\d{1,2}\s*час", s): return True
    return False

# ========= LEXICON =========
MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12,
}

WEEKDAY_INDEX = {
    "понедельник":0,"вторник":1,"среда":2,"среду":2,"четверг":3,"пятница":4,"пятницу":4,"суббота":5,"субботу":5,"воскресенье":6
}

# порядковые в родительном — для «полтретьего» (значит 2:30)
ORD_GEN_TO_PREV_HOUR = {
    "первого":12, "второго":1, "третьего":2, "четвёртого":3, "четвертого":3, "пятого":4, "шестого":5,
    "седьмого":6, "восьмого":7, "девятого":8, "десятого":9, "одиннадцатого":10, "двенадцатого":11
}
# названия часов для «без пятнадцати четыре»
HOUR_WORD_TO_NUM = {
    "час":1,"два":2,"трёх":3,"трех":3,"три":3,"четыре":4,"пять":5,"шесть":6,"семь":7,"восемь":8,
    "девять":9,"десять":10,"одиннадцать":11,"двенадцать":12,
    "двух":2,"трёх":3,"трех":3,"четырёх":4,"четырех":4,"пяти":5,"шести":6,"семи":7,"восьми":8,
    "девяти":9,"десяти":10,"одиннадцати":11,"двенадцати":12
}
MIN_WORD_TO_NUM = {
    "пяти":5,"десяти":10,"пятнадцати":15,"двадцати":20,"двадцати пяти":25
}

# ========= REGEX =========
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
RX_IN_WEEKS = re.compile(
    r"\bчерез\s*(\d+)?\s*недел[юи]\b(?:\s*в\s*(\d{1,2})(?::(\d{2}))?)?",
    re.I
)

RX_SAME_TIME = re.compile(r"\bв это же время\b", re.I)
RX_IN_N_DAYS = re.compile(r"\bчерез\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b", re.I)

# 1710 → 17:10
RX_COMPACT_HHMM = re.compile(r"(?<![:\d])([01]?\d|2[0-3])([0-5]\d)(?!\d)", re.I)

# «полтретьего», «пол третьего»
RX_HALF_OF_NEXT = re.compile(r"\bпол\s*([А-Яа-яё]+|\d+)\b", re.I)

# «без пяти пять», «без 15 четыре»
RX_BEZ = re.compile(
    r"\bбез\s+([А-Яа-яё]+|\d+)\s+([А-Яа-яё]+|\d+)\b",
    re.I
)

# ========= PARSE LOGIC =========
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
    if RX_DAY_WORD_TIME.search(s) or RX_DAY_WORD_ONLY.search(s):
        return None
    m = RX_DAY_ONLY.search(s)
    if not m:
        return None
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
    now = datetime.now(tz).replace(second=0, microsecond=0)
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

    # двусмысленно (8 -> 08:00/20:00): варианты
    v1 = mk_dt(base, h % 24, mm)
    v2 = mk_dt(base, (h + 12) % 24, mm)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("amb", rest, soonest([v1, v2]))

def parse_only_time(text: str):
    s = norm(text)
    now = datetime.now(tz).replace(second=0, microsecond=0)

    # «10 утра / 7 вечера»
    mb = RX_BARE_TIME_WITH_MER.search(s)
    if mb:
        h = int(mb.group(1)); mm = int(mb.group(2) or 0); mer = mb.group(3).lower()
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        dt = now.replace(hour=h % 24, minute=mm)
        if dt <= now: dt += timedelta(days=1)
        rest = (s[:mb.start()] + s[mb.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    # «в 17:10» / «в 17»
    m = RX_ONLY_TIME.search(s)
    if m:
        h = int(m.group(1)); mm = int(m.group(2) or 0)
        if hour_is_unambiguous(h):
            dt = now.replace(hour=h % 24, minute=mm)
            if dt <= now: dt += timedelta(days=1)
            rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
            return ("ok", dt, rest)
        # 8 -> варианты сегодня/завтра (08:00/20:00)
        v1 = now.replace(hour=h % 24, minute=mm)
        v2 = now.replace(hour=(h + 12) % 24, minute=mm)
        if v1 <= now: v1 += timedelta(days=1)
        if v2 <= now: v2 += timedelta(days=1)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("amb", rest, soonest([v1, v2]))

    # «1710» → 17:10
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
    """через (n) неделю/недели [в HH[:MM]]"""
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

def parse_dot_date(text: str):
    s = norm(text); m = RX_DOT_DATE.search(s)
    if not m: return None
    dd, mm = int(m.group(1)), int(m.group(2))
    yy, hh, minu, mer = m.group(3), m.group(4), m.group(5), (m.group(6) or "").lower()
    now = datetime.now(tz); yyyy = now.year if not yy else (int(yy)+2000 if len(yy)==2 else int(yy))
    try: base = date(yyyy, mm, dd)
    except ValueError: return None
    rest = RX_DOT_DATE.sub("", s, count=1).strip(" ,.-")

    if not hh: return ("need_time", base, rest)
    h = int(hh); minute = int(minu or 0)
    if mer:
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        return ("ok", mk_dt(base, h%24, minute), rest)
    if hour_is_unambiguous(h): return ("ok", mk_dt(base, h%24, minute), rest)
    v1 = mk_dt(base, h%24, minute); v2 = mk_dt(base, (h+12)%24, minute)
    return ("amb", rest, soonest([v1, v2]))

def parse_month_date(text: str):
    s = norm(text); m = RX_MONTH_DATE.search(s)
    if not m: return None
    dd = int(m.group(1)); mon = m.group(2).lower()
    if mon not in MONTHS: return None
    mm = MONTHS[mon]
    hh, minu, mer = m.group(3), m.group(4), (m.group(5) or "").lower()
    now = datetime.now(tz); yyyy = now.year
    try: base = date(yyyy, mm, dd)
    except ValueError: return None
    if base < now.date():
        try: base = date(yyyy+1, mm, dd)
        except ValueError: return None
    rest = RX_MONTH_DATE.sub("", s, count=1).strip(" ,.-")
    if not hh: return ("need_time", base, rest)
    h = int(hh); minute = int(minu or 0)
    if mer:
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        return ("ok", mk_dt(base, h%24, minute), rest)
    if hour_is_unambiguous(h): return ("ok", mk_dt(base, h%24, minute), rest)
    v1 = mk_dt(base, h%24, minute); v2 = mk_dt(base, (h+12)%24, minute)
    return ("amb", rest, soonest([v1, v2]))

def nearest_future_day(day: int, now: datetime) -> date:
    y,m = now.year, now.month
    try:
        cand = date(y,m,day)
        if cand > now.date(): return cand
    except ValueError: pass
    y2,m2 = (y+1,1) if m==12 else (y,m+1)
    for dmax in (31,30,29,28):
        try: return date(y2,m2, min(day,dmax))
        except ValueError: continue
    return date(y2,m2,28)

def parse_day_of_month(text: str):
    s = norm(text); m = RX_DAY_OF_MONTH.search(s)
    if not m: return None
    dd = int(m.group(1)); hh, minu, mer = m.group(2), m.group(3), (m.group(4) or "").lower()
    now = datetime.now(tz); base = nearest_future_day(dd, now)
    rest = RX_DAY_OF_MONTH.sub("", s, count=1).strip(" ,.-")
    if not hh: return ("need_time", base, rest)
    h = int(hh); minute = int(minu or 0)
    if mer:
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        return ("ok", mk_dt(base, h%24, minute), rest)
    if hour_is_unambiguous(h): return ("ok", mk_dt(base, h%24, minute), rest)
    v1 = mk_dt(base, h%24, minute); v2 = mk_dt(base, (h+12)%24, minute)
    return ("amb", rest, soonest([v1, v2]))

def parse_weekday_part_only(text: str):
    """в понедельник утром — спросить время (без подстановки часов)"""
    s = norm(text)
    m_w = re.search(r"\b(понедельник|вторник|сред[ауы]|четверг|пятниц[ауы]|суббот[ауы]|воскресень[ея])\b", s, re.I)
    m_p = RX_ANY_MER.search(s)
    if m_w and m_p:
        wd = m_w.group(1).lower()
        idx = WEEKDAY_INDEX.get(wd)
        if idx is None: return None
        now = datetime.now(tz)
        days_ahead = (idx - now.weekday()) % 7
        if days_ahead == 0: days_ahead = 7
        base = (now + timedelta(days=days_ahead)).date()
        rest = (s[:m_w.start()] + s[m_w.end():]).strip(" ,.-")
        rest = RX_ANY_MER.sub("", rest, count=1).strip(" ,.-")
        return ("need_time", base, rest)
    return None

def parse_half_of_next(text: str):
    """полтретьего / пол третьего -> 2:30 (или 14:30)"""
    s = norm(text)
    m = RX_HALF_OF_NEXT.search(s)
    if not m: return None
    word = m.group(1).lower()
    now = datetime.now(tz)
    # если цифра: «пол 7» -> 6:30
    if word.isdigit():
        prev = max(0, int(word)-1)
        base_hour = prev if prev != 0 else 12  # 12:30 для «пол 1»
    else:
        base_hour = ORD_GEN_TO_PREV_HOUR.get(word)
        if base_hour is None:
            return None
    # уточнение «утром/вечером»?
    mer_m = RX_ANY_MER.search(s)
    h = base_hour
    if mer_m:
        mer = mer_m.group(1).lower()
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        dt = now.replace(hour=h % 24, minute=30, second=0, microsecond=0)
        if dt <= now: dt += timedelta(days=1)
        rest = RX_ANY_MER.sub("", (s[:m.start()] + s[m.end():]).strip(" ,.-"), count=1)
        return ("ok", dt, rest)
    # без уточнения — дать варианты 02:30 и 14:30
    v1 = now.replace(hour=h % 24, minute=30, second=0, microsecond=0)
    v2 = now.replace(hour=(h + 12) % 24, minute=30, second=0, microsecond=0)
    if v1 <= now: v1 += timedelta(days=1)
    if v2 <= now: v2 += timedelta(days=1)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("amb", rest, soonest([v1, v2]))

def word_or_digit_to_int(token: str) -> int | None:
    t = token.lower()
    if t.isdigit(): return int(t)
    if t in MIN_WORD_TO_NUM: return MIN_WORD_TO_NUM[t]
    if t in HOUR_WORD_TO_NUM: return HOUR_WORD_TO_NUM[t]
    return None

def parse_bez(text: str):
    """без пяти пять -> 4:55 ; без пятнадцати четыре -> 3:45"""
    s = norm(text)
    m = RX_BEZ.search(s)
    if not m: return None
    mins_token = m.group(1); hour_token = m.group(2)
    mins = word_or_digit_to_int(mins_token)
    hour = word_or_digit_to_int(hour_token)
    if mins is None or hour is None: return None
    if not (1 <= mins < 60 and 1 <= hour <= 12): return None
    # 4:55 = (hour-1): (60-mins)
    h = (hour - 1) % 12
    if h == 0: h = 12
    mm = 60 - mins
    now = datetime.now(tz)
    # уточнение «утром/вечером»?
    mer_m = RX_ANY_MER.search(s)
    if mer_m:
        mer = mer_m.group(1).lower()
        if mer.startswith("дн") or mer.startswith("веч"): h = h + 12 if h < 12 else h
        if mer.startswith("ноч"): h = 0 if h == 12 else h
        dt = now.replace(hour=h % 24, minute=mm)
        if dt <= now: dt += timedelta(days=1)
        rest = RX_ANY_MER.sub("", (s[:m.start()] + s[m.end():]).strip(" ,.-"), count=1)
        return ("ok", dt, rest)
    # варианты (08:xx/20:xx)
    v1 = now.replace(hour=h % 24, minute=mm)
    v2 = now.replace(hour=(h + 12) % 24, minute=mm)
    if v1 <= now: v1 += timedelta(days=1)
    if v2 <= now: v2 += timedelta(days=1)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("amb", rest, soonest([v1, v2]))

# ========= COMMANDS =========
@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет! Я бот-напоминалка.\n"
        "• Понимаю: «завтра в 19», «24.05 21:30», «через неделю в 15», «1710», "
        "«полтретьего», «без пятнадцати четыре», «в понедельник утром» (попрошу время).\n"
        "• Если есть только «завтра/послезавтра/…» — спрошу время.\n"
        "• Для «утром/вечером/днём/ночью» без цифр — всегда спрашиваю точные часы.\n"
        "/list — список, /ping — проверка, /cancel — отменить уточнение."
    )

@dp.message(Command("ping"))
async def cmd_ping(m: Message): await m.answer("pong ✅")

@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    uid = m.from_user.id
    if uid in PENDING:
        PENDING.pop(uid, None)
        await m.reply("Ок, отменил уточнение. Пиши новое напоминание.")
    else:
        await m.reply("Нечего отменять.")

@dp.message(Command("list"))
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
@dp.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    text = norm(m.text)

    # 0) есть незакрытое уточнение
    if uid in PENDING:
        st = PENDING[uid]
        if text.lower() in ("отмена","/cancel","cancel"):
            PENDING.pop(uid, None); await m.reply("Ок, отменил уточнение.")
            return
        if st.get("variants"):
            # ждём нажатие кнопок; если прислали новый запрос — сбросим
            if text_looks_like_new_request(text):
                PENDING.pop(uid, None)
            else:
                await m.reply("Нажмите кнопку ниже ⬇️", reply_markup=kb_variants(st["variants"]))
                return
        elif st.get("base_date"):
            mt = re.search(r"(?:^|\bв\s*)(\d{1,2})(?::(\d{2}))?\b", text, re.I)
            if not mt:
                await m.reply("Нужно точное время цифрами, например: 19 или 19:30.")
                return
            h = int(mt.group(1)); minute = int(mt.group(2) or 0)
            dt = mk_dt(st["base_date"], h, minute)
            desc = st.get("description","Напоминание")
            PENDING.pop(uid, None)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
            plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
            return

    # 1) только день («завтра», «послезавтра»)
    r = parse_day_only(text)
    if r:
        _, base, rest = r
        desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "base_date": base}
        await m.reply(f"Окей, {base.strftime('%d.%m')}. Во сколько? Напишите, например: 10 или 10:30.")
        return

    # 2) день + часть суток (без цифр)
    r = parse_dayword_part_only(text)
    if r:
        _, base, rest = r
        desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "base_date": base}
        await m.reply(f"Окей, {base.strftime('%d.%m')}. Во сколько? (например: 19 или 19:30)")
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
        await m.reply(f"Окей, {base.strftime('%d.%m')}. Во сколько?")
        return

    # 4) относительное «через …»
    r = parse_relative(text)
    if r:
        dt, rest = r; desc = clean_desc(rest or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
        await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return

    # 5) «в это же время через N дней»
    m_same = RX_SAME_TIME.search(text)
    if m_same:
        now = datetime.now(tz).replace(second=0, microsecond=0)
        m_nd = RX_IN_N_DAYS.search(text)
        if m_nd:
            n = int(m_nd.group(1))
            dt = now + timedelta(days=n)
            desc = clean_desc(RX_IN_N_DAYS.sub("", RX_SAME_TIME.sub("", text)).strip(" ,.-"))
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return

    # 6) «сегодня/завтра в HH[:MM]»
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

    # 7) «в понедельник утром»
    r = parse_weekday_part_only(text)
    if r:
        _, base, rest = r; desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "base_date": base}
        await m.reply(f"Окей, {base.strftime('%d.%m')}. Во сколько?"); return

    # 8) конкретные календарные даты
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

    # 9) «в 17 часов»
    r = parse_exact_hour(text)
    if r:
        dt, rest = r; desc = clean_desc(rest or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
        await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return

    # 10) «1710» / «в 17:10» / «10 утра»
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

    # 11) «полтретьего»
    r = parse_half_of_next(text)
    if r:
        tag = r[0]
        if tag == "ok":
            _, dt, rest = r; desc = clean_desc(rest or text)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"}); plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}"); return
        _, rest, variants = r; desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "variants": variants}
        await m.reply(f"Уточните время для «{desc}»", reply_markup=kb_variants(variants)); return

    # 12) «без пяти пять»
    r = parse_bez(text)
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
        "Не понял дату/время. Примеры: «завтра в 19», «24.05 21:30», «через неделю в 15», "
        "«1710», «полтретьего», «без пятнадцати четыре», «в понедельник утром» (потом время)."
    )

# ========= CALLBACKS =========
@dp.callback_query(F.data.startswith("time|"))
async def cb_time(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in PENDING or not PENDING[uid].get("variants"):
        await cb.answer("Нет активного уточнения"); return
    iso = cb.data.split("|", 1)[1]
    dt = datetime.fromisoformat(iso)
    dt = tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)
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
