import asyncio
import logging
import os
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardRemove
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import gspread
from google.oauth2.service_account import Credentials

# --- Настройки ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
SHEET_ID = os.getenv('SHEET_ID')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
WEBHOOK_PATH = f'/webhook/{BOT_TOKEN.split(":")[0]}'

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
thread_executor = ThreadPoolExecutor(max_workers=5) # Потоки для Google Sheets

# --- Состояния ---
class SurveyStates(StatesGroup):
    company_name = State()
    address = State()
    phone = State()
    printer_model = State()
    issue = State()
    desired_date = State()

# --- Работа с таблицами (в отдельном потоке) ---
def sync_save_to_sheets(data: dict):
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1

        row = [
            datetime.now().strftime('%d.%m.%Y %H:%M'),
            f"@{data.get('username', '')}",
            data.get('company_name'),
            data.get('address'),
            data.get('phone'),
            data.get('printer_model'),
            data.get('issue'),
            data.get('desired_date'),
            data.get('file_id', 'Нет фото') # Сохраняем ID файла для поиска в TG
        ]
        sheet.append_row(row)
        return True
    except Exception as e:
        logger.error(f"Sheets Error: {e}")
        return False

# --- Хендлеры опроса ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(username=message.from_user.username or "no_username")
    await message.answer("1. Введите название компании или ваше имя:")
    await state.set_state(SurveyStates.company_name)

@dp.message(SurveyStates.company_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(company_name=message.text)
    await message.answer("2. Укажите адрес:")
    await state.set_state(SurveyStates.address)

@dp.message(SurveyStates.address)
async def process_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await message.answer("3. Ваш номер телефона:")
    await state.set_state(SurveyStates.phone)

@dp.message(SurveyStates.phone)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("4. Модель принтера:")
    await state.set_state(SurveyStates.printer_model)

@dp.message(SurveyStates.printer_model)
async def process_model(message: Message, state: FSMContext):
    await state.update_data(printer_model=message.text)
    await message.answer("5. Опишите проблему (можно прикрепить ОДНО фото):")
    await state.set_state(SurveyStates.issue)

@dp.message(SurveyStates.issue)
async def process_issue(message: Message, state: FSMContext):
    # Сохраняем текст или описание под фото
    issue_text = message.text or message.caption or "Без описания"
    file_id = message.photo[-1].file_id if message.photo else "Нет фото"
    
    await state.update_data(issue=issue_text, file_id=file_id)
    await message.answer("6. Желаемая дата и время визита:")
    await state.set_state(SurveyStates.desired_date)

@dp.message(SurveyStates.desired_date)
async def process_date(message: Message, state: FSMContext):
    await state.update_data(desired_date=message.text)
    data = await state.get_data()
    
    msg = await message.answer("⏳ Сохраняю заявку...")
    
    # Запуск записи в таблицу без блокировки бота
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(thread_executor, sync_save_to_sheets, data)
    
    if success:
        await msg.edit_text("✅ Заявка принята! Мы скоро свяжемся с вами.")
    else:
        await msg.edit_text("❌ Ошибка при сохранении. Но мы всё равно получили ваше сообщение!")
    
    await state.clear()

# --- Жизненный цикл Webhook ---

async def on_lifecycle(app: web.Application):
    # STARTUP
    webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    logger.info(f"Webhook set to: {webhook_url}")
    
    yield # Работа приложения
    
    # SHUTDOWN
    logger.info("Closing bot session...")
    await bot.delete_webhook()
    await bot.session.close()
    thread_executor.shutdown(wait=True)

# --- Приложение ---

def main():
    app = web.Application()
    
    # Регистрация вебхука в aiogram
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    # Настройка очистки
    app.cleanup_ctx.append(on_lifecycle)
    
    # Healthcheck для Render
    app.router.add_get("/health", lambda r: web.Response(text="OK"))

    web.run_app(app, host='0.0.0.0', port=int(os.getenv('PORT', 8080)))

if __name__ == "__main__":
    main()
