import asyncio
import logging
import os
import json
import base64
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, 
    ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import gspread
from google.oauth2.service_account import Credentials

# --- Настройки ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
SHEET_ID = os.getenv('SHEET_ID')
GOOGLE_CRED_RAW = os.getenv('GOOGLE_CREDENTIALS_JSON')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
ADMIN_ID = os.getenv('ADMIN_ID', '').strip()
WEBHOOK_PATH = f'/webhook/{BOT_TOKEN.split(":")[0]}'

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
thread_executor = ThreadPoolExecutor(max_workers=5)

class SurveyStates(StatesGroup):
    company_name = State()
    address = State()
    phone = State()
    printer_model = State()
    issue_description = State()
    waiting_for_media = State()
    desired_date = State()
    confirm_data = State()

# --- Вспомогательные функции ---

def get_creds():
    try:
        # Пытаемся декодировать, если это Base64 (для Render)
        decoded = base64.b64decode(GOOGLE_CRED_RAW).decode('utf-8')
        return json.loads(decoded)
    except Exception:
        return json.loads(GOOGLE_CRED_RAW)

def format_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', str(phone))
    if digits.startswith('8'): digits = '7' + digits[1:]
    elif not digits.startswith('7'): digits = '7' + digits
    if len(digits) == 11: return f"+{digits}"
    return None

def sync_save_to_sheets(data: dict):
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(get_creds(), scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        row = [
            datetime.now().strftime('%d.%m.%Y %H:%M'),
            f"@{data.get('username')}",
            data.get('company_name'),
            data.get('address'),
            data.get('phone'),
            data.get('printer_model'),
            data.get('issue_description'),
            "\n".join(data.get('media', [])),
            data.get('desired_date')
        ]
        sheet.append_row(row)
        return True
    except Exception as e:
        logger.error(f"Sheets Error: {e}")
        return False

# --- Клавиатуры ---

def get_cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)

def get_skip_kb():
    builder = ReplyKeyboardBuilder()
    builder.button(text="Пропустить фото ➡️")
    builder.button(text="Отмена")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

# --- Хендлеры ---

@dp.message(Command("start"))
@dp.message(F.text == "Отмена")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 **Здравствуйте!**\n\nЯ бот для подачи заявки на ремонт оборудования.\n"
        "1/6 • Введите **Название компании** или ваше имя:",
        parse_mode="Markdown", reply_markup=get_cancel_kb()
    )
    await state.set_state(SurveyStates.company_name)

@dp.message(SurveyStates.company_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(company_name=message.text, username=message.from_user.username or "нет")
    await message.answer("2/6 • Укажите **Адрес** объекта:", parse_mode="Markdown")
    await state.set_state(SurveyStates.address)

@dp.message(SurveyStates.address)
async def process_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await message.answer("3/6 • Ваш контактный **Телефон**:", parse_mode="Markdown")
    await state.set_state(SurveyStates.phone)

@dp.message(SurveyStates.phone)
async def process_phone(message: Message, state: FSMContext):
    phone = format_phone(message.text)
    if not phone:
        await message.answer("❌ Неверный формат. Нужно 11 цифр (например, 89991234567):")
        return
    await state.update_data(phone=phone)
    await message.answer("4/6 • Модель **оборудования** (принтера):", parse_mode="Markdown")
    await state.set_state(SurveyStates.printer_model)

@dp.message(SurveyStates.printer_model)
async def process_model(message: Message, state: FSMContext):
    await state.update_data(printer_model=message.text)
    await message.answer("5/6 • Кратко опишите **суть проблемы**:", parse_mode="Markdown")
    await state.set_state(SurveyStates.issue_description)

@dp.message(SurveyStates.issue_description)
async def process_issue(message: Message, state: FSMContext):
    await state.update_data(issue_description=message.text, media=[])
    await message.answer(
        "📸 **Пришлите фото или видео проблемы**\n\n"
        "Можно несколько. Если фото нет — нажмите кнопку ниже:",
        parse_mode="Markdown", reply_markup=get_skip_kb()
    )
    await state.set_state(SurveyStates.waiting_for_media)

@dp.message(SurveyStates.waiting_for_media, F.text == "Пропустить фото ➡️")
async def skip_media(message: Message, state: FSMContext):
    await message.answer("6/6 • Желаемая **дата и время** визита:", parse_mode="Markdown", reply_markup=get_cancel_kb())
    await state.set_state(SurveyStates.desired_date)

@dp.message(SurveyStates.waiting_for_media, F.photo | F.video)
async def handle_media(message: Message, state: FSMContext):
    data = await state.get_data()
    media = data.get('media', [])
    if message.photo: media.append(f"Photo: {message.photo[-1].file_id}")
    elif message.video: media.append(f"Video: {message.video.file_id}")
    await state.update_data(media=media)
    
    current_timer = data.get('timer')
    if current_timer: current_timer.cancel()
    new_timer = asyncio.create_task(wait_for_next_media(message, state))
    await state.update_data(timer=new_timer)

async def wait_for_next_media(message: Message, state: FSMContext):
    await asyncio.sleep(4) # Ждем 4 секунды после последнего фото
    await message.answer("✅ Файлы приняты.\n6/6 • Желаемая **дата и время** визита:", reply_markup=get_cancel_kb())
    await state.set_state(SurveyStates.desired_date)

@dp.message(SurveyStates.desired_date)
async def process_date(message: Message, state: FSMContext):
    await state.update_data(desired_date=message.text)
    data = await state.get_data()
    
    confirm_text = (
        "📋 **Проверьте данные:**\n\n"
        f"🏢 Компания: {data['company_name']}\n"
        f"📍 Адрес: {data['address']}\n"
        f"📞 Тел: {data['phone']}\n"
        f"🖨 Модель: {data['printer_model']}\n"
        f"📝 Проблема: {data['issue_description']}\n"
        f"🕒 Дата: {message.text}\n"
        f"🖼 Файлов: {len(data.get('media', []))}\n\n"
        "Всё верно?"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, отправить ✅", callback_data="confirm_send")],
        [InlineKeyboardButton(text="Заполнить заново ✏️", callback_data="restart")]
    ])
    await message.answer(confirm_text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "restart")
