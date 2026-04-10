import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- State ---
monitoring_active = False
protected_user_id: Optional[int] = None
emergency_contacts: list[int] = []  # Telegram user IDs
check_interval_minutes = 60
response_timeout_minutes = 10
last_check_message_id: Optional[int] = None
waiting_for_response = False
missed_checks = 0

def get_check_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я в порядку", callback_data="im_ok")
    ]])

async def send_check():
    global last_check_message_id, waiting_for_response, missed_checks

    if not monitoring_active or not protected_user_id:
        return

    waiting_for_response = True

    msg = await bot.send_message(
        chat_id=protected_user_id,
        text=(
            "🔔 *Перевірка безпеки*\n\n"
            "Натисни кнопку, щоб підтвердити що все добре.\n"
            f"У тебе є *{response_timeout_minutes} хвилин*."
        ),
        parse_mode="Markdown",
        reply_markup=get_check_keyboard()
    )
    last_check_message_id = msg.message_id

    # Schedule timeout check
    scheduler.add_job(
        check_timeout,
        'date',
        run_date=datetime.now() + timedelta(minutes=response_timeout_minutes),
        id='timeout_check',
        replace_existing=True
    )

async def check_timeout():
    global waiting_for_response, missed_checks

    if not waiting_for_response:
        return  # Was answered

    missed_checks += 1
    waiting_for_response = False

    logger.warning(f"No response! Missed checks: {missed_checks}")

    # Alert protected user
    if protected_user_id:
        await bot.send_message(
            protected_user_id,
            "⚠️ Час вийшов! Надсилаю тривогу екстреним контактам...",
        )

    # Alert emergency contacts
    if not emergency_contacts:
        if protected_user_id:
            await bot.send_message(
                protected_user_id,
                "❗ Екстрені контакти не додані! Додай через /add_contact",
            )
        return

    alert_text = (
        "🚨 *ТРИВОГА*\n\n"
        "Твій підзахисний контакт *не відповів* на перевірку безпеки!\n\n"
        f"⏰ Час: {datetime.now().strftime('%H:%M, %d.%m.%Y')}\n"
        f"❌ Пропущених перевірок поспіль: {missed_checks}\n\n"
        "Перевір чи все з ним/нею добре!"
    )

    for contact_id in emergency_contacts:
        try:
            await bot.send_message(contact_id, alert_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to notify contact {contact_id}: {e}")

# --- Handlers ---

@dp.callback_query(F.data == "im_ok")
async def handle_ok(callback: CallbackQuery):
    global waiting_for_response, missed_checks

    if callback.from_user.id != protected_user_id:
        await callback.answer("Це не твій бот 😊")
        return

    waiting_for_response = False
    missed_checks = 0

    # Remove timeout job
    try:
        scheduler.remove_job('timeout_check')
    except:
        pass

    await callback.message.edit_text("✅ Чудово! До наступної перевірки 💚")
    await callback.answer("Відмічено! Все добре 💚")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    global protected_user_id

    protected_user_id = message.from_user.id

    await message.answer(
        "🛡 *Бот безпеки активовано*\n\n"
        "Я буду регулярно перевіряти що з тобою все добре.\n\n"
        "*Команди:*\n"
        "/monitor\_on — запустити моніторинг\n"
        "/monitor\_off — зупинити моніторинг\n"
        "/add\_contact — додати екстрений контакт\n"
        "/contacts — список контактів\n"
        "/interval — змінити інтервал перевірок\n"
        "/status — поточний статус\n"
        "/check\_now — перевірка зараз\n\n"
        f"Твій ID: `{message.from_user.id}`",
        parse_mode="Markdown"
    )


@dp.message(Command("monitor_on"))
async def cmd_monitor_on(message: Message):
    global monitoring_active

    if message.from_user.id != protected_user_id:
        await message.answer("❌ Тільки захищений користувач може керувати ботом.")
        return

    if monitoring_active:
        await message.answer("✅ Моніторинг вже активний.")
        return

    monitoring_active = True

    # Schedule recurring checks
    scheduler.add_job(
        send_check,
        'interval',
        minutes=check_interval_minutes,
        id='regular_check',
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(minutes=check_interval_minutes)
    )

    await message.answer(
        f"🟢 Моніторинг запущено!\n"
        f"Перевірки кожні *{check_interval_minutes} хв*.\n"
        f"Час на відповідь: *{response_timeout_minutes} хв*.",
        parse_mode="Markdown"
    )


@dp.message(Command("monitor_off"))
async def cmd_monitor_off(message: Message):
    global monitoring_active

    if message.from_user.id != protected_user_id:
        await message.answer("❌ Тільки захищений користувач може керувати ботом.")
        return

    monitoring_active = False

    try:
        scheduler.remove_job('regular_check')
        scheduler.remove_job('timeout_check')
    except:
        pass

    await message.answer("🔴 Моніторинг зупинено.")


@dp.message(Command("add_contact"))
async def cmd_add_contact(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "Вкажи Telegram ID контакту:\n"
            "`/add_contact 123456789`\n\n"
            "Щоб дізнатись свій ID — перешли будь-яке повідомлення боту @userinfobot",
            parse_mode="Markdown"
        )
        return

    try:
        contact_id = int(parts[1])
    except ValueError:
        await message.answer("❌ ID має бути числом.")
        return

    if contact_id in emergency_contacts:
        await message.answer("✅ Цей контакт вже є в списку.")
        return

    emergency_contacts.append(contact_id)

    # Test message to contact
    try:
        await bot.send_message(
            contact_id,
            "🛡 Тебе додано як екстрений контакт у боті безпеки.\n"
            "Якщо підзахисна людина не відповість на перевірку — ти отримаєш тривогу."
        )
        await message.answer(f"✅ Контакт `{contact_id}` додано. Їм надіслано повідомлення.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(
            f"⚠️ Контакт додано, але не вдалось надіслати тестове повідомлення.\n"
            f"Переконайся що ця людина запустила бота командою /start.\n"
            f"Помилка: {e}"
        )


@dp.message(Command("contacts"))
async def cmd_contacts(message: Message):
    if not emergency_contacts:
        await message.answer("📋 Екстрених контактів немає.\nДодай: `/add_contact 123456789`", parse_mode="Markdown")
        return

    text = "📋 *Екстрені контакти:*\n"
    for i, c in enumerate(emergency_contacts, 1):
        text += f"{i}. `{c}`\n"
    text += "\nВидалити: `/remove_contact 123456789`"
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("remove_contact"))
async def cmd_remove_contact(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Вкажи ID: `/remove_contact 123456789`", parse_mode="Markdown")
        return

    try:
        contact_id = int(parts[1])
    except ValueError:
        await message.answer("❌ ID має бути числом.")
        return

    if contact_id in emergency_contacts:
        emergency_contacts.remove(contact_id)
        await message.answer(f"✅ Контакт `{contact_id}` видалено.", parse_mode="Markdown")
    else:
        await message.answer("❌ Такого контакту немає в списку.")


@dp.message(Command("interval"))
async def cmd_interval(message: Message):
    global check_interval_minutes

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            f"Поточний інтервал: *{check_interval_minutes} хв*\n"
            f"Змінити: `/interval 30` (від 5 до 1440 хв)",
            parse_mode="Markdown"
        )
        return

    try:
        mins = int(parts[1])
    except ValueError:
        await message.answer("❌ Вкажи число хвилин.")
        return

    if not (5 <= mins <= 1440):
        await message.answer("❌ Від 5 до 1440 хвилин.")
        return

    check_interval_minutes = mins

    if monitoring_active:
        scheduler.reschedule_job('regular_check', trigger='interval', minutes=mins)
        await message.answer(f"✅ Інтервал змінено на *{mins} хв*. Застосовано одразу.", parse_mode="Markdown")
    else:
        await message.answer(f"✅ Інтервал встановлено *{mins} хв*. Запусти `/monitor_on`.", parse_mode="Markdown")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    status = "🟢 Активний" if monitoring_active else "🔴 Зупинений"
    waiting = "⏳ Так" if waiting_for_response else "✅ Ні"

    await message.answer(
        f"📊 *Статус бота:*\n\n"
        f"Моніторинг: {status}\n"
        f"Очікує відповіді: {waiting}\n"
        f"Пропущено підряд: {missed_checks}\n"
        f"Інтервал: {check_interval_minutes} хв\n"
        f"Таймаут: {response_timeout_minutes} хв\n"
        f"Екстрених контактів: {len(emergency_contacts)}",
        parse_mode="Markdown"
    )


@dp.message(Command("check_now"))
async def cmd_check_now(message: Message):
    if message.from_user.id != protected_user_id and message.from_user.id not in emergency_contacts:
        await message.answer("❌ Немає доступу.")
        return
    await send_check()
    if message.from_user.id != protected_user_id:
        await message.answer("✅ Перевірку надіслано.")


async def main():
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
