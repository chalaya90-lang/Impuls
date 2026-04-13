import asyncio
import logging
import os
from datetime import datetime, time
from typing import Optional

import pytz
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SafetyBot")

# ================= НАЛАШТУВАННЯ =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ТУТ")
TIMEZONE = pytz.timezone("Europe/Kyiv")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# ================= СТАН =================

# ID головної користувачки бота
protected_user_id: Optional[int] = None

# Екстрені контакти: {user_id: {"name": str, "phone": str}}
emergency_contacts: dict = {}

# Чи активний моніторинг
monitoring_active: bool = False

# Чекаємо відповіді після пінгу
waiting_for_response: bool = False

# Інтервал пінгу в хвилинах (за замовчуванням 60)
ping_interval_minutes: int = 60

# Нічний режим: 23:00–07:00 за замовчуванням
quiet_start: Optional[time] = time(23, 0)
quiet_end: Optional[time] = time(7, 0)

# Остання відома локація
last_location: Optional[dict] = None

# Стани введення
user_states: dict = {}

# ================= ДОПОМІЖНІ =================

def is_quiet_time() -> bool:
    """Чи зараз нічний тихий режим."""
    if quiet_start is None or quiet_end is None:
        return False
    now = datetime.now(TIMEZONE).time()
    if quiet_start <= quiet_end:
        return quiet_start <= now <= quiet_end
    else:
        # Перехід через північ, напр. 23:00–07:00
        return now >= quiet_start or now <= quiet_end

def is_protected(user_id: int) -> bool:
    return user_id == protected_user_id

def is_contact(user_id: int) -> bool:
    return user_id in emergency_contacts

def monitoring_status_text() -> str:
    status = "🟢 Увімкнено" if monitoring_active else "🔴 Вимкнено"
    interval = f"⏱ Пінг кожні {ping_interval_minutes} хв"
    quiet = (
        f"🌙 Тихий режим: {quiet_start.strftime('%H:%M')}–{quiet_end.strftime('%H:%M')}"
        if quiet_start and quiet_end else "🌙 Тихий режим: вимкнено"
    )
    contacts = (
        "\n".join(f"  • {v['name']} ({v['phone']})" for v in emergency_contacts.values())
        if emergency_contacts else "  (немає)"
    )
    return (
        f"📊 Статус моніторингу:\n\n"
        f"{status}\n{interval}\n{quiet}\n\n"
        f"📋 Контакти:\n{contacts}"
    )

# ================= КЛАВІАТУРИ =================

def main_kb() -> ReplyKeyboardMarkup:
    on_off = "🔴 Вимкнути моніторинг" if monitoring_active else "🟢 Увімкнути моніторинг"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🆘 СОС — ДОПОМОЖІТЬ!")],
            [KeyboardButton(text=on_off)],
            [KeyboardButton(text="📍 Поділитись локацією", request_location=True)],
            [KeyboardButton(text="👤 Додати контакт", request_contact=True)],
            [KeyboardButton(text="⚙️ Налаштування"), KeyboardButton(text="📊 Статус")],
        ],
        resize_keyboard=True,
    )

def settings_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏱ Змінити інтервал пінгу")],
            [KeyboardButton(text="🌙 Налаштувати тихий режим")],
            [KeyboardButton(text="🗑 Видалити контакт")],
            [KeyboardButton(text="🔙 Назад")],
        ],
        resize_keyboard=True,
    )

def ok_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Все добре, я в безпеці!", callback_data="im_ok")
    ]])

# ================= КОМАНДИ =================

