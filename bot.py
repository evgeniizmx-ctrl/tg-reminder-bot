import os
import re
import asyncio
import aiosqlite
from datetime import datetime, timedelta, date
import pytz
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================== ОКРУЖЕНИЕ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_TZ = os.getenv("APP_TZ", "Europe/Moscow")
DB_PATH = os.getenv("DB_PATH", "reminders.db")
tz = pytz.timezone(APP_TZ)

# ================== ИНИЦИАЛИЗАЦИЯ ==================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=APP_TZ)

# Временное хранилище для состояний (можно заменить на Redis)
PENDING: Dict[int, dict] = {}

# ================== БАЗА ДАННЫХ ==================
async def init_db():
    """Инициализация базы данных"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                remind_dt TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_states (
                user_id INTEGER PRIMARY KEY,
                data TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def save_reminder(user_id: int, text: str, remind_dt: datetime) -> int:
    """Сохранение напоминания в БД"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO reminders (user_id, text, remind_dt) VALUES (?, ?, ?)",
            (user_id, text, remind_dt.isoformat())
        )
        await db.commit()
        return cursor.lastrowid

async def get_user_reminders(user_id: int) -> List[dict]:
    """Получение напоминаний пользователя"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, text, remind_dt, status FROM reminders WHERE user_id = ? ORDER BY remind_dt",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "text": row[1],
                "remind_dt": datetime.fromisoformat(row[2]),
                "status": row[3]
            }
            for row in rows
        ]

async def save_pending_state(user_id: int, data: dict):
    """Сохранение состояния ожидания"""
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pending_states (user_id, data) VALUES (?, ?)",
            (user_id, json.dumps(data))
        )
        await db.commit()

async def get_pending_state(user_id: int) -> Optional[dict]:
    """Получение состояния ожидания"""
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT data FROM pending_states WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

async def delete_pending_state(user_id: int):
    """Удаление состояния ожидания"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM pending_states WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

async def get_pending_reminders() -> List[dict]:
    """Получение напоминаний, готовых к отправке"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, user_id, text, remind_dt FROM reminders WHERE status = 'pending' AND remind_dt <= ?",
            (datetime.now(tz).isoformat(),)
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "user_id": row[1],
                "text": row[2],
                "remind_dt": datetime.fromisoformat(row[3])
            }
            for row in rows
        ]

async def update_reminder_status(reminder_id: int, status: str):
    """Обновление статуса напоминания"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET status = ? WHERE id = ?",
            (status, reminder_id)
        )
        await db.commit()

# ================== УТИЛИТЫ ==================
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "", flags=re.UNICODE).strip()

def clean_desc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(напомни(те)?|пожалуйста)\b[\s,:-]*", "", s, flags=re.I)
    s = re.sub(r"^(о|про|насч[её]т)\s+", "", s, flags=re.I)
    return s.strip() or "Напоминание"

async def send_reminder(user_id: int, text: str, reminder_id: int):
    """Отправка напоминания с обработкой ошибок"""
    try:
        await bot.send_message(user_id, f"🔔 Напоминание: {text}")
        await update_reminder_status(reminder_id, "sent")
        print(f"Напоминание {reminder_id} отправлено пользователю {user_id}")
    except Exception as e:
        print(f"Ошибка отправки напоминания {reminder_id}: {e}")
        await update_reminder_status(reminder_id, "failed")
        # Можно добавить логику повторных попыток

async def check_reminders():
    """Фоновая задача для проверки и отправки напоминаний"""
    while True:
        try:
            reminders = await get_pending_reminders()
            for reminder in reminders:
                asyncio.create_task(
                    send_reminder(reminder["user_id"], reminder["text"], reminder["id"])
                )
            await asyncio.sleep(10)  # Проверяем каждые 10 секунд
        except Exception as e:
            print(f"Ошибка в фоновой задаче проверки напоминаний: {e}")
            await asyncio.sleep(30)

def plan(reminder_id: int, user_id: int, text: str, remind_dt: datetime):
    """Планирование напоминания (для совместимости)"""
    # В новой архитекции используется фоновая задача check_reminders
    # Эта функция оставлена для совместимости со старым кодом
    pass

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
    t = f"{h12}:{m:02d}" if m else f"{h12}"
    return f"{dword} в {t} {mer}"

