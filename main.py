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
from google.oauth2.service_account import Credentials

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения (Render)
BOT_TOKEN = os.getenv('BOT_TOKEN')
SHEET_ID = os.getenv('SHEET_ID')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')  # JSON как строка
WEBHOOK_PATH = f'/webhook/{BOT_TOKEN.split(":")[0]}'
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class SurveyStates(StatesGroup):
    company_name = State()
    address = State()
    phone = State()
    printer_model = State()
    issue = State()
    desired_date = State()

async def save_to_sheets(data: dict):
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(eval(GOOGLE_CREDENTIALS_JSON), scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1

        row = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            data.get('username', 'Нет username'),
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
        logger.error(f'Ошибка сохранения: {e}')

@dp.message(Command('start'))
async def start_survey(message: Message, state: FSMContext):
    username = message.from_user.username if message.from_user.username else 'Нет username'
    await state.update_data(username=username)

    await message.answer(
        f'Привет! Ваш Telegram: @{username if username != "Нет username" else "не указан"}\n\n'
        'Давайте оформим заявку на ремонт принтера.\n'
        '1. Укажите название компании или имя:'
    )
    await state.set_state(SurveyStates.company_name)

@dp.message(SurveyStates.company_name)
async def process_company_name(message: Message, state: FSMContext):
    await state.update_data(company_name=message.text)
    await message.answer('2. Укажите адрес:')
    await state.set_state(SurveyStates.address)

@dp.message(SurveyStates.address)
async def process_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await message.answer('3. Укажите телефон (по желанию, можно пропустить):')
    await state.set_state(SurveyStates.phone)

@dp.message(SurveyStates.phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip() if message.text and message.text.strip() else 'Не указан'
    await state.update_data(phone=phone)
    await message.answer('4. Укажите модель принтера:')
    await state.set_state(SurveyStates.printer_model)

@dp.message(SurveyStates.printer_model)
async def process_printer_model(message: Message, state: FSMContext):
    await state.update_data(printer_model=message.text)
    await message.answer('5. Что беспокоит? (поломка, заправка картриджа, доставка или другое).\n'
                         'Если поломка — пришлите фото или видео (можно несколько).')
    await state.set_state(SurveyStates.issue)

@dp.message(SurveyStates.issue)
async def process_issue(message: Message, state: FSMContext):
    issue_text = message.text or ''
    media_url = ''

    if message.photo:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        media_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}'
    elif message.video:
        file_info = await bot.get_file(message.video.file_id)
        media_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}'

    if 'поломка' in issue_text.lower() and not media_url:
        await message.answer('Для поломки желательно фото/видео — пришлите или просто продолжите текстом.')
        return

    await state.update_data(issue=issue_text, media_url=media_url)
    await message.answer('6. Укажите желаемую дату (формат YYYY-MM-DD):')
    await state.set_state(SurveyStates.desired_date)