@router.message(Command("start"))
async def cmd_start(msg: Message):
    global protected_user_id

    # Якщо це перший запуск — реєструємо як головну користувачку
    if protected_user_id is None:
        protected_user_id = msg.from_user.id
        await msg.answer(
            "👋 Привіт! Я твій особистий бот безпеки.\n\n"
            "Як це працює:\n"
            "🟢 Увімкни моніторинг — і я буду регулярно питати чи все добре\n"
            "✅ Натискай «Все добре» коли отримуєш пінг\n"
            "🆘 Якщо страшно — натисни СОС і я одразу сповіщу твої контакти з геолокацією\n"
            "👤 Додай довірених людей через кнопку «Додати контакт'\n\n"
            "Ти в безпеці. Я поруч 💙",
            reply_markup=main_kb(),
        )
        return

    # Якщо це екстрений контакт
    if is_contact(msg.from_user.id):
        name = emergency_contacts[msg.from_user.id]["name"]
        await msg.answer(
            f"👋 {name}, ти в списку довірених контактів.\n"
            "Якщо твоя подруга натисне СОС або не відповість на пінг — "
            "ти отримаєш сповіщення з її геолокацією.",
        )
        return

    await msg.answer("Привіт! Напиши /start ще раз якщо виникли проблеми.")

# ================= МОНІТОРИНГ =================

@router.message(F.text.in_({"🟢 Увімкнути моніторинг", "🔴 Вимкнути моніторинг"}))
async def toggle_monitoring(msg: Message):
    global monitoring_active
    if not is_protected(msg.from_user.id):
        return

    monitoring_active = not monitoring_active

    if monitoring_active:
        # Запускаємо планувальник
        if scheduler.get_job("ping_job"):
            scheduler.remove_job("ping_job")
        scheduler.add_job(
            send_ping,
            "interval",
            minutes=ping_interval_minutes,
            id="ping_job",
            replace_existing=True,
        )
        await msg.answer(
            f"🟢 Моніторинг увімкнено!\n"
            f"Буду питати кожні {ping_interval_minutes} хв.\n"
            f"Не забувай відповідати ✅",
            reply_markup=main_kb(),
        )
    else:
        if scheduler.get_job("ping_job"):
            scheduler.remove_job("ping_job")
        if scheduler.get_job("alarm_job"):
            scheduler.remove_job("alarm_job")
        await msg.answer("🔴 Моніторинг вимкнено.", reply_markup=main_kb())

# ================= ПІНГ =================

async def send_ping():
    global waiting_for_response
    if not protected_user_id or not monitoring_active:
        return
    if is_quiet_time():
        return

    waiting_for_response = True
    await bot.send_message(
        protected_user_id,
        "🔔 Привіт! Як ти?\n\nНатисни кнопку нижче щоб підтвердити що все добре.\n"
        "Якщо не відповіси протягом 10 хвилин — я сповіщу твої контакти.",
        reply_markup=ok_inline_kb(),
    )

    # Таймер тривоги через 10 хвилин
    from datetime import timedelta
    alarm_time = datetime.now(TIMEZONE) + timedelta(minutes=10)
    scheduler.add_job(
        send_alarm,
        "date",
        run_date=alarm_time,
        id="alarm_job",
        replace_existing=True,
    )

@router.callback_query(F.data == "im_ok")
async def handle_ok(callback: CallbackQuery):
    global waiting_for_response
    if not is_protected(callback.from_user.id):
        await callback.answer("Ця кнопка не для тебе 😊")
        return

    waiting_for_response = False

    try:
        scheduler.remove_job("alarm_job")
    except Exception:
        pass

    await callback.message.edit_text("✅ Чудово! Рада що ти в безпеці 💙")
    await callback.answer("✅ Відповідь прийнято!")

    # Сповіщаємо контакти що все добре
    for cid in emergency_contacts:
        try:
            await bot.send_message(cid, "✅ Все добре — подруга підтвердила що вона в безпеці 💙")
        except Exception:
            pass

async def send_alarm():
    global waiting_for_response
    if not waiting_for_response:
        return

    alert = (
        "🚨 УВАГА! ТРИВОГА!\n\n"
        "Твоя подруга не відповіла на перевірку безпеки протягом 10 хвилин!\n"
        "Зв'яжись з нею негайно!"
    )

    for cid in emergency_contacts:
        try:
            await bot.send_message(cid, alert)
            # Надсилаємо останню відому локацію
            if last_location:
                await bot.send_location(
                    cid,
                    latitude=last_location["lat"],
                    longitude=last_location["lon"],
                )
        except Exception as e:
            logger.error(f"Не вдалось надіслати тривогу {cid}: {e}")

# ================= СОС =================

