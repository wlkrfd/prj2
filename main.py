import os
import re
import json
import csv
import random
import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile
)

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Ошибка: Токен бота не найден в файле .env!")

# Настройка путей
BASE_DIR = Path(__file__).parent
USERS_JSON_PATH = BASE_DIR / "users.json"
DATABASE_CSV_PATH = BASE_DIR / "database.csv"
PHOTOS_DIR = BASE_DIR / "photos"

PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================================
# 1. РАБОТА С ФАЙЛАМИ (JSON И CSV ДЛЯ EXCEL)
# =====================================================================

def load_users() -> dict:
    """Загружает словарь пользователей из JSON-файла с безопасными значениями по умолчанию."""
    if not USERS_JSON_PATH.exists():
        return {}
    try:
        with open(USERS_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Гарантируем наличие новых полей для обратной совместимости
            for u in data.values():
                u.setdefault("likes_given", [])
                u.setdefault("likes_received", [])
                u.setdefault("matches", [])
                u.setdefault("filter_city_only", False)
                u.setdefault("filter_age_min", 14)
                u.setdefault("filter_age_max", 99)
                u.setdefault("is_active", True)
            return data
    except (json.JSONDecodeError, OSError):
        return {}

def update_csv(users: dict) -> None:
    """Обновляет таблицу database.csv для просмотра админом через Excel."""
    fieldnames = [
        "ID", "Username", "Телефон", "Имя", "Возраст", 
        "Пол", "Кого ищет", "Город", "О себе", "Путь к фото", 
        "Статус", "Мэтчей", "Дата регистрации"
    ]

    with open(DATABASE_CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for u in users.values():
            status = "Активна" if u.get("is_active", True) else "Скрыта/Удалена"
            writer.writerow({
                "ID": u.get("user_id"),
                "Username": u.get("username"),
                "Телефон": u.get("phone"),
                "Имя": u.get("name"),
                "Возраст": u.get("age"),
                "Пол": u.get("gender"),
                "Кого ищет": u.get("target_gender"),
                "Город": u.get("city"),
                "О себе": u.get("bio"),
                "Путь к фото": u.get("photo_path"),
                "Статус": status,
                "Мэтчей": len(u.get("matches", [])),
                "Дата регистрации": u.get("registered_at")
            })

def save_user(user_data: dict) -> None:
    """Сохраняет пользователя в users.json и обновляет CSV."""
    users = load_users()
    user_id_str = str(user_data["user_id"])
    users[user_id_str] = user_data

    with open(USERS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=4)

    update_csv(users)

def save_all_users(users: dict) -> None:
    """Перезаписывает всю базу пользователей."""
    with open(USERS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=4)
    update_csv(users)

def set_user_active_status(user_id: int, is_active: bool) -> bool:
    """Включает или выключает анкету (мягкое удаление)."""
    users = load_users()
    uid = str(user_id)
    if uid in users:
        users[uid]["is_active"] = is_active
        save_all_users(users)
        return True
    return False

def get_candidate(current_user_id: int, exclude_ids: set) -> dict | None:
    """
    Умный подбор анкеты с учетом:
    - пола (кого ищет)
    - фильтра по городу (если включен)
    - фильтра по возрасту
    - исключения уже лайкнутых людей
    """
    users = load_users()
    me = users.get(str(current_user_id))
    if not me:
        return None

    target_gender = me.get("target_gender", "Все равно")
    city_only = me.get("filter_city_only", False)
    my_city = me.get("city", "").strip().lower()
    min_age = me.get("filter_age_min", 14)
    max_age = me.get("filter_age_max", 99)
    already_liked = set(me.get("likes_given", []))

    candidates = []

    for uid_str, u in users.items():
        uid = int(uid_str)
        # Пропускаем себя, просмотренных в сессии, неактивных и тех, кого уже лайкали
        if uid == current_user_id or uid in exclude_ids or uid in already_liked:
            continue
        if not u.get("is_active", True):
            continue

        # Фильтр по полу
        if target_gender == "Мужчину" and u.get("gender") != "Мужчина":
            continue
        if target_gender == "Женщину" and u.get("gender") != "Женщина":
            continue

        # Фильтр по возрасту
        u_age = u.get("age", 18)
        if u_age < min_age or u_age > max_age:
            continue

        # Фильтр по городу
        if city_only and u.get("city", "").strip().lower() != my_city:
            continue

        candidates.append(u)

    return random.choice(candidates) if candidates else None


# =====================================================================
# 2. FSM (МАШИНА СОСТОЯНИЙ)
# =====================================================================

class Registration(StatesGroup):
    waiting_for_contact = State()
    waiting_for_name_age = State()
    waiting_for_gender = State()
    waiting_for_target_gender = State()
    waiting_for_city = State()
    waiting_for_bio = State()
    waiting_for_photo = State()

class FilterSettings(StatesGroup):
    waiting_for_age_range = State()


# =====================================================================
# 3. КЛАВИАТУРЫ
# =====================================================================

def kb_main_menu(likes_count: int = 0) -> ReplyKeyboardMarkup:
    """Главное меню со счетчиком входящих лайков."""
    likes_btn = f"💌 Лайки ({likes_count})" if likes_count > 0 else "💌 Лайки"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Смотреть анкеты")],
            [KeyboardButton(text=likes_btn), KeyboardButton(text="👤 Моя анкета")],
            [KeyboardButton(text="⚙️ Фильтры поиска")]
        ],
        resize_keyboard=True
    )

