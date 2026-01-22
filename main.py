import asyncio
import logging
import os
import signal
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
SHEET_ID = os.getenv('SHEET_ID')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')
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
        logger.info('Заявка сохранена')
    except Exception as e:
        logger.error(f'Ошибка Sheets: {e}')

@dp.message(Command('start'))
async def start_survey(message: Message, state: FSMContext):
    username = message.from_user.username or 'Нет username'
    await state.update_data(username=username)
    await message.answer(
        f'Привет! Telegram: @{username if username != "Нет username" else "не указан"}\n\n'
        '1. Название компании / имя:'
    )
    await state.set_state(SurveyStates.company_name)

# Остальные handlers (сокращаю для краткости, вставь свои как были)
@dp.message(SurveyStates.company_name)
async def process_company_name(message: Message, state: FSMContext):
    await state.update_data(company_name=message.text)
    await message.answer('2. Адрес:')
    await state.set_state(SurveyStates.address)

# ... (phone, printer_model, issue, desired_date — оставь как у тебя)

@dp.message(Command('cancel'))
async def cancel_survey(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Отменено. /start заново.')

async def on_startup(_):
    await bot.set_webhook(f'{WEBHOOK_URL}{WEBHOOK_PATH}', drop_pending_updates=True)
    logger.info('Webhook установлен')

async def on_shutdown(_):
    logger.info('on_shutdown')

async def graceful_shutdown_ctx(app: web.Application):
    yield
    logger.info("Graceful shutdown старт")
    # Ждём завершения pending задач
    pending = asyncio.all_tasks()
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    
    try:
        await bot.session.close()
        logger.info("bot.session закрыта")
    except Exception as e:
        logger.warning(f"Ошибка bot.session: {e}")
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook удалён")
    except Exception as e:
        logger.warning(f"Ошибка webhook: {e}")
    
    await asyncio.sleep(3)  # даём aiohttp время на закрытие коннекторов
    logger.info("Graceful shutdown завершён")

def main():
    app = web.Application()
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.cleanup_ctx.append(graceful_shutdown_ctx)

    async def health(request):
        return web.Response(text='OK', status=200)
    app.router.add_get('/health', health)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Перехват SIGTERM от Render
    def handle_sigterm(signum, frame):
        logger.info("SIGTERM получен → закрываем сессию")
        asyncio.create_task(bot.session.close())
        asyncio.create_task(bot.delete_webhook(drop_pending_updates=True))

    signal.signal(signal.SIGTERM, handle_sigterm)

    web.run_app(
        app,
        host='0.0.0.0',
        port=int(os.getenv('PORT', 8080)),
        shutdown_timeout=90  # максимум, что Render позволяет
    )

if __name__ == '__main__':
    main()