@router.message(F.text == "🆘 СОС — ДОПОМОЖІТЬ!")
async def sos(msg: Message):
    if not is_protected(msg.from_user.id):
        return

    if not emergency_contacts:
        await msg.answer(
            "⚠️ У тебе немає збережених контактів!\n"
            "Додай довірених людей через кнопку «👤 Додати контакт»"
        )
        return

    sos_text = (
        "🚨🚨🚨 СОС! ПОТРІБНА ДОПОМОГА! 🚨🚨🚨\n\n"
        "Твоя подруга натиснула кнопку SOS!\n"
        "Зв'яжись з нею НЕГАЙНО!"
    )

    sent_count = 0
    for cid, info in emergency_contacts.items():
        try:
            await bot.send_message(cid, sos_text)
            if last_location:
                await bot.send_location(
                    cid,
                    latitude=last_location["lat"],
                    longitude=last_location["lon"],
                )
            sent_count += 1
        except Exception as e:
            logger.error(f"СОС не надіслано {cid}: {e}")

    await msg.answer(
        f"🆘 СОС надіслано {sent_count} контакт(ам)!\n"
        f"Допомога вже в дорозі 💙\n\n"
        f"Якщо можеш — поділись своєю поточною локацією кнопкою нижче.",
        reply_markup=main_kb(),
    )

# ================= ЛОКАЦІЯ =================

@router.message(F.location)
async def handle_location(msg: Message):
    global last_location
    last_location = {
        "lat": msg.location.latitude,
        "lon": msg.location.longitude,
        "time": datetime.now(TIMEZONE).strftime("%d.%m %H:%M"),
    }

    if is_protected(msg.from_user.id):
        await msg.answer(
            f"📍 Локацію збережено ({last_location['time']})\n"
            "Якщо спрацює тривога — контакти отримають це місце.",
            reply_markup=main_kb(),
        )

# ================= КОНТАКТИ =================

@router.message(F.contact)
async def handle_contact(msg: Message):
    if not is_protected(msg.from_user.id):
        return

    contact = msg.contact
    if not contact.user_id:
        await msg.answer(
            "⚠️ Цей контакт не має Telegram ID.\n"
            "Попроси людину самостійно написати боту /start — тоді зможу її додати.",
            reply_markup=main_kb(),
        )
        return

    name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
    phone = contact.phone_number or "невідомо"

    emergency_contacts[contact.user_id] = {"name": name, "phone": phone}

    await msg.answer(
        f"✅ {name} додано як довірений контакт!\n"
        f"Вони отримають сповіщення якщо щось піде не так.",
        reply_markup=main_kb(),
    )

    # Повідомляємо контакт
    try:
        await bot.send_message(
            contact.user_id,
            f"👋 Привіт, {name}!\n\n"
            "Тебе додано як довірений контакт бота безпеки.\n"
            "Якщо твоя подруга натисне СОС або не відповість на перевірку — "
            "ти отримаєш сповіщення з її геолокацією.\n\n"
            "Напиши /start щоб активувати отримання сповіщень.",
        )
    except Exception:
        await msg.answer(
            "⚠️ Не вдалось надіслати повідомлення контакту.\n"
            "Попроси їх написати боту /start щоб активувати сповіщення.",
            reply_markup=main_kb(),
        )

# ================= НАЛАШТУВАННЯ =================

@router.message(F.text == "⚙️ Налаштування")
async def settings(msg: Message):
    if not is_protected(msg.from_user.id):
        return
    await msg.answer("⚙️ Налаштування:", reply_markup=settings_kb())

@router.message(F.text == "📊 Статус")
async def status(msg: Message):
    if not is_protected(msg.from_user.id):
        return
    await msg.answer(monitoring_status_text(), reply_markup=main_kb())

@router.message(F.text == "⏱ Змінити інтервал пінгу")
async def change_interval(msg: Message):
    if not is_protected(msg.from_user.id):
        return
    user_states[msg.from_user.id] = "awaiting_interval"
    await msg.answer(
        "⏱ Введи інтервал у хвилинах (від 10 до 240):\n\n"
        "Наприклад: 30 — пінг кожні півгодини\n"
        "60 — кожну годину\n"
        "120 — кожні дві години",
        reply_markup=ReplyKeyboardRemove(),
    )

