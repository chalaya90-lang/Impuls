import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Impuls")

# Токен бери зі свого оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- Тимчасова база даних (краще потім замінити на SQLite) ---
protected_user_id: Optional[int] = None
emergency_contacts = {} # {user_id: phone_number}
monitoring_active = True
waiting_for_response = False

# --- Клавіатури ---

def get_main_keyboard():
    # Головне меню для подруги
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Надіслати мою локацію", request_location=True)],
            [KeyboardButton(text="📞 Додати екстрений контакт", request_contact=True)],
            [KeyboardButton(text="⚙️ Статус моніторингу")]
        ],
        resize_keyboard=True
    )

def get_emergency_keyboard():
    # Меню для тих, хто рятує (екстрені контакти)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❓ Перевірити, чи все добре?")]
        ],
        resize_keyboard=True
    )

def get_check_inline():
    # Кнопка підтвердження безпеки
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я в порядку! Все добре", callback_data="im_ok")
    ]])

# --- Логіка перевірок ---

async def send_check_request():
    global waiting_for_response
    if not protected_user_id or not monitoring_active:
        return

    waiting_for_response = True
    await bot.send_message(
        protected_user_id,
        "🔔 ПЕРЕВІРКА БЕЗПЕКИ\nБудь ласка, підтвердь, що ти на зв'язку!",
        reply_markup=get_check_inline()
    )

    # Запускаємо таймер на 10 хвилин до тривоги
    scheduler.add_job(
        send_alarm,
        'date',
        run_date=datetime.now() + timedelta(minutes=10),
        id='alarm_job',
        replace_existing=True
    )

async def send_alarm():
    global waiting_for_response
    if not waiting_for_response:
        return

    alert_msg = "🚨 УВАГА! ТРИВОГА! Подруга не відповіла на перевірку безпеки!"
    
    for contact_id in emergency_contacts.keys():
        try:
            await bot.send_message(contact_id, alert_msg)
        except Exception as e:
            logger.error(f"Не вдалося сповістити {contact_id}: {e}")

# --- Хендлери ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    global protected_user_id
    # Якщо хочеш розділити ролі, тут можна додати логіку
    await message.answer(
        "Привіт! Я твій бот-захисник. Використовуй кнопки нижче для налаштування.",
        reply_markup=get_main_keyboard()
    )

# Додавання екстреного контакту через кнопку телефону
@dp.message(F.contact)
async def handle_contact(message: Message):
    contact_id = message.contact.user_id
    if not contact_id:
        await message.answer("Цей контакт не має Telegram ID, я не зможу йому написати.")
        return
    
    emergency_contacts[contact_id] = message.contact.phone_number
    await message.answer(f"✅ Контакт {message.contact.first_name} додано до списку рятівників!")
    
    # Сповіщаємо рятівника
    try:
        await bot.send_message(
            contact_id, 
            "Ти доданий як екстрений контакт! Тепер ти можеш перевіряти статус подруги.",
            reply_markup=get_emergency_keyboard()
        )
    except:
        await message.answer("⚠️ Рятівник має спочатку сам запустити цього бота, щоб я міг йому писати.")
# Обробка локації
@dp.message(F.location)
async def handle_location(message: Message):
    lat = message.location.latitude
    lon = message.location.longitude
    await message.answer(f"📍 Локацію отримано! Координати збережено.")
    # Тут можна додати відправку локації контактам, якщо це тривога

# Кнопка від рятівника: "Перевірити чи все добре?"
@dp.message(F.text == "❓ Перевірити, чи все добре?")
async def force_check(message: Message):
    if message.from_user.id in emergency_contacts:
        await message.answer("Запит надіслано! Чекаємо на відповідь.")
        await send_check_request()
    else:
        await message.answer("У вас немає прав для цієї дії.")

# Обробка кнопки "Я в порядку"
@dp.callback_query(F.data == "im_ok")
async def handle_ok(callback: CallbackQuery):
    global waiting_for_response
    waiting_for_response = False
    
    try:
        scheduler.remove_job('alarm_job')
    except:
        pass
    
    await callback.message.edit_text("✅ Дякую! Я передам усім, що ти в безпеці.")
    
    # Сповіщаємо контакти, що все ок
    for contact_id in emergency_contacts.keys():
        await bot.send_message(contact_id, "✅ Подруга підтвердила, що вона в безпеці.")

async def main():
    # Запуск регулярної перевірки раз на годину
    scheduler.add_job(send_check_request, 'interval', minutes=60)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