async def restart_call(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await cmd_start(callback.message, state)
    await callback.answer()

@dp.callback_query(F.data == "confirm_send")
async def confirm_send(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    # Индикация загрузки
    status_msg = await callback.message.answer("⏳ Сохранение заявки...")
    await callback.message.delete()

    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(thread_executor, sync_save_to_sheets, data)
    
    try: await status_msg.delete()
    except: pass

    if success:
        await callback.message.answer("🎉 **Заявка принята!**\nМы скоро свяжемся с вами.", parse_mode="Markdown")
        
        # УВЕДОМЛЕНИЕ АДМИНУ
        if ADMIN_ID:
            try:
                user_url = f"https://t.me/{data['username']}" if data['username'] != "нет" else "нет"
                admin_text = (
                    f"🔔 **НОВАЯ ЗАЯВКА**\n"
                    f"━━━━━━━━━━━━━\n"
                    f"🏢 **Компания:** {data['company_name']}\n"
                    f"📍 **Адрес:** {data['address']}\n"
                    f"📞 **Телефон:** {data['phone']}\n"
                    f"🛠 **Модель:** {data['printer_model']}\n"
                    f"🕒 **Дата:** {data['desired_date']}\n"
                    f"📝 **Суть:** {data['issue_description']}\n\n"
                    f"👤 **Клиент:** @{data['username']} [Написать]({user_url})"
                )[:1000]

                media_list = data.get('media', [])
                if not media_list:
                    await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown", disable_web_page_preview=True)
                else:
                    album = []
                    for i, item in enumerate(media_list[:10]):
                        f_type, f_id = item.split(": ", 1)
                        caption = admin_text if i == 0 else None
                        if "Photo" in f_type:
                            album.append(InputMediaPhoto(media=f_id, caption=caption, parse_mode="Markdown"))
                        else:
                            album.append(InputMediaVideo(media=f_id, caption=caption, parse_mode="Markdown"))
                    await bot.send_media_group(ADMIN_ID, media=album)
            except Exception as e:
                logger.error(f"Admin Notify Error: {e}")
    else:
        await callback.message.answer("❌ Ошибка сохранения. Мы свяжемся с вами напрямую.")
    
    await state.clear()
    await callback.answer()

# --- Webhook & LifeCycle ---

async def on_lifecycle(app: web.Application):
    full_url = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
    await bot.set_webhook(full_url, drop_pending_updates=True)
    yield
    await bot.session.close()
    thread_executor.shutdown(wait=True)

def main():
    app = web.Application()
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.cleanup_ctx.append(on_lifecycle)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    web.run_app(app, host='0.0.0.0', port=int(os.getenv('PORT', 8080)))

if __name__ == "__main__":
    main()