def kb_variants(dts: list[datetime]) -> InlineKeyboardMarkup:
    rows = []
    for dt in soonest(dts):
        rows.append([InlineKeyboardButton(text=human_label(dt),
                                          callback_data=f"time|{dt.isoformat()}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ================== РУССКИЕ МЕСЯЦЫ ==================
MONTHS = {
    "января":1, "февраля":2, "марта":3, "апреля":4, "мая":5, "июня":6,
    "июля":7, "августа":8, "сентября":9, "октября":10, "ноября":11, "декабря":12,
    "январь":1,"февраль":2,"март":3,"апрель":4,"май":5,"июнь":6,"июль":7,
    "август":8,"сентябрь":9,"октябрь":10,"ноябрь":11,"декабрь":12,
}

def nearest_future_day(day: int, now: datetime) -> date:
    import calendar
    y, m = now.year, now.month
    
    # Пытаемся найти день в текущем месяце
    try:
        candidate = date(y, m, day)
        if candidate > now.date():
            return candidate
    except ValueError:
        pass  # Дня нет в текущем месяце
    
    # Если не вышло, идем в следующие месяцы
    month = m + 1
    year = y
    while True:
        if month > 12:
            month = 1
            year += 1
        
        # Узнаем, сколько дней в целевом месяце
        _, last_day = calendar.monthrange(year, month)
        target_day = min(day, last_day)
        
        try:
            return date(year, month, target_day)
        except ValueError:
            month += 1
            if month > 12:
                month = 1
                year += 1

# ================== ПАРСЕРЫ ==================
# ... (все парсеры остаются без изменений, как в оригинальном коде) ...
# Парсеры: RX_HALF_HOUR, RX_REL, RX_REL_SINGULAR, RX_SAME_TIME, RX_TMR, 
# RX_ATMR, RX_IN_N_DAYS, RX_DAY_WORD_TIME, RX_ONLY_TIME, RX_DOT_DATE,
# RX_MONTH_DATE, RX_DAY_OF_MONTH, parse_relative, parse_same_time,
# apply_meridian, parse_dayword_time, parse_only_time, parse_dot_date,
# parse_month_date, parse_day_of_month

# ================== КОМАНДЫ ==================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет! Я бот-напоминалка.\n"
        "Понимаю: «24 мая в 19», «1 числа в 7», «через 30 минут/час/минуту», "
        "«завтра в 6», «в это же время завтра».\n"
        "/list — список, /ping — проверка."
    )

@dp.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong ✅")

@dp.message(Command("list"))
async def cmd_list(m: Message):
    uid = m.from_user.id
    items = await get_user_reminders(uid)
    if not items:
        await m.answer("Пока нет напоминаний.")
        return
    
    lines = []
    for item in items:
        status = "✅" if item["status"] == "sent" else "⏰" if item["status"] == "pending" else "❌"
        lines.append(f"{status} {item['text']} — {item['remind_dt'].strftime('%d.%m %H:%M')}")
    
    await m.answer("\n".join(lines) if lines else "Нет активных напоминаний.")

@dp.message(Command("clear"))
async def cmd_clear(m: Message):
    """Очистка всех напоминаний пользователя"""
    # Реализация очистки напоминаний из БД
    await m.answer("Функция очистки в разработке")

# ================== ОСНОВНАЯ ЛОГИКА ==================
@dp.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    text = norm(m.text)

    # Проверяем, есть ли сохраненное состояние в БД
    pending_state = await get_pending_state(uid)
    if pending_state:
        PENDING[uid] = pending_state

    # если ждём уточнение
    if uid in PENDING:
        st = PENDING[uid]
        if st.get("variants"):
            await m.reply("Нажмите кнопку ниже ⬇️")
            return
        if st.get("base_date"):
            mt = re.search(r"(?:^|\bв\s*)(\d{1,2})(?::(\d{2}))?\s*(утра|дня|вечера|ночи)?\b", text, re.I)
            if not mt:
                await m.reply("Во сколько?")
                return
            h = int(mt.group(1)); minute = int(mt.group(2) or 0); mer = mt.group(3)
            hh = apply_meridian(h, mer)
            dt = mk_dt(st["base_date"], hh, minute)
            desc = st.get("description", "Напоминание")
            
            # Сохраняем в БД вместо глобального списка
            await save_reminder(uid, desc, dt)
            await delete_pending_state(uid)
            PENDING.pop(uid, None)
            
            await m.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({APP_TZ})")
            return
        PENDING.pop(uid, None)
        await delete_pending_state(uid)

    # Парсеры (остаются без изменений, но теперь сохраняют в БД)
    # ... (код парсеров такой же, но с заменой REMINDERS.append на save_reminder) ...
    
    # Пример для parse_relative:
    r = parse_relative(text)
    if r:
        dt, rest = r
        desc = clean_desc(rest or text)
        await save_reminder(uid, desc, dt)
        await m.reply(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({APP_TZ})")
        return

    # Аналогично для других парсеров...
    # parse_same_time, parse_dayword_time, parse_only_time, parse_dot_date,
    # parse_month_date, parse_day_of_month

    await m.reply("Не понял дату/время. Примеры: «24.05 19:00», «24 мая в 19», «1 числа в 7», «через 30 минут», «завтра в 6».")

# ================== КНОПКИ ==================
@dp.callback_query(F.data.startswith("time|"))
async def choose_time(cb: CallbackQuery):
    uid = cb.from_user.id
    
    # Проверяем состояние в БД
    pending_state = await get_pending_state(uid)
    if not pending_state or not pending_state.get("variants"):
        await cb.answer("Нет активного уточнения")
        return
        
    PENDING[uid] = pending_state
    
    try:
        iso = cb.data.split("|", 1)[1]
        dt = datetime.fromisoformat(iso)
        dt = tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)
    except Exception as e:
        print("parse cb time error:", e)
        await cb.answer("Ошибка выбора времени")
        return

    desc = PENDING[uid].get("description", "Напоминание")
    
    # Сохраняем в БД
    await save_reminder(uid, desc, dt)
    await delete_pending_state(uid)
    PENDING.pop(uid, None)

    try:
        await cb.message.edit_text(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({APP_TZ})")
    except Exception:
        await cb.message.answer(f"Принял. Напомню: «{desc}» в {dt.strftime('%d.%m %H:%M')} ({APP_TZ})")
    await cb.answer("Установлено ✅")

# ================== ЗАПУСК ==================
async def main():
    # Инициализация БД
    await init_db()
    
    # Запуск фоновой задачи для проверки напоминаний
    asyncio.create_task(check_reminders())
    
    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
