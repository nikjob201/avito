import aiogram
import asyncio
import json
import os
import logging
import time 
import random 
import html
from typing import Dict, List, Any, Optional, Set, Tuple 
from curl_cffi import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from transliterate import translit
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "60")) 

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан в .env")

PROXY_STRING = os.getenv("PROXY_STRING")
PROXY_CHANGE_URL = os.getenv("PROXY_CHANGE_URL")

DATA_DIR = "data"
PRESETS_FILE = os.path.join(DATA_DIR, "avitomoment.json")
LOCATIONS_FILE = os.path.join(DATA_DIR, "avito_locations.json")
SEEN_ADS_FILE = os.path.join(DATA_DIR, "seen_ads.json") 
os.makedirs(DATA_DIR, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router() 

def load_json_file(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка чтения JSON {path}: {e}")
        return None

def save_json_file(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

locations = load_json_file(LOCATIONS_FILE) or []

def normalize(s: str) -> str:
    return ''.join(ch for ch in s if ch.isalnum()).lower()

def city_to_slug(city_name: str) -> str:
    if not city_name:
        return "moskva"
    try:
        slug = translit(city_name.lower(), 'ru', reversed=True)
    except Exception:
        slug = city_name.lower()
    slug = slug.replace(" ", "-").replace("ё", "e").replace("й", "j").replace("'", "")
    return slug

def random_user_agent() -> str:
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]
    return random.choice(user_agents)

def change_ip() -> bool:
    if not PROXY_CHANGE_URL:
        logger.warning("PROXY_CHANGE_URL не настроен. Смена IP невозможна.")
        return False
    
    logger.info("⚡ Получен 429/403. Запускаю смену IP через API...")
    try:
        import requests as std_requests 
        response = std_requests.get(url=PROXY_CHANGE_URL, timeout=15)
        
        if response.status_code == 200:
            logger.success("IP успешно изменен. Пауза 10 сек для активации.")
            time.sleep(10) 
            return True
        else:
            logger.error(f"Ошибка API смены IP: {response.status_code}.")
            return False
    except Exception as e:
        logger.error(f"Не удалось подключиться к API смены IP: {e}")
        return False

def find_json_on_page(html_code: str) -> dict:
    soup = BeautifulSoup(html_code, "html.parser")
    try:
        for script in soup.select('script'):
            if script.get('type') == 'mime/invalid':
                script_content = script.text
                script_content = html.unescape(script_content) 
                parsed_data = json.loads(script_content)
                if 'state' in parsed_data:
                    return parsed_data['state']
                if 'data' in parsed_data:
                    return parsed_data['data']
                return parsed_data
    except Exception as e:
        logger.error(f"Ошибка при извлечении JSON: {e}")
    return {}

def search_avito_single_attempt(query: str, city_slug: str = "moskva", limit: int = 10, retries: int = 5) -> Tuple[str, List[Dict[str, Any]]]:
    current_slug = city_to_slug(city_slug)
    
    proxy_data = None
    if PROXY_STRING:
        proxy_data = {"https": f"http://{PROXY_STRING}", "http": f"http://{PROXY_STRING}"}
    
    if '?' in query:
        url = f"https://www.avito.ru/{current_slug}/{query}&s=104"
    else:
        url = f"https://www.avito.ru/{current_slug}?q={requests.utils.quote(query)}&s=104"
        
    headers = {"User-Agent": random_user_agent()}
    session = requests.Session()

    for attempt in range(1, retries + 1):
        try:
            response = session.get(
                url=url,
                headers=headers,
                proxies=proxy_data,
                impersonate="chrome",
                http_version=3,
                timeout=20,
                verify=False,
            )

            if response.status_code in [429, 403]:
                logger.warning(f"Попытка {attempt}: Статус {response.status_code}. Блокировка!")
                if not change_ip():
                     logger.error("Смена IP не удалась. Пауза 5 минут.")
                     time.sleep(300)
                continue 

            response.raise_for_status()
            
            json_data = find_json_on_page(response.text)
            if not json_data:
                logger.warning("HTML получен, но JSON-блок не найден.")
                return "OK", []

            items_list = json_data.get('catalog', {}).get('items', [])
            results = []
            
            for item in items_list[:limit]:
                if not item.get('id'): continue
                
                photo_url = None
                images = item.get('images', [])
                if images and isinstance(images, list) and images[0].get('url'):
                     photo_url = images[0].get('url')
                     if photo_url.startswith('//'):
                         photo_url = 'https:' + photo_url

                results.append({
                    "title": item.get('title', '—'),
                    "price": item.get('priceDetailed', {}).get('string', '—'),
                    "url": "https://www.avito.ru" + item.get('urlPath', ''),
                    "photo": photo_url
                })
            
            return "OK", results

        except requests.RequestsError as e:
            logger.warning(f"⚠️ Ошибка сети (Попытка {attempt}): {e}.")
            if attempt < retries:
                time.sleep(10 * attempt)
            else:
                logger.error(f"❌ Не удалось получить данные ({city_slug}, {query})")
                return "ERROR", []
        except Exception as e:
            logger.exception("Неожиданная ошибка при парсинге: %s", e)
            return "ERROR", []

    return "ERROR", []

def load_presets() -> Dict[str, List[Dict[str, Any]]]:
    return load_json_file(PRESETS_FILE) or {}

def save_presets(data: Dict[str, List[Dict[str, Any]]]):
    save_json_file(PRESETS_FILE, data)

class CreatePreset(StatesGroup):
    name = State()
    region = State()
    city = State()
    query = State()

class EditPreset(StatesGroup):
    waiting_for_value = State()

def start_menu():
    kb = [
        [KeyboardButton(text="➡️ Начать")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def main_menu():
    kb = [
        [KeyboardButton(text="🎯 Мои пресеты"), KeyboardButton(text="➕ Создать пресет")],
        [KeyboardButton(text="🚀 Запустить мониторинг"), KeyboardButton(text="⏹ Остановить мониторинг")],
        [KeyboardButton(text="ℹ️ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def cancel_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="↩️ Отмена")]], resize_keyboard=True)

def build_presets_inline(user_presets: List[Dict[str, Any]]):
    kb = []
    for p in user_presets:
        name = p.get("name", "Без названия")
        status = "🟢" if p.get("active") else "⚫"
        kb.append([
            InlineKeyboardButton(text=f"{status} {name}", callback_data=f"select|{name}"),
            InlineKeyboardButton(text="✏️", callback_data=f"edit|{name}"),
            InlineKeyboardButton(text="🗑", callback_data=f"delete|{name}")
        ])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

user_tasks: Dict[int, asyncio.Task] = {} 

def load_seen_ads() -> Dict[str, List[str]]:
    return load_json_file(SEEN_ADS_FILE) or {}

def save_seen_ads(data: Dict[str, List[str]]):
    save_json_file(SEEN_ADS_FILE, data)
    
seen_ads_db = load_seen_ads()

async def process_single_preset(user_id: int, preset: dict, key: str):
    global seen_ads_db 
    
    query = preset.get("query")
    city = preset.get("city", "Москва")
    
    try:
        status, results = await asyncio.to_thread(search_avito_single_attempt, query, city, 10)
        
        if status == "ERROR" or not results:
            return

        prev_urls: Set[str] = set(seen_ads_db.get(key, []))
        
        new_ads = [ad for ad in results if ad.get("url") and ad.get("url") not in prev_urls]

        if not new_ads:
            return

        logger.info(f"User {user_id}, {key}: Найдено {len(new_ads)} новых объявлений.")

        for ad in new_ads[:3]: 
            caption = (
                f"🔹 <b>{ad.get('title', '—')}</b>\n"
                f"Цена: {ad.get('price', '—')}\n"
                f"{ad.get('url')}"
            )
            
            try:
                if ad.get("photo"):
                    await bot.send_photo(user_id, ad["photo"], caption=caption, parse_mode="HTML")
                else:
                    await bot.send_message(user_id, caption, parse_mode="HTML")
                
                await asyncio.sleep(random.uniform(1, 3)) 
                
            except Exception as e:
                logger.warning(f"Не удалось отправить {ad.get('url')} пользователю {user_id}: {e}")

        seen_ads_db[key] = [ad.get("url") for ad in results if ad.get("url")]

    except asyncio.CancelledError:
        raise 
    except Exception as e:
        logger.error(f"Критическая ошибка в process_single_preset: {e}")

async def user_monitor_loop(user_id: int):
    global seen_ads_db
    logger.info(f"🔁 Monitor loop started for user {user_id}. Interval: {MONITOR_INTERVAL_SECONDS}s")

    try:
        while True: 
            data = load_presets()
            user_presets = data.get(str(user_id), [])
            tasks = []
            
            active_presets = [p for p in user_presets if p.get("active")]

            if not active_presets:
                logger.warning(f"User {user_id}: Нет активных пресетов. Остановка мониторинга.")
                break 
            
            logger.info(f"🟢 User {user_id}: Начинаю проверку {len(active_presets)} активных пресетов.") 

            for preset in active_presets:
                key = f"{user_id}__{preset.get('name')}" 
                tasks.append(
                    process_single_preset(user_id, preset, key)
                )
            
            if tasks:
                logger.debug(f"User {user_id}: Запуск {len(tasks)} задач на проверку...")
                await asyncio.sleep(random.uniform(0, 3)) 
                await asyncio.gather(*tasks, return_exceptions=True)

            save_seen_ads(seen_ads_db)

            await asyncio.sleep(MONITOR_INTERVAL_SECONDS)

    except asyncio.CancelledError:
        logger.info(f"🛑 Monitor loop cancelled for user {user_id}.")
    except Exception as e:
        logger.exception(f"Ошибка в цикле мониторинга пользователя {user_id}: {e}")
    finally:
        if user_id in user_tasks:
            del user_tasks[user_id]
        logger.info(f"🔁 Monitor loop stopped for user {user_id}.")

@router.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("✅ Бот запущен! Сейчас проверим настройки...")
    logger.info("Got /start from %s", message.from_user.id)
    await message.answer("👋 Привет! Я бот для мониторинга Avito.\nНажмите 'Начать' для работы:", reply_markup=start_menu())

@router.message(F.text == "➡️ Начать")
async def start_handler(message: types.Message):
    await message.answer("Выберите действие:", reply_markup=main_menu())

@router.message(F.text == "↩️ Отмена")
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu())


@router.message(F.text == "🎯 Мои пресеты")
async def show_presets_handler(message: types.Message):
    data = load_presets()
    user_id = str(message.from_user.id)
    user_presets = data.get(user_id, [])
    if not user_presets:
        await message.answer("📂 У вас нет пресетов.", reply_markup=main_menu())
        return
    kb = build_presets_inline(user_presets)
    await message.answer("📋 Ваши пресеты:", reply_markup=kb)

@router.callback_query(F.data == "back_main")
async def back_main_callback(callback: types.CallbackQuery):
    await callback.answer() 
    try:
        await callback.message.edit_text("Выберите действие:", reply_markup=None)
    except aiogram.exceptions.TelegramBadRequest:
        pass
    await callback.message.answer("Выберите действие:", reply_markup=main_menu())

@router.callback_query(F.data.startswith("select|"))
async def select_preset(callback: types.CallbackQuery):
    await callback.answer() 
    data = load_presets()
    user_id = str(callback.from_user.id)
    user_presets = data.get(user_id, [])
    name = callback.data.split("|", 1)[1]
    found = False
    for p in user_presets:
        if p.get("name") == name:
            p["active"] = not p.get("active", False) 
            found = True
            status = "активирован" if p["active"] else "деактивирован"
    if not found:
        await callback.answer("❌ Пресет не найден.", show_alert=True)
        return
    save_presets(data)
    kb = build_presets_inline(user_presets)
    try:
        await callback.message.edit_text("📋 Ваши пресеты:", reply_markup=kb)
    except aiogram.exceptions.TelegramBadRequest:
        pass

@router.callback_query(F.data.startswith("delete|"))
async def delete_preset(callback: types.CallbackQuery):
    await callback.answer("🗑 Пресет удалён.") 
    data = load_presets()
    user_id = str(callback.from_user.id)
    user_presets = data.get(user_id, [])
    name = callback.data.split("|", 1)[1]
    data[user_id] = [p for p in user_presets if p.get("name") != name]
    save_presets(data)
    kb = build_presets_inline(data.get(user_id, []))
    try:
        await callback.message.edit_text("📋 Ваши пресеты:", reply_markup=kb)
    except aiogram.exceptions.TelegramBadRequest:
        pass 
    
@router.callback_query(F.data.startswith("edit|"))
async def edit_preset(callback: types.CallbackQuery):
    await callback.answer() 
    name = callback.data.split("|", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить название", callback_data=f"edit_field|{name}|name")],
        [InlineKeyboardButton(text="🌍 Изменить регион", callback_data=f"edit_field|{name}|region")],
        [InlineKeyboardButton(text="🏙 Изменить город", callback_data=f"edit_field|{name}|city")],
        [InlineKeyboardButton(text="🔎 Изменить запрос", callback_data=f"edit_field|{name}|query")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")] 
    ])
    await callback.message.edit_text(f"Что хотите изменить в пресете «{name}»?", reply_markup=kb)

@router.callback_query(F.data.startswith("edit_field|"))
async def edit_field_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer() 
    try:
        parts = callback.data.split("|", 2)
        _, preset_name, field = parts
        user_id = str(callback.from_user.id)
        data = load_presets()
        user_presets = data.get(user_id, [])
        preset = next((p for p in user_presets if p.get("name") == preset_name), None)
        if not preset:
            await callback.answer("Пресет не найден.", show_alert=True)
            return
        await state.update_data(edit_preset_name=preset_name, edit_field=field)
        await state.set_state(EditPreset.waiting_for_value)
        current_value = preset.get(field, "—")
        await callback.message.answer(
            f"✏️ Введите новое значение для поля «{field}» (сейчас: {current_value})",
            reply_markup=cancel_menu()
        )
    except Exception as e:
        logger.exception("Ошибка в edit_field_callback: %s", e)
        await callback.answer("❌ Ошибка при редактировании.", show_alert=True)

@router.message(EditPreset.waiting_for_value)
async def process_edit_field(message: types.Message, state: FSMContext):
    if message.text == "↩️ Отмена":
        await cancel_handler(message, state)
        return
    user_id = str(message.from_user.id)
    sdata = await state.get_data()
    preset_name = sdata.get("edit_preset_name")
    field = sdata.get("edit_field")
    if not preset_name or not field:
        await message.answer("❌ Ошибка состояния. Попробуйте снова.", reply_markup=main_menu())
        await state.clear()
        return
    data = load_presets()
    user_presets = data.get(user_id, [])
    preset = next((p for p in user_presets if p.get("name") == preset_name), None)
    if not preset:
        await message.answer("❌ Пресет не найден.", reply_markup=main_menu())
        await state.clear()
        return
    preset[field] = message.text.strip()
    if field == "region":
        preset["city"] = "" 
    save_presets(data)
    await message.answer(f"✅ Поле «{field}» обновлено.", reply_markup=main_menu())
    await state.clear()

@router.message(F.text == "➕ Создать пресет")
async def create_preset(message: types.Message, state: FSMContext):
    await message.answer("Введите название пресета:", reply_markup=cancel_menu())
    await state.set_state(CreatePreset.name)

@router.message(CreatePreset.name)
async def set_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите регион:", reply_markup=cancel_menu())
    await state.set_state(CreatePreset.region)

@router.message(CreatePreset.region)
async def set_region(message: types.Message, state: FSMContext):
    await state.update_data(region=message.text.strip())
    await message.answer("Введите город:", reply_markup=cancel_menu())
    await state.set_state(CreatePreset.city)

@router.message(CreatePreset.city)
async def set_city(message: types.Message, state: FSMContext):
    await state.update_data(city=message.text.strip())
    await message.answer("Введите поисковый запрос (например: avtomobili?q=BMW):", reply_markup=cancel_menu())
    await state.set_state(CreatePreset.query)

@router.message(CreatePreset.query)
async def finish_preset(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    data = load_presets()
    s = await state.get_data()
    s["query"] = message.text.strip()
    s["active"] = False
    data.setdefault(user_id, []).append(s)
    save_presets(data)
    await message.answer("✅ Пресет сохранён.", reply_markup=main_menu())
    await state.clear()

@router.message(F.text == "🚀 Запустить мониторинг")
async def start_monitor(message: types.Message):
    user_id = message.from_user.id
    data = load_presets()
    user_presets = data.get(str(user_id), [])
    active_presets = [p for p in user_presets if p.get("active")]
    if not active_presets:
        await message.answer("⚠️ Ошибка: выберите хотя бы один активный пресет перед запуском мониторинга.")
        return
    if user_id in user_tasks:
        await message.answer("ℹ️ Ваш мониторинг уже запущен.")
        return
    task = asyncio.create_task(user_monitor_loop(user_id))
    user_tasks[user_id] = task
    await message.answer(f"🔁 Ваш персональный монитор запущен ({len(active_presets)} активных пресетов).\n(Проверка будет проходить каждые {MONITOR_INTERVAL_SECONDS} секунд)")

@router.message(F.text == "⏹ Остановить мониторинг")
async def stop_monitor(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_tasks:
        await message.answer("ℹ️ Ваш мониторинг не запущен.")
        return
    task = user_tasks[user_id]
    task.cancel()
    await message.answer("✅ Отправлен сигнал остановки. Мониторинг скоро завершится.")

@router.message(F.text == "ℹ️ Помощь")
async def help_cmd(message: types.Message):
    await message.answer(
        "📘 Инструкция:\n"
        "1. Создайте пресет (название, регион, город, запрос)\n"
        "2. Активируйте нужный пресет\n"
        "3. Запустите мониторинг — бот будет присылать новые объявления."
    )

async def main():
    dp.include_router(router)
    logger.info("✅ Бот и роутер подключены. Старт polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлен пользователем (KeyboardInterrupt).")
    except Exception as e:
        logger.exception("Ошибка при запуске: %s", e)