import os
import re
import asyncio
from datetime import datetime, timedelta, date
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --------- ENV / TZ ---------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_TZ = os.getenv("APP_TZ", "Europe/Moscow")
tz = pytz.timezone(APP_TZ)

# --------- BOT / SCHED ---------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=APP_TZ)

# PENDING[user_id] = {"description": str, "variants": [datetime], "base_date": date}
PENDING: dict[int, dict] = {}
REMINDERS: list[dict] = []

# --------- UTILS ---------
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "", flags=re.UNICODE).strip()

def clean_desc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", s, flags=re.I)
    s = re.sub(r"^(о|про|насч[её]т)\s+", "", s, flags=re.I)
    return s.strip() or "Напоминание"

def fmt_dt(dt: datetime) -> str:
    return f"{dt.strftime('%d.%m')} в {dt.strftime('%H:%M')} ({APP_TZ})"

async def send_reminder(uid: int, text: str):
    try:
        await bot.send_message(uid, f"🔔 Напоминание: {text}")
    except Exception as e:
        print("send_reminder error:", e)

def plan(rem: dict):
    scheduler.add_job(send_reminder, "date",
                      run_date=rem["remind_dt"],
                      args=[rem["user_id"], rem["text"]])

def mk_dt(d: date, h: int, m: int) -> datetime:
    return tz.localize(datetime(d.year, d.month, d.day, h % 24, m % 60, 0, 0))

def soonest(dts: list[datetime]) -> list[datetime]:
    return sorted(dts, key=lambda x: x)

def human_label(dt: datetime) -> str:
    now = datetime.now(tz)
    if dt.date() == now.date():
        dword = "Сегодня"
    elif dt.date() == (now + timedelta(days=1)).date():
        dword = "Завтра"
    else:
        dword = dt.strftime("%d.%m")
    h, m = dt.hour, dt.minute
    if 0 <= h <= 4: mer = "ночи"
    elif 5 <= h <= 11: mer = "утра"
    elif 12 <= h <= 16: mer = "дня"
    else: mer = "вечера"
    h12 = h % 12 or 12
    t = f"{h12}:{m:02d}" if m else f"{h12}"
    return f"{dword} в {t} {mer}"

def kb_variants(dts: list[datetime]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=human_label(dt), callback_data=f"time|{dt.isoformat()}")]
            for dt in soonest(dts)
        ]
    )

# --------- MONTHS ---------
MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12,
    "январь":1,"февраль":2,"март":3,"апрель":4,"май":5,"июнь":6,"июль":7,
    "август":8,"сентябрь":9,"октябрь":10,"ноябрь":11,"декабрь":12,
}

def nearest_future_day(day: int, now: datetime) -> date:
    y, m = now.year, now.month
    try:
        cand = date(y, m, day)
        if cand > now.date():
            return cand
    except ValueError:
        pass
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    for dcap in (31,30,29,28):
        try:
            return date(y2, m2, min(day, dcap))
        except ValueError:
            continue
    return date(y2, m2, 28)

# --------- REGEX ---------
RX_HALF_HOUR = re.compile(r"\bчерез\s+пол\s*часа\b", re.I)
RX_REL = [
    (re.compile(r"\bчерез\s+(\d+)\s*(секунд(?:у|ы)?|сек\.?)\b", re.I), "seconds"),
    (re.compile(r"\bчерез\s+(\d+)\s*(минут(?:у|ы)?|мин\.?)\b", re.I), "minutes"),
    (re.compile(r"\bчерез\s+(\d+)\s*(час(?:а|ов)?|ч\.?)\b", re.I), "hours"),
    (re.compile(r"\bчерез\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b", re.I), "days"),
]
RX_REL_SINGULAR = [
    (re.compile(r"\bчерез\s+секунд[ую]\b", re.I), "seconds", 1),
    (re.compile(r"\bчерез\s+минут[ую]\b", re.I), "minutes", 1),
    (re.compile(r"\bчерез\s+час\b", re.I), "hours", 1),
    (re.compile(r"\bчерез\s+день\b", re.I), "days", 1),
]
RX_SAME_TIME = re.compile(r"\bв это же время\b", re.I)
RX_TMR = re.compile(r"\bзавтра\b", re.I)
RX_ATMR = re.compile(r"\bпослезавтра\b", re.I)
RX_IN_N_DAYS = re.compile(r"\bчерез\s+(\d+)\s*(дн(?:я|ей)?|день|дн\.?)\b", re.I)

# «сегодня/завтра/послезавтра … в HH[:MM] [меридиан]»
RX_DAY_WORD_TIME = re.compile(
    r"\b(сегодня|завтра|послезавтра)\b.*?\bв\s*(\d{1,2})"
    r"(?:(?::(\d{2}))|\s*час(?:ов|а)?)?"
    r"(?:\s*(утра|дн[её]м|дня|вечера|ночью|ночи))?\b",
    re.I | re.DOTALL
)
# «сегодня/завтра/послезавтра» + часть суток (без числа)
RX_DAY_WORD_ONLY = re.compile(
    r"\b(сегодня|завтра|послезавтра)\b.*?\b(утром|дн[её]м|дня|вечером|ночью|ночи)\b",
    re.I | re.DOTALL
)

# «в HH[:MM]»
RX_ONLY_TIME = re.compile(r"\bв\s*(\d{1,2})(?::(\d{2}))?\b", re.I)
# «HH[:MM] утром/вечером …» — БЕЗ «в»
RX_BARE_TIME_WITH_MER = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(утром|дн[её]м|дня|вечером|ночью|ночи)\b", re.I)

# «в HH часов»
RX_EXACT_HOUR = re.compile(r"\bв\s*(\d{1,2})\s*час(ов|а)?\b", re.I)

# даты
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

RX_ANY_MER = re.compile(r"\b(утром|дн[её]м|дня|вечером|ночью|ночи)\b", re.I)
RX_TODAY = re.compile(r"\bсегодня\b", re.I)

# --------- HELPERS ---------
def hour_is_unambiguous(h: int) -> bool:
    return h >= 13 or h == 0  # 13..23 или 00

def part_of_day_defaults(word: str) -> int:
    w = word.lower()
    if w.startswith("утр"):   return 9
    if w.startswith("дн"):    return 13
    if w.startswith("веч"):   return 19
    return 1  # ночь

def apply_meridian(h: int, mer: str | None) -> int:
    if not mer: return h
    mer = mer.lower()
    if mer.startswith("дн"):   return h + 12 if h < 12 else h
    if mer.startswith("веч"):  return h + 12 if h < 12 else h
    if mer.startswith("ноч"):  return 0 if h == 12 else h
    return h  # утром — без сдвига

def text_looks_like_new_request(s: str) -> bool:
    s = norm(s).lower()
    if re.search(r"\bчерез\b", s): return True
    if re.search(r"\b(сегодня|завтра|послезавтра)\b", s): return True
    if re.search(r"\b\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?", s): return True
    if re.search(r"\b\d{1,2}\s+[а-яё]+", s): return True
    if re.search(r"\bв\s*\d{1,2}\s*час(ов|а)?\b", s): return True
    if re.search(r"\bв\s*\d{1,2}(?::\d{2})?\s*(утром|дн[её]м|дня|вечером|ночью|ночи)\b", s): return True
    if re.search(r"\bв\s*(?:1[3-9]|2[0-3]|00)\b", s): return True
    return False