@dp.message(SurveyStates.desired_date)
async def process_desired_date(message: Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
        await state.update_data(desired_date=date_str)
        data = await state.get_data()
        await save_to_sheets(data)
        await message.answer('Заявка успешно отправлена! Спасибо, с вами свяжутся в ближайшее время.')
        await state.clear()
    except ValueError:
        await message.answer('Неверный формат. Нужно YYYY-MM-DD (например, 2026-01-25). Попробуйте снова:')

@dp.message(Command('cancel'))
async def cancel_survey(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Опрос отменён. Чтобы начать заново — /start')

async def on_startup(_):
    await bot.set_webhook(f'{WEBHOOK_URL}{WEBHOOK_PATH}', drop_pending_updates=True)
    logger.info('Webhook установлен')

async def on_shutdown(_):
    logger.info('on_shutdown вызван')

async def graceful_shutdown_ctx(app: web.Application):
    # Startup (если нужно — пусто)
    yield
    # Cleanup
    logger.info("Graceful shutdown: закрываем сессию бота")
    try:
        await bot.session.close()
    except Exception as e:
        logger.warning(f'Ошибка закрытия bot.session: {e}')
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f'Ошибка удаления webhook: {e}')
    logger.info('Shutdown завершён')
    await asyncio.sleep(1)

def main():
    app = web.Application()
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # Регистрируем graceful shutdown
    app.cleanup_ctx.append(graceful_shutdown_ctx)

    # Health-check
    async def health(request):
        return web.Response(text='OK', status=200)
    app.router.add_get('/health', health)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    web.run_app(
        app,
        host='0.0.0.0',
        port=int(os.getenv('PORT', 8080)),
        shutdown_timeout=30
    )

if __name__ == '__main__':
    main()import asyncio
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
from google.oauth2.service_account import Credentials

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения (Render)
BOT_TOKEN = os.getenv('BOT_TOKEN')
SHEET_ID = os.getenv('SHEET_ID')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')  # JSON как строка
WEBHOOK_PATH = f'/webhook/{BOT_TOKEN.split(":")[0]}'
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class SurveyStates(StatesGroup):
    company_name = State()
    address = State()
    phone = State()
    printer_model = State()
    issue = State()
    desired_date = State()

async def save_to_sheets(data: dict):
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(eval(GOOGLE_CREDENTIALS_JSON), scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1

        row = [
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            data.get('username', 'Нет username'),
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
        logger.error(f'Ошибка сохранения: {e}')

@dp.message(Command('start'))
async def start_survey(message: Message, state: FSMContext):
    username = message.from_user.username if message.from_user.username else 'Нет username'
    await state.update_data(username=username)

    await message.answer(
        f'Привет! Ваш Telegram: @{username if username != "Нет username" else "не указан"}\n\n'
        'Давайте оформим заявку на ремонт принтера.\n'
        '1. Укажите название компании или имя:'
    )
    await state.set_state(SurveyStates.company_name)

@dp.message(SurveyStates.company_name)
async def process_company_name(message: Message, state: FSMContext):
    await state.update_data(company_name=message.text)
    await message.answer('2. Укажите адрес:')
    await state.set_state(SurveyStates.address)

@dp.message(SurveyStates.address)
async def process_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await message.answer('3. Укажите телефон (по желанию, можно пропустить):')
    await state.set_state(SurveyStates.phone)

@dp.message(SurveyStates.phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip() if message.text and message.text.strip() else 'Не указан'
    await state.update_data(phone=phone)
    await message.answer('4. Укажите модель принтера:')
    await state.set_state(SurveyStates.printer_model)

@dp.message(SurveyStates.printer_model)
async def process_printer_model(message: Message, state: FSMContext):
    await state.update_data(printer_model=message.text)
    await message.answer('5. Что беспокоит? (поломка, заправка картриджа, доставка или другое).\n'
                         'Если поломка — пришлите фото или видео (можно несколько).')
    await state.set_state(SurveyStates.issue)

@dp.message(SurveyStates.issue)
async def process_issue(message: Message, state: FSMContext):
    issue_text = message.text or ''
    media_url = ''

    if message.photo:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        media_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}'
    elif message.video:
        file_info = await bot.get_file(message.video.file_id)
        media_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}'

    if 'поломка' in issue_text.lower() and not media_url:
        await message.answer('Для поломки желательно фото/видео — пришлите или просто продолжите текстом.')
        return

    await state.update_data(issue=issue_text, media_url=media_url)
    await message.answer('6. Укажите желаемую дату (формат YYYY-MM-DD):')
    await state.set_state(SurveyStates.desired_date)

@dp.message(SurveyStates.desired_date)
async def process_desired_date(message: Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
        await state.update_data(desired_date=date_str)
        data = await state.get_data()
        await save_to_sheets(data)
        await message.answer('Заявка успешно отправлена! Спасибо, с вами свяжутся в ближайшее время.')
        await state.clear()
    except ValueError:
        await message.answer('Неверный формат. Нужно YYYY-MM-DD (например, 2026-01-25). Попробуйте снова:')

@dp.message(Command('cancel'))
async def cancel_survey(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Опрос отменён. Чтобы начать заново — /start')

async def on_startup(_):
    await bot.set_webhook(f'{WEBHOOK_URL}{WEBHOOK_PATH}', drop_pending_updates=True)
    logger.info('Webhook установлен')

async def on_shutdown(_):
    logger.info('on_shutdown вызван')

async def graceful_shutdown_ctx(app: web.Application):
    # Startup (если нужно — пусто)
    yield
    # Cleanup
    logger.info("Graceful shutdown: закрываем сессию бота")
    try:
        await bot.session.close()
    except Exception as e:
        logger.warning(f'Ошибка закрытия bot.session: {e}')
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f'Ошибка удаления webhook: {e}')
    logger.info('Shutdown завершён')
    await asyncio.sleep(1)

def main():
    app = web.Application()
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # Регистрируем graceful shutdown
    app.cleanup_ctx.append(graceful_shutdown_ctx)

    # Health-check
    async def health(request):
        return web.Response(text='OK', status=200)
    app.router.add_get('/health', health)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    web.run_app(
        app,
        host='0.0.0.0',
        port=int(os.getenv('PORT', 8080)),
        shutdown_timeout=30
    )

if __name__ == '__main__':
    main()