@router.message(F.text == "🌙 Налаштувати тихий режим")
async def change_quiet(msg: Message):
    if not is_protected(msg.from_user.id):
        return
    user_states[msg.from_user.id] = "awaiting_quiet"
    await msg.answer(
        "🌙 Введи тихий режим у форматі ГГ:ХХ-ГГ:ХХ\n\n"
        "Наприклад: 23:00-07:00\n"
        "Щоб вимкнути тихий режим — напиши: вимкнути",
        reply_markup=ReplyKeyboardRemove(),
    )

@router.message(F.text == "🗑 Видалити контакт")
async def delete_contact(msg: Message):
    if not is_protected(msg.from_user.id):
        return
    if not emergency_contacts:
        await msg.answer("У тебе поки немає контактів.", reply_markup=settings_kb())
        return

    buttons = [
        [InlineKeyboardButton(
            text=f"🗑 {info['name']} ({info['phone']})",
            callback_data=f"del_contact:{cid}"
        )]
        for cid, info in emergency_contacts.items()
    ]
    await msg.answer(
        "Оберіть контакт для видалення:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )

@router.callback_query(F.data.startswith("del_contact:"))
async def confirm_delete_contact(callback: CallbackQuery):
    if not is_protected(callback.from_user.id):
        return
    cid = int(callback.data.split(":")[1])
    name = emergency_contacts.get(cid, {}).get("name", "невідомо")
    emergency_contacts.pop(cid, None)
    await callback.message.edit_text(f"🗑 {name} видалено з контактів.")
    await callback.answer("Видалено")
    await bot.send_message(callback.from_user.id, "Список оновлено.", reply_markup=settings_kb())

@router.message(F.text == "🔙 Назад")
async def back(msg: Message):
    user_states.pop(msg.from_user.id, None)
    await msg.answer("Головне меню 👇", reply_markup=main_kb())

# ================= ОБРОБКА ВВЕДЕННЯ =================

@router.message()
async def text_handler(msg: Message):
    uid = msg.from_user.id
    state = user_states.get(uid)

    if state == "awaiting_interval":
        text = (msg.text or "").strip()
        if not text.isdigit():
            await msg.answer("Введи число хвилин, наприклад: 30")
            return
        minutes = int(text)
        if not (10 <= minutes <= 240):
            await msg.answer("Введи число від 10 до 240:")
            return

        global ping_interval_minutes
        ping_interval_minutes = minutes
        user_states.pop(uid, None)

        # Оновлюємо планувальник якщо моніторинг активний
        if monitoring_active and scheduler.get_job("ping_job"):
            scheduler.reschedule_job("ping_job", trigger="interval", minutes=minutes)

        await msg.answer(
            f"✅ Інтервал встановлено: кожні {minutes} хв.",
            reply_markup=settings_kb(),
        )
        return

    if state == "awaiting_quiet":
        text = (msg.text or "").strip().lower()
        if text == "вимкнути":
            global quiet_start, quiet_end
            quiet_start = None
            quiet_end = None
            user_states.pop(uid, None)
            await msg.answer("✅ Тихий режим вимкнено.", reply_markup=settings_kb())
            return

        try:
            parts = text.split("-")
            qs = datetime.strptime(parts[0].strip(), "%H:%M").time()
            qe = datetime.strptime(parts[1].strip(), "%H:%M").time()
            quiet_start = qs
            quiet_end = qe
            user_states.pop(uid, None)
            await msg.answer(
                f"✅ Тихий режим: {qs.strftime('%H:%M')}–{qe.strftime('%H:%M')}\n"
                "В цей час пінги надсилатись не будуть.",
                reply_markup=settings_kb(),
            )
        except Exception:
            await msg.answer(
                "Не зрозуміла формат. Введи так: 23:00-07:00\n"
                "Або напиши «вимкнути»"
            )
        return

    # Якщо нічого не підійшло
    if is_protected(uid):
        await msg.answer("Скористайся кнопками 👇", reply_markup=main_kb())

# ================= MAIN =================

async def main():
    scheduler.start()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