# --------- PARSERS ---------
def parse_relative(text: str):
    s = norm(text); now = datetime.now(tz).replace(second=0, microsecond=0)
    if RX_HALF_HOUR.search(s):
        dt = now + timedelta(minutes=30)
        return dt, RX_HALF_HOUR.sub("", s).strip(" ,.-")
    for rx, kind, val in RX_REL_SINGULAR:
        m = rx.search(s)
        if m:
            dt = now + (timedelta(seconds=val) if kind=="seconds" else
                        timedelta(minutes=val) if kind=="minutes" else
                        timedelta(hours=val) if kind=="hours" else
                        timedelta(days=val))
            return dt, (s[:m.start()] + s[m.end():]).strip(" ,.-")
    for rx, kind in RX_REL:
        m = rx.search(s)
        if m:
            n = int(m.group(1))
            dt = now + (timedelta(seconds=n) if kind=="seconds" else
                        timedelta(minutes=n) if kind=="minutes" else
                        timedelta(hours=n) if kind=="hours" else
                        timedelta(days=n))
            return dt, (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return None

def parse_same_time(text: str):
    s = norm(text)
    if not RX_SAME_TIME.search(s):
        return None
    now = datetime.now(tz).replace(second=0, microsecond=0)
    days = 1 if RX_TMR.search(s) else 2 if RX_ATMR.search(s) else None
    if days is None:
        m = RX_IN_N_DAYS.search(s)
        if m: days = int(m.group(1))
    if days is None:
        return None
    dt = (now + timedelta(days=days)).replace(second=0, microsecond=0)
    s2 = RX_IN_N_DAYS.sub("", RX_ATMR.sub("", RX_TMR.sub("", RX_SAME_TIME.sub("", s)))).strip(" ,.-")
    return dt, s2

def parse_dayword_time(text: str):
    s = norm(text); now = datetime.now(tz).replace(second=0, microsecond=0)

    m = RX_DAY_WORD_TIME.search(s)
    if m:
        word = m.group(1).lower()
        h = int(m.group(2)); mm = int(m.group(3) or 0)
        mer = (m.group(4) or "").lower()
        base = now.date()
        if word == "завтра": base = (now + timedelta(days=1)).date()
        elif word == "послезавтра": base = (now + timedelta(days=2)).date()

        if mer:
            dt = mk_dt(base, apply_meridian(h, mer), mm)
            rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
            return ("ok", dt, rest)

        if hour_is_unambiguous(h):
            dt = mk_dt(base, h % 24, mm)
            rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
            return ("ok", dt, rest)

        v1 = mk_dt(base, h % 24, mm)
        v2 = mk_dt(base, (h + 12) % 24, mm)
        if base == now.date():
            if v1 <= now: v1 += timedelta(days=1)
            if v2 <= now: v2 += timedelta(days=1)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("amb", rest, soonest([v1, v2]))

    m2 = RX_DAY_WORD_ONLY.search(s)
    if m2:
        word = m2.group(1).lower()
        mer = m2.group(2).lower()
        base = now.date()
        if word == "завтра": base = (now + timedelta(days=1)).date()
        elif word == "послезавтра": base = (now + timedelta(days=2)).date()
        h = part_of_day_defaults(mer)
        dt = mk_dt(base, h, 0)
        rest = RX_DAY_WORD_ONLY.sub("", s, count=1).strip(" ,.-")
        return ("ok", dt, rest)

    return None

def parse_only_time(text: str):
    """
    Обрабатывает время без даты:
    - 'в HH[:MM]'
    - 'HH[:MM] утром/вечером/...' (без 'в')
    Учитывает 'сегодня' для выбора ближайшего сегодняшнего слота.
    """
    s = norm(text)
    now = datetime.now(tz).replace(second=0, microsecond=0)

    # bare time with meridian (без 'в'): "10 утра"
    mb = RX_BARE_TIME_WITH_MER.search(s)
    if mb:
        h = int(mb.group(1)); mm = int(mb.group(2) or 0); mer = mb.group(3)
        dt = now.replace(hour=apply_meridian(h, mer) % 24, minute=mm)
        if dt <= now: dt += timedelta(days=1)
        rest = (s[:mb.start()] + s[mb.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    # classic: "в 10[:30]"
    m = RX_ONLY_TIME.search(s)
    if not m:
        return None
    h = int(m.group(1)); mm = int(m.group(2) or 0)

    # если указана часть суток где-то в тексте
    mer_m = RX_ANY_MER.search(s)
    if mer_m:
        mer = mer_m.group(1)
        dt = now.replace(hour=apply_meridian(h, mer) % 24, minute=mm)
        if dt <= now: dt += timedelta(days=1)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    # если 'сегодня' и двусмысленный час — выбираем ближайший СЕГОДНЯ
    if RX_TODAY.search(s) and not hour_is_unambiguous(h):
        dt1 = now.replace(hour=h % 24, minute=mm)
        dt2 = now.replace(hour=(h + 12) % 24, minute=mm)
        candidates = [dt for dt in (dt1, dt2) if dt >= now and dt.date() == now.date()]
        if candidates:
            dt = min(candidates)
            rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
            return ("ok", dt, rest)
        # если сегодня уже никак — завтра в тот же час
        dt = (now + timedelta(days=1)).replace(hour=h % 24, minute=mm)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    # однозначный 24-часовой час
    if hour_is_unambiguous(h):
        dt = now.replace(hour=h % 24, minute=mm)
        if dt <= now: dt += timedelta(days=1)
        rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
        return ("ok", dt, rest)

    # двусмысленный без 'сегодня' — предложим выбор
    v1 = now.replace(hour=h % 24, minute=mm)
    v2 = now.replace(hour=(h + 12) % 24, minute=mm)
    if v1 <= now: v1 += timedelta(days=1)
    if v2 <= now: v2 += timedelta(days=1)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return ("amb", rest, soonest([v1, v2]))

def parse_exact_hour(text: str):
    s = norm(text); m = RX_EXACT_HOUR.search(s)
    if not m: return None
    h = int(m.group(1))
    now = datetime.now(tz).replace(second=0, microsecond=0)
    dt = now.replace(hour=h % 24, minute=0)
    if dt <= now: dt += timedelta(days=1)
    rest = (s[:m.start()] + s[m.end():]).strip(" ,.-")
    return dt, rest

def parse_dot_date(text: str):
    s = norm(text); m = RX_DOT_DATE.search(s)
    if not m: return None
    dd, mm = int(m.group(1)), int(m.group(2))
    yy, hh, minu, mer = m.group(3), m.group(4), m.group(5), m.group(6)
    now = datetime.now(tz)
    yyyy = now.year if not yy else (int(yy) + 2000 if len(yy)==2 else int(yy))
    try:
        base = date(yyyy, mm, dd)
    except ValueError:
        return None
    rest = RX_DOT_DATE.sub("", s, count=1).strip(" ,.-")

    if not hh:
        mer_m = RX_ANY_MER.search(s)
        if mer_m:
            dt = mk_dt(base, part_of_day_defaults(mer_m.group(1)), 0)
            return ("ok", dt, rest)
        return ("day", base, rest)

    h = int(hh); minute = int(minu or 0)
    if mer:
        dt = mk_dt(base, apply_meridian(h, mer), minute)
        return ("ok", dt, rest)
    if hour_is_unambiguous(h):
        dt = mk_dt(base, h % 24, minute)
        return ("ok", dt, rest)
    dt1 = mk_dt(base, h % 24, minute)
    dt2 = mk_dt(base, (h + 12) % 24, minute)
    return ("amb", rest, soonest([dt1, dt2]))

def parse_month_date(text: str):
    s = norm(text); m = RX_MONTH_DATE.search(s)
    if not m: return None
    dd = int(m.group(1)); mon = m.group(2).lower()
    if mon not in MONTHS: return None
    mm = MONTHS[mon]
    hh, minu = m.group(3), m.group(4)
    mer = (m.group(5) or "").lower() if m.group(5) else None

    now = datetime.now(tz); yyyy = now.year
    try:
        base = date(yyyy, mm, dd)
    except ValueError:
        return None
    if base < now.date():
        try: base = date(yyyy + 1, mm, dd)
        except ValueError: return None

    rest = RX_MONTH_DATE.sub("", s, count=1).strip(" ,.-")

    if not hh:
        mer_m = RX_ANY_MER.search(s)
        if mer_m:
            dt = mk_dt(base, part_of_day_defaults(mer_m.group(1)), 0)
            return ("ok", dt, rest)
        return ("day", base, rest)

    h = int(hh); minute = int(minu or 0)
    if mer:
        dt = mk_dt(base, apply_meridian(h, mer), minute)
        return ("ok", dt, rest)
    if hour_is_unambiguous(h):
        dt = mk_dt(base, h % 24, minute)
        return ("ok", dt, rest)
    dt1 = mk_dt(base, h % 24, minute)
    dt2 = mk_dt(base, (h + 12) % 24, minute)
    return ("amb", rest, soonest([dt1, dt2]))

def parse_day_of_month(text: str):
    s = norm(text); m = RX_DAY_OF_MONTH.search(s)
    if not m: return None
    dd = int(m.group(1)); hh, minu = m.group(2), m.group(3)
    mer = (m.group(4) or "").lower() if m.group(4) else None
    now = datetime.now(tz); base = nearest_future_day(dd, now)
    rest = RX_DAY_OF_MONTH.sub("", s, count=1).strip(" ,.-")

    if not hh:
        mer_m = RX_ANY_MER.search(s)
        if mer_m:
            dt = mk_dt(base, part_of_day_defaults(mer_m.group(1)), 0)
            return ("ok", dt, rest)
        return ("day", base, rest)

    h = int(hh); minute = int(minu or 0)
    if mer:
        dt = mk_dt(base, apply_meridian(h, mer), minute)
        return ("ok", dt, rest)
    if hour_is_unambiguous(h):
        dt = mk_dt(base, h % 24, minute)
        return ("ok", dt, rest)
    dt1 = mk_dt(base, h % 24, minute)
    dt2 = mk_dt(base, (h + 12) % 24, minute)
    return ("amb", rest, soonest([dt1, dt2]))

# --------- COMMANDS ---------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет! Я бот-напоминалка.\n"
        "Понимаю: «24 мая в 19», «1 числа в 7», «через 30 минут/час», "
        "«завтра в 6», «в 17 часов», «в 21», «24.05 21:30», "
        "«завтра вечером», «24 мая утром», «в 7 вечером», «10 утра».\n"
        "Если время 13–23 или 00 — считаю его точным и не спрашиваю уточнений.\n"
        "/list — список, /ping — проверка, /cancel — отменить уточнение."
    )

@dp.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong ✅")

@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    uid = m.from_user.id
    if uid in PENDING:
        PENDING.pop(uid, None)
        await m.answer("Ок, отменил уточнение. Пиши новое напоминание.")
    else:
        await m.answer("Нечего отменять, я готов 🤝")

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

# --------- MAIN HANDLER ---------
@dp.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    text = norm(m.text)

    # режим уточнения
    if uid in PENDING:
        st = PENDING[uid]

        if text.lower() in ("отмена", "cancel", "/cancel"):
            PENDING.pop(uid, None)
            await m.reply("Ок, отменил уточнение. Пиши новое напоминание.")
            return

        if st.get("variants") and text_looks_like_new_request(text):
            PENDING.pop(uid, None)
        elif st.get("variants"):
            await m.reply("Нажмите кнопку ниже ⬇️", reply_markup=kb_variants(st["variants"]))
            return
        elif st.get("base_date"):
            mt = re.search(r"(?:^|\bв\s*)(\d{1,2})(?::(\d{2}))?\s*(утром|дн[её]м|дня|вечером|ночью|ночи)?\b", text, re.I)
            if not mt:
                mer_m = RX_ANY_MER.search(text)
                if mer_m:
                    h = part_of_day_defaults(mer_m.group(1)); minute = 0
                    dt = mk_dt(st["base_date"], h, minute)
                else:
                    await m.reply("Во сколько?")
                    return
            else:
                h = int(mt.group(1)); minute = int(mt.group(2) or 0); mer = mt.group(3)
                if mer:
                    h = apply_meridian(h, mer)
                dt = mk_dt(st["base_date"], h, minute)
            desc = st.get("description", "Напоминание")
            PENDING.pop(uid, None)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
            plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
            return
        # PENDING очищен — продолжаем как новое сообщение

    # 1) относительное
    r = parse_relative(text)
    if r:
        dt, rest = r; desc = clean_desc(rest or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
        plan(REMINDERS[-1])
        await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
        return

    # 2) в это же время …
    r = parse_same_time(text)
    if r:
        dt, rest = r; desc = clean_desc(rest or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
        plan(REMINDERS[-1])
        await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
        return

    # 3) сегодня/завтра/послезавтра …
    r = parse_dayword_time(text)
    if r:
        tag = r[0]
        if tag == "ok":
            _, dt, rest = r; desc = clean_desc(rest or text)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
            plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
            return
        _, rest, variants = r
        desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "variants": variants, "repeat":"none"}
        await m.reply(f"Уточните, во сколько напомнить «{desc}»?", reply_markup=kb_variants(variants))
        return

    # 4) конкретные даты
    for parser in (parse_dot_date, parse_month_date, parse_day_of_month):
        r = parser(text)
        if r:
            tag = r[0]
            if tag == "ok":
                _, dt, rest = r; desc = clean_desc(rest or text)
                REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
                plan(REMINDERS[-1])
                await m.reply(f"Готово. Напомню: «{desc}» {fmt_dt(dt)}")
                return
            if tag == "amb":
                _, rest, variants = r; desc = clean_desc(rest or text)
                PENDING[uid] = {"description": desc, "variants": variants, "repeat":"none"}
                await m.reply(f"Уточните, во сколько напомнить «{desc}»?", reply_markup=kb_variants(variants))
                return
            _, base, rest = r; desc = clean_desc(rest or text)
            PENDING[uid] = {"description": desc, "base_date": base, "repeat":"none"}
            await m.reply(f"Окей, {base.strftime('%d.%m')}. В какое время?")
            return

    # 5) «в HH часов»
    r = parse_exact_hour(text)
    if r:
        dt, rest = r; desc = clean_desc(rest or text)
        REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
        plan(REMINDERS[-1])
        await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
        return

    # 6) «в HH[:MM]» / «HH[:MM] утром …»
    r = parse_only_time(text)
    if r:
        tag = r[0]
        if tag == "ok":
            _, dt, rest = r; desc = clean_desc(rest or text)
            REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
            plan(REMINDERS[-1])
            await m.reply(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
            return
        _, rest, variants = r; desc = clean_desc(rest or text)
        PENDING[uid] = {"description": desc, "variants": variants, "repeat":"none"}
        await m.reply(f"Уточните, во сколько напомнить «{desc}»?", reply_markup=kb_variants(variants))
        return

    await m.reply("Не понял дату/время. Примеры: «24.05 19:00», «24 мая вечером», «завтра в 7 утра», «в 17 часов», «через 30 минут», «10 утра».")

# --------- CALLBACK ---------
@dp.callback_query(F.data.startswith("time|"))
async def choose_time(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in PENDING or not PENDING[uid].get("variants"):
        await cb.answer("Нет активного уточнения")
        return
    try:
        iso = cb.data.split("|", 1)[1]
        dt = datetime.fromisoformat(iso)
        dt = tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)
    except Exception:
        await cb.answer("Ошибка выбора времени")
        return
    desc = PENDING[uid].get("description", "Напоминание")
    PENDING.pop(uid, None)
    REMINDERS.append({"user_id": uid, "text": desc, "remind_dt": dt, "repeat":"none"})
    plan(REMINDERS[-1])
    try:
        await cb.message.edit_text(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
    except Exception:
        await cb.message.answer(f"Принял. Напомню: «{desc}» {fmt_dt(dt)}")
    await cb.answer("Установлено ✅")

# --------- RUN ---------
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