def kb_contact() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def kb_gender() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👨 Мужчина", callback_data="gender:Мужчина"),
                InlineKeyboardButton(text="👩 Женщина", callback_data="gender:Женщина")
            ]
        ]
    )

def kb_target_gender() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👨 Мужчину", callback_data="target:Мужчину"),
                InlineKeyboardButton(text="👩 Женщину", callback_data="target:Женщину")
            ],
            [
                InlineKeyboardButton(text="👥 Все равно", callback_data="target:Все равно")
            ]
        ]
    )

def kb_my_profile(is_active: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✏️ Заполнить анкету заново", callback_data="profile:edit")]
    ]
    if is_active:
        buttons.append([InlineKeyboardButton(text="🗑️ Удалить анкету (скрыть)", callback_data="profile:delete")])
    else:
        buttons.append([InlineKeyboardButton(text="♻️ Восстановить анкету", callback_data="profile:restore")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_candidate_actions(candidate_id: int, username: str) -> InlineKeyboardMarkup:
    """Действия при просмотре анкеты."""
    buttons = [
        [
            InlineKeyboardButton(text="❤️ Лайк", callback_data=f"like:{candidate_id}"),
            InlineKeyboardButton(text="👎 Дальше", callback_data=f"skip:{candidate_id}")
        ]
    ]
    clean_username = username.replace("@", "") if username and username != "Нет username" else None
    if clean_username:
        buttons.append([
            InlineKeyboardButton(text="💬 Написать в ЛС", url=f"https://t.me/{clean_username}")
        ])

    buttons.append([
        InlineKeyboardButton(text="🛑 Закончить просмотр", callback_data="browse:stop")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_incoming_like_actions(liker_id: int) -> InlineKeyboardMarkup:
    """Кнопки ответа на входящий лайк."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❤️ Ответить взаимностью", callback_data=f"accept_like:{liker_id}"),
                InlineKeyboardButton(text="👎 Пропустить", callback_data=f"decline_like:{liker_id}")
            ],
            [
                InlineKeyboardButton(text="🛑 В главное меню", callback_data="browse:stop")
            ]
        ]
    )

def kb_filter_settings(city_only: bool, min_age: int, max_age: int) -> InlineKeyboardMarkup:
    """Клавиатура настройки фильтров."""
    city_text = "🏙️ Город: Только мой ✅" if city_only else "🏙️ Город: Все города 🌍"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=city_text, callback_data="filter:toggle_city")],
            [InlineKeyboardButton(text=f"🎯 Возраст: {min_age}-{max_age} лет", callback_data="filter:change_age")],
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="browse:stop")]
        ]
    )


# =====================================================================
# 4. ОБРАБОТЧИКИ СООБЩЕНИЙ
# =====================================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# --- СТАРТ И НАЧАЛО РЕГИСТРАЦИИ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    users = load_users()
    user_id_str = str(message.from_user.id)

    if user_id_str in users:
        u = users[user_id_str]
        if not u.get("is_active", True):
            await message.answer(
                "⚠️ Твоя анкета сейчас скрыта/удалена. Никто не может её увидеть.\n"
                "Ты можешь восстановить её в любой момент:",
                reply_markup=kb_my_profile(is_active=False)
            )
            return

        likes_count = len(u.get("likes_received", []))
        await message.answer(
            f"С возвращением, {u['name']}! 👋\nИспользуй меню для поиска или управления анкетой.",
            reply_markup=kb_main_menu(likes_count)
        )
        return

    await state.set_state(Registration.waiting_for_contact)
    await message.answer(
        "👋 Привет! Добро пожаловать в сервис знакомств.\n\n"
        "Чтобы подтвердить профиль и защитить сообщество от ботов, "
        "нажми кнопку <b>«📱 Поделиться контактом»</b> ниже.",
        reply_markup=kb_contact(),
        parse_mode="HTML"
    )

@dp.message(Registration.waiting_for_contact, F.contact)
async def step_contact_success(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    username = f"@{message.from_user.username}" if message.from_user.username else "Нет username"

    await state.update_data(
        user_id=message.from_user.id,
        username=username,
        phone=phone
    )

    await state.set_state(Registration.waiting_for_name_age)
    await message.answer(
        "✅ Контакт подтвержден!\n\n"
        "Теперь напиши свое <b>Имя</b> и <b>Возраст</b> через запятую.\n"
        "<i>Пример: Иван, 19</i>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )

@dp.message(Registration.waiting_for_contact)
async def step_contact_invalid(message: Message):
    await message.answer("⚠️ Нажми именно кнопку <b>«📱 Поделиться контактом»</b> внизу экрана:", reply_markup=kb_contact(), parse_mode="HTML")


# --- ШАГ 2: ИМЯ И ВОЗРАСТ (ОТ 14 ЛЕТ) ---
@dp.message(Registration.waiting_for_name_age, F.text)
async def step_name_age(message: Message, state: FSMContext):
    text = message.text.strip()
    match = re.match(r"^([a-zA-Zа-яА-ЯёЁ\s\-]{2,30})[,\s]+(\d{1,2})$", text)

    if not match:
        await message.answer(
            "⚠️ Неверный формат! Введи имя и возраст через запятую.\n<i>Пример: Анна, 18</i>",
            parse_mode="HTML"
        )
        return

    name = match.group(1).strip().capitalize()
    age = int(match.group(2))

    if age < 14 or age > 99:
        await message.answer(
            "⚠️ Возраст должен быть от 14 до 99 лет.\nПопробуй ещё раз (например: <i>Денис, 16</i>):",
            parse_mode="HTML"
        )
        return

    await state.update_data(name=name, age=age)
    await state.set_state(Registration.waiting_for_gender)
    await message.answer(f"Приятно познакомиться, {name}! 🤝\nВыбери свой пол:", reply_markup=kb_gender())

@dp.message(Registration.waiting_for_name_age)
async def step_name_age_invalid(message: Message):
    await message.answer("⚠️ Пожалуйста, напиши текстом: <i>Имя, Возраст</i> (например: <i>Максим, 17</i>).", parse_mode="HTML")


# --- ШАГ 3: ПОЛ И ПРЕДПОЧТЕНИЯ ---
@dp.callback_query(Registration.waiting_for_gender, F.data.startswith("gender:"))
async def step_gender(callback: CallbackQuery, state: FSMContext):
    gender = callback.data.split(":")[1]
    await state.update_data(gender=gender)
    await callback.answer()

    await state.set_state(Registration.waiting_for_target_gender)
    await callback.message.edit_text("Кого ты ищешь для общения?", reply_markup=kb_target_gender())

@dp.callback_query(Registration.waiting_for_target_gender, F.data.startswith("target:"))
async def step_target_gender(callback: CallbackQuery, state: FSMContext):
    target = callback.data.split(":")[1]
    await state.update_data(target_gender=target)
    await callback.answer()

    await state.set_state(Registration.waiting_for_city)
    await callback.message.answer("🏙️ Из какого ты города? Напиши название:")


# --- ШАГ 4: ГОРОД ---
@dp.message(Registration.waiting_for_city, F.text)
async def step_city(message: Message, state: FSMContext):
    city = message.text.strip().capitalize()
    if len(city) < 2 or len(city) > 40:
        await message.answer("⚠️ Название города должно быть от 2 до 40 символов. Попробуй еще раз:")
        return

    await state.update_data(city=city)
    await state.set_state(Registration.waiting_for_bio)
    await message.answer("📝 Расскажи пару слов о себе (твои интересы, цели знакомства):")

@dp.message(Registration.waiting_for_city)
async def step_city_invalid(message: Message):
    await message.answer("⚠️ Напиши название своего города текстовым сообщением:")


# --- ШАГ 5: О СЕБЕ И ФОТО ---
@dp.message(Registration.waiting_for_bio, F.text)
async def step_bio(message: Message, state: FSMContext):
    bio = message.text.strip()
    if len(bio) < 5:
        await message.answer("⚠️ Расскажи чуть подробнее о себе (хотя бы пару слов):")
        return

    await state.update_data(bio=bio)
    await state.set_state(Registration.waiting_for_photo)
    await message.answer("📸 Супер! Теперь отправь <b>одну фотографию</b> для анкеты:", parse_mode="HTML")

@dp.message(Registration.waiting_for_bio)
async def step_bio_invalid(message: Message):
    await message.answer("⚠️ Напиши краткое описание о себе текстом:")

@dp.message(Registration.waiting_for_photo, F.photo)
async def step_photo_success(message: Message, state: FSMContext):
    user_id = message.from_user.id
    saved_photo_path = PHOTOS_DIR / f"{user_id}.jpg"

    largest_photo = message.photo[-1]
    await bot.download(largest_photo, destination=saved_photo_path)

    data = await state.get_data()
    users = load_users()
    existing_user = users.get(str(user_id), {})

    user_profile = {
        "user_id": user_id,
        "username": data.get("username", existing_user.get("username", "Нет username")),
        "phone": data.get("phone", existing_user.get("phone", "Нет номера")),
        "name": data["name"],
        "age": data["age"],
        "gender": data["gender"],
        "target_gender": data["target_gender"],
        "city": data["city"],
        "bio": data["bio"],
        "photo_path": str(saved_photo_path),
        "is_active": True,
        "likes_given": existing_user.get("likes_given", []),
        "likes_received": existing_user.get("likes_received", []),
        "matches": existing_user.get("matches", []),
        "filter_city_only": existing_user.get("filter_city_only", False),
        "filter_age_min": existing_user.get("filter_age_min", 14),
        "filter_age_max": existing_user.get("filter_age_max", 99),
        "registered_at": existing_user.get("registered_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    }

    save_user(user_profile)
    await state.clear()

    caption = (
        "🎉 <b>Регистрация успешно завершена!</b>\n\n"
        f"👤 <b>Имя:</b> {user_profile['name']}, {user_profile['age']}\n"
        f"🚻 <b>Пол:</b> {user_profile['gender']} (ищет: {user_profile['target_gender']})\n"
        f"📍 <b>Город:</b> {user_profile['city']}\n"
        f"📝 <b>О себе:</b> {user_profile['bio']}\n"
        f"🔗 <b>Telegram:</b> {user_profile['username']}\n\n"
        "Теперь ты можешь искать пару и просматривать анкеты! 👇"
    )

    photo_to_send = FSInputFile(saved_photo_path)
    likes_count = len(user_profile["likes_received"])
    await message.answer_photo(photo=photo_to_send, caption=caption, reply_markup=kb_main_menu(likes_count), parse_mode="HTML")

@dp.message(Registration.waiting_for_photo)
async def step_photo_invalid(message: Message):
    await message.answer("⚠️ Пожалуйста, пришли именно <b>фотографию</b> (как сжатое фото в Telegram):", parse_mode="HTML")


# =====================================================================
# 5. МОЯ АНКЕТА, РЕДАКТИРОВАНИЕ, СКРЫТИЕ
# =====================================================================

@dp.message(F.text == "👤 Моя анкета")
async def show_my_profile(message: Message):
    users = load_users()
    user_id_str = str(message.from_user.id)

    if user_id_str not in users:
        await message.answer("У тебя еще нет анкеты. Нажми /start для регистрации.")
        return

    u = users[user_id_str]
    is_active = u.get("is_active", True)
    status_text = "🟢 Активна (видна всем)" if is_active else "🔴 Скрыта / Удалена"

    caption = (
        f"👤 <b>Твоя анкета:</b>\n\n"
        f"<b>Имя:</b> {u['name']}, {u['age']}\n"
        f"<b>Пол:</b> {u['gender']} (ищет: {u['target_gender']})\n"
        f"<b>Город:</b> {u['city']}\n"
        f"<b>О себе:</b> {u['bio']}\n"
        f"<b>Статус:</b> {status_text}\n"
        f"<b>Симпатий:</b> ❤️ {len(u.get('likes_received', []))} | 🎉 Мэтчей: {len(u.get('matches', []))}\n"
        f"<b>Юзернейм:</b> {u['username']}"
    )

    if Path(u["photo_path"]).exists():
        photo = FSInputFile(u["photo_path"])
        await message.answer_photo(photo=photo, caption=caption, reply_markup=kb_my_profile(is_active), parse_mode="HTML")
    else:
        await message.answer(caption, reply_markup=kb_my_profile(is_active), parse_mode="HTML")

@dp.callback_query(F.data == "profile:edit")
async def edit_profile(callback: CallbackQuery, state: FSMContext):
    users = load_users()
    u = users.get(str(callback.from_user.id), {})

    username = f"@{callback.from_user.username}" if callback.from_user.username else u.get("username", "Нет username")
    phone = u.get("phone", "Не указан")

    await state.update_data(user_id=callback.from_user.id, username=username, phone=phone)
    await state.set_state(Registration.waiting_for_name_age)
    await callback.answer()
    await callback.message.answer(
        "✏️ Давай обновим анкету!\n\n"
        "Напиши свое <b>Имя</b> и <b>Возраст</b> через запятую.\n<i>Пример: Иван, 20</i>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "profile:delete")
async def delete_profile(callback: CallbackQuery):
    set_user_active_status(callback.from_user.id, is_active=False)
    await callback.answer("Анкета скрыта!")
    await callback.message.edit_reply_markup(reply_markup=kb_my_profile(is_active=False))
    await callback.message.answer(
        "🗑️ <b>Твоя анкета удалена (скрыта) из поиска.</b>\n"
        "Данные сохранены, и ты можешь восстановить анкету в любой момент кнопкой выше.",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "profile:restore")
async def restore_profile(callback: CallbackQuery):
    set_user_active_status(callback.from_user.id, is_active=True)
    await callback.answer("Анкета снова активна!")
    await callback.message.edit_reply_markup(reply_markup=kb_my_profile(is_active=True))
    users = load_users()
    likes_count = len(users.get(str(callback.from_user.id), {}).get("likes_received", []))
    await callback.message.answer("🎉 Твоя анкета снова активна и видна в поиске!", reply_markup=kb_main_menu(likes_count))


# =====================================================================
# 6. НАСТРОЙКА ФИЛЬТРОВ ПОИСКА
# =====================================================================

@dp.message(F.text == "⚙️ Фильтры поиска")
async def show_filters(message: Message):
    users = load_users()
    me = users.get(str(message.from_user.id))
    if not me:
        await message.answer("Сначала пройди регистрацию /start!")
        return

    city_only = me.get("filter_city_only", False)
    min_age = me.get("filter_age_min", 14)
    max_age = me.get("filter_age_max", 99)

    city_status = f"Только твой город ({me['city']})" if city_only else "Все города"
    text = (
        "⚙️ <b>Параметры поиска анкет:</b>\n\n"
        f"🏙️ <b>Локация:</b> {city_status}\n"
        f"🎯 <b>Возраст:</b> от {min_age} до {max_age} лет\n\n"
        "<i>Нажми на кнопку ниже, чтобы изменить фильтр:</i>"
    )

    await message.answer(text, reply_markup=kb_filter_settings(city_only, min_age, max_age), parse_mode="HTML")

@dp.callback_query(F.data == "filter:toggle_city")
async def toggle_city_filter(callback: CallbackQuery):
    users = load_users()
    uid = str(callback.from_user.id)
    if uid in users:
        current = users[uid].get("filter_city_only", False)
        users[uid]["filter_city_only"] = not current
        save_all_users(users)

        city_only = users[uid]["filter_city_only"]
        min_age = users[uid].get("filter_age_min", 14)
        max_age = users[uid].get("filter_age_max", 99)

        await callback.answer("Фильтр города обновлен!")
        await callback.message.edit_reply_markup(reply_markup=kb_filter_settings(city_only, min_age, max_age))

@dp.callback_query(F.data == "filter:change_age")
async def ask_new_age_range(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FilterSettings.waiting_for_age_range)
    await callback.answer()
    await callback.message.answer(
        "🎯 Введи желаемый диапазон возраста через дефис или пробел.\n"
        "<i>Пример: 16-20 или 18 25</i> (от 14 до 99 лет):",
        parse_mode="HTML"
    )

@dp.message(FilterSettings.waiting_for_age_range, F.text)
async def set_new_age_range(message: Message, state: FSMContext):
    text = message.text.strip()
    match = re.match(r"^(\d{1,2})[\s\-]+(\d{1,2})$", text)

    if not match:
        await message.answer("⚠️ Неверный формат! Введи два числа через дефис (например: <i>16-22</i>):", parse_mode="HTML")
        return

    age1, age2 = int(match.group(1)), int(match.group(2))
    min_age, max_age = min(age1, age2), max(age1, age2)

    if min_age < 14 or max_age > 99:
        await message.answer("⚠️ Возраст должен быть в диапазоне от 14 до 99 лет. Попробуй еще раз:")
        return

    users = load_users()
    uid = str(message.from_user.id)
    if uid in users:
        users[uid]["filter_age_min"] = min_age
        users[uid]["filter_age_max"] = max_age
        save_all_users(users)

    await state.clear()
    likes_count = len(users.get(uid, {}).get("likes_received", []))
    await message.answer(
        f"✅ Фильтр возраста успешно сохранен: <b>от {min_age} до {max_age} лет</b>!",
        reply_markup=kb_main_menu(likes_count),
        parse_mode="HTML"
    )


# =====================================================================
# 7. ПОИСК АНКЕТ, ЛАЙКИ И ВЗАИМНЫЙ МЭТЧ
# =====================================================================

async def send_next_candidate(target_message: Message, user_id: int, state: FSMContext):
    """Показывает следующего кандидата с учетом фильтров."""
    users = load_users()
    me = users.get(str(user_id))

    if not me or not me.get("is_active", True):
        await target_message.answer("Сначала заполни или активируй свою анкету!")
        return

    state_data = await state.get_data()
    seen_ids = set(state_data.get("seen_ids", []))

    candidate = get_candidate(current_user_id=user_id, exclude_ids=seen_ids)

    if not candidate:
        await state.update_data(seen_ids=[])
        likes_count = len(me.get("likes_received", []))
        await target_message.answer(
            "😔 Новых анкет по твоим фильтрам пока нет или ты посмотрел(а) всех доступных пользователей!\n"
            "Попробуй изменить фильтры возраста/города или заглянуть позже.",
            reply_markup=kb_main_menu(likes_count)
        )
        await state.clear()
        return

    seen_ids.add(candidate["user_id"])
    await state.update_data(seen_ids=list(seen_ids))

    caption = (
        f"👤 <b>{candidate['name']}</b>, {candidate['age']}, {candidate['city']}\n\n"
        f"📝 <b>О себе:</b> {candidate['bio']}\n"
    )
    if candidate.get("username") and candidate["username"] != "Нет username":
        caption += f"🔗 <b>Telegram:</b> {candidate['username']}"

    photo_path = Path(candidate["photo_path"])
    kb = kb_candidate_actions(candidate["user_id"], candidate.get("username", ""))

    if photo_path.exists():
        photo = FSInputFile(photo_path)
        await target_message.answer_photo(photo=photo, caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        await target_message.answer(caption, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "🔍 Смотреть анкеты")
async def start_browsing(message: Message, state: FSMContext):
    await state.clear()
    await send_next_candidate(message, message.from_user.id, state)

@dp.callback_query(F.data.startswith("skip:"))
async def skip_candidate(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_next_candidate(callback.message, callback.from_user.id, state)

@dp.callback_query(F.data.startswith("like:"))
async def like_candidate(callback: CallbackQuery, state: FSMContext):
    candidate_id = int(callback.data.split(":")[1])
    sender_id = callback.from_user.id

    users = load_users()
    sender = users.get(str(sender_id))
    candidate = users.get(str(candidate_id))

    if not sender or not candidate:
        await callback.answer("Ошибка пользователя.")
        return

    # Записываем, что мы лайкнули кандидата
    if candidate_id not in sender.get("likes_given", []):
        sender["likes_given"].append(candidate_id)

    # ПРОВЕРКА НА ВЗАИМНЫЙ МЭТЧ! 🎉
    is_match = sender_id in candidate.get("likes_given", [])

    if is_match:
        # Это взаимный мэтч!
        if candidate_id not in sender.get("matches", []):
            sender["matches"].append(candidate_id)
        if sender_id not in candidate.get("matches", []):
            candidate["matches"].append(sender_id)

        # Удаляем из входящих лайков, так как пара уже сложилась
        if sender_id in candidate.get("likes_received", []):
            candidate["likes_received"].remove(sender_id)
        if candidate_id in sender.get("likes_received", []):
            sender["likes_received"].remove(candidate_id)

        save_all_users(users)
        await callback.answer("🎉 Это взаимная симпатия!", show_alert=True)

        # Отправляем экраны мэтча обоим
        clean_cand_user = candidate.get("username", "").replace("@", "")
        cand_contact = f"@{clean_cand_user}" if clean_cand_user and clean_cand_user != "Нет username" else f'<a href="tg://user?id={candidate_id}">{candidate["name"]}</a>'
        match_msg_sender = (
            "🎉🎉🎉 <b>У ВАС ВЗАИМНАЯ СИМПАТИЯ!</b> 🎉🎉🎉\n\n"
            f"Тебе и <b>{candidate['name']}</b> ({candidate['age']} лет, {candidate['city']}) понравились анкеты друг друга!\n\n"
            f"👉 <b>Написать в Telegram:</b> {cand_contact}"
        )
        if Path(candidate["photo_path"]).exists():
            await bot.send_photo(sender_id, photo=FSInputFile(candidate["photo_path"]), caption=match_msg_sender, parse_mode="HTML")
        else:
            await bot.send_message(sender_id, text=match_msg_sender, parse_mode="HTML")

        clean_sender_user = sender.get("username", "").replace("@", "")
        sender_contact = f"@{clean_sender_user}" if clean_sender_user and clean_sender_user != "Нет username" else f'<a href="tg://user?id={sender_id}">{sender["name"]}</a>'
        match_msg_candidate = (
            "🎉🎉🎉 <b>У ВАС ВЗАИМНАЯ СИМПАТИЯ!</b> 🎉🎉🎉\n\n"
            f"Тебе и <b>{sender['name']}</b> ({sender['age']} лет, {sender['city']}) понравились анкеты друг друга!\n\n"
            f"👉 <b>Написать в Telegram:</b> {sender_contact}"
        )
        try:
            if Path(sender["photo_path"]).exists():
                await bot.send_photo(candidate_id, photo=FSInputFile(sender["photo_path"]), caption=match_msg_candidate, parse_mode="HTML")
            else:
                await bot.send_message(candidate_id, text=match_msg_candidate, parse_mode="HTML")
        except Exception:
            pass

    else:
        # Обычный лайк — добавляем во входящие кандидату
        if sender_id not in candidate.get("likes_received", []):
            candidate["likes_received"].append(sender_id)
        save_all_users(users)
        await callback.answer("❤️ Лайк отправлен!")

        # Уведомляем кандидата о новом лайке
        try:
            likes_count = len(candidate["likes_received"])
            await bot.send_message(
                chat_id=candidate_id,
                text=f"💌 <b>Кому-то понравилась твоя анкета!</b>\n"
                     f"У тебя новых лайков: <b>{likes_count}</b>.\n"
                     f"Открой раздел <b>«💌 Лайки»</b> в меню бота, чтобы посмотреть!",
                parse_mode="HTML"
            )
        except Exception:
            pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    await send_next_candidate(callback.message, sender_id, state)


# =====================================================================
# 8. ПРОСМОТР ВХОДЯЩИХ ЛАЙКОВ («КТО МЕНЯ ОЦЕНИЛ»)
# =====================================================================

async def send_next_incoming_like(target_message: Message, user_id: int):
    """Показывает профиль человека из очереди входящих лайков."""
    users = load_users()
    me = users.get(str(user_id))

    if not me or not me.get("likes_received"):
        likes_cnt = len(me.get("likes_received", [])) if me else 0
        await target_message.answer(
            "💌 У тебя пока нет новых лайков.\nПродолжай смотреть анкеты в поиске!",
            reply_markup=kb_main_menu(likes_cnt)
        )
        return

    liker_id = me["likes_received"][0]
    liker = users.get(str(liker_id))

    if not liker or not liker.get("is_active", True):
        # Если лайкнувший удалил анкету
        me["likes_received"].pop(0)
        save_all_users(users)
        await send_next_incoming_like(target_message, user_id)
        return

    caption = (
        f"💌 <b>Этому человеку понравилась твоя анкета:</b>\n\n"
        f"👤 <b>{liker['name']}</b>, {liker['age']}, {liker['city']}\n"
        f"📝 <b>О себе:</b> {liker['bio']}\n"
    )

    photo_path = Path(liker["photo_path"])
    kb = kb_incoming_like_actions(liker_id)

    if photo_path.exists():
        await target_message.answer_photo(photo=FSInputFile(photo_path), caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        await target_message.answer(caption, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text.startswith("💌 Лайки"))
async def open_incoming_likes(message: Message, state: FSMContext):
    await state.clear()
    await send_next_incoming_like(message, message.from_user.id)

# Ответ взаимностью из раздела лайков
@dp.callback_query(F.data.startswith("accept_like:"))
async def accept_incoming_like(callback: CallbackQuery):
    liker_id = int(callback.data.split(":")[1])
    my_id = callback.from_user.id

    users = load_users()
    me = users.get(str(my_id))
    liker = users.get(str(liker_id))

    if not me or not liker:
        await callback.answer("Ошибка.")
        return

    # Добавляем в мэтчи обоим
    if liker_id not in me.get("matches", []):
        me["matches"].append(liker_id)
    if my_id not in liker.get("matches", []):
        liker["matches"].append(my_id)

    # Удаляем из входящих
    if liker_id in me.get("likes_received", []):
        me["likes_received"].remove(liker_id)

    save_all_users(users)
    await callback.answer("🎉 Взаимная симпатия!", show_alert=True)

    # Отправляем контакты
    clean_liker = liker.get("username", "").replace("@", "")
    liker_contact = f"@{clean_liker}" if clean_liker and clean_liker != "Нет username" else f'<a href="tg://user?id={liker_id}">{liker["name"]}</a>'
    await callback.message.answer(
        f"🎉 <b>У вас взаимная симпатия с {liker['name']}!</b>\n\n"
        f"👉 <b>Написать в Telegram:</b> {liker_contact}",
        parse_mode="HTML"
    )

    # Уведомляем того, кому ответили
    try:
        clean_my = me.get("username", "").replace("@", "")
        my_contact = f"@{clean_my}" if clean_my and clean_my != "Нет username" else f'<a href="tg://user?id={my_id}">{me["name"]}</a>'
        await bot.send_message(
            liker_id,
            f"🎉 <b>{me['name']} ответил(а) тебе взаимностью!</b>\n\n"
            f"👉 <b>Написать в Telegram:</b> {my_contact}",
            parse_mode="HTML"
        )
    except Exception:
        pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    # Показываем следующий лайк
    await send_next_incoming_like(callback.message, my_id)

# Пропуск лайка
@dp.callback_query(F.data.startswith("decline_like:"))
async def decline_incoming_like(callback: CallbackQuery):
    liker_id = int(callback.data.split(":")[1])
    my_id = callback.from_user.id

    users = load_users()
    me = users.get(str(my_id))
    if me and liker_id in me.get("likes_received", []):
        me["likes_received"].remove(liker_id)
        save_all_users(users)

    await callback.answer("Пропущено")
    try:
        await callback.message.delete()
    except Exception:
        pass

    await send_next_incoming_like(callback.message, my_id)

@dp.callback_query(F.data == "browse:stop")
async def stop_actions(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    users = load_users()
    likes_count = len(users.get(str(callback.from_user.id), {}).get("likes_received", []))
    await callback.answer("Главное меню")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Ты вернулся в главное меню:", reply_markup=kb_main_menu(likes_count))


# =====================================================================
# 9. ТОЧКА ВХОДА (ЗАПУСК)
# =====================================================================

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    logging.info("Бот знакомств успешно запущен...")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")