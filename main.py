import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import gspread
from google.auth import default
from google.oauth2.service_account import Credentials

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных из env (на Render — Environment Variables)
BOT_TOKEN = os.getenv('BOT_TOKEN')
SHEET_ID = os.getenv('SHEET_ID')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')  # Полный JSON как строка
WEBHOOK_PATH = f'/webhook/{BOT_TOKEN.split(":")[0]}'  # Простая защита пути
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # https://your-service.onrender.com (без /webhook)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# FSM состояния
class SurveyStates(StatesGroup):
    company_name = State()    # 1. Название компании/Имя
    address = State()         # 2. Адрес
    phone = State()           # 3. Телефон (по желанию)
    printer_model = State()   # 4. Модель принтера
    issue = State()           # 5. Что беспокоит (текст + фото/видео если поломка)
    desired_date = State()    # 6. Желаемая дата

# Функция сохранения в Google Sheets
async def save_to_sheets(data: dict):
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(eval(GOOGLE_CREDENTIALS_JSON), scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        row = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),  # ID по времени
            data.get('company_name', 'Не указано'),
            data.get('address', 'Не указан'),
            data.get('phone', 'Не указан'),
            data.get('printer_model', 'Не указана'),
            data.get('issue', 'Не указана'),
            data.get('media_url', 'Нет фото/видео'),
            data.get('desired_date', 'Не указана')
        ]
        sheet.append_row(row)
        logger.info('Заявка сохранена в Google Sheets')
    except Exception as e:
        logger.error(f'Ошибка сохранения в Sheets: {e}')

# Старт опроса
@dp.message(Command('start'))
async def start_survey(message: Message, state: FSMContext):
    await message.answer('Привет! Давайте оформим заявку на ремонт принтера.\n'
                         '1. Укажите название компании или имя:')
    await state.set_state(SurveyStates.company_name)

# 1. Название компании/Имя
@dp.message(SurveyStates.company_name)
async def process_company_name(message: Message, state: FSMContext):
    await state.update_data(company_name=message.text)
    await message.answer('2. Укажите адрес:')
    await state.set_state(SurveyStates.address)

# 2. Адрес
@dp.message(SurveyStates.address)
async def process_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await message.answer('3. Укажите телефон (по желанию, можно пропустить):')
    await state.set_state(SurveyStates.phone)

# 3. Телефон (по желанию)
@dp.message(SurveyStates.phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip() if message.text.strip() else 'Не указан'
    await state.update_data(phone=phone)
    await message.answer('4. Укажите модель принтера:')
    await state.set_state(SurveyStates.printer_model)

# 4. Модель принтера
@dp.message(SurveyStates.printer_model)
async def process_printer_model(message: Message, state: FSMContext):
    await state.update_data(printer_model=message.text)
    await message.answer('5. Что беспокоит? (поломка, заправка картриджа, доставка или другое).\n'
                         'Если поломка — пришлите фото или видео.')
    await state.set_state(SurveyStates.issue)

# 5. Что беспокоит + фото/видео
@dp.message(SurveyStates.issue)
async def process_issue(message: Message, state: FSMContext):
    issue_text = message.text if message.text else ''
    media_url = ''
    if message.photo:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        media_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}'
    elif message.video:
        video = message.video
        file_info = await bot.get_file(video.file_id)
        media_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}'
    else:
        if 'поломка' in issue_text.lower():
            await message.answer('Для поломки желательно фото/видео. Пришлите или продолжите текстом.')
            return  # Ждём фото/видео или повтор текста

    await state.update_data(issue=issue_text, media_url=media_url)
    await message.answer('6. Укажите желаемую дату (формат YYYY-MM-DD):')
    await state.set_state(SurveyStates.desired_date)

# 6. Желаемая дата + валидация
@dp.message(SurveyStates.desired_date)
async def process_desired_date(message: Message, state: FSMContext):
    try:
        datetime.strptime(message.text, '%Y-%m-%d')
        await state.update_data(desired_date=message.text)
        data = await state.get_data()
        await save_to_sheets(data)
        await message.answer('Заявка принята! Спасибо, с вами свяжутся.')
        await state.clear()
    except ValueError:
        await message.answer('Неверный формат даты (должен быть YYYY-MM-DD). Попробуйте снова:')

# Отмена опроса
@dp.message(Command('cancel'))
async def cancel_survey(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Опрос отменён. Начните заново с /start.')

# Startup: Установка webhook
async def on_startup(dispatcher: Dispatcher):
    await bot.set_webhook(f'{WEBHOOK_URL}{WEBHOOK_PATH}')
    logger.info('Webhook установлен')

# Shutdown: Удаление webhook
async def on_shutdown(dispatcher: Dispatcher):
    await bot.delete_webhook()
    logger.info('Webhook удалён')

# Main для webhook на Render
def main():
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host='0.0.0.0', port=int(os.getenv('PORT', 8080)))

if __name__ == '__main__':
    main()