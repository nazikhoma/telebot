import os
import uuid
import re
import json
import hashlib
import logging
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils import executor
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Float, ForeignKey, select
from sqlalchemy.exc import SQLAlchemyError
import aiohttp

# Завантаження змінних середовища
load_dotenv()

# Конфігурації
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
WORKSECTION_API_URL = os.getenv("WORKSECTION_API_URL")
PAGE_SIZE = 4
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# Налаштування логування
LOG_FILENAME = 'bot.log'
logging.basicConfig(
    level=logging.DEBUG,  # Змініть на INFO або WARNING в продакшн
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILENAME, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Ініціалізація бота та диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# Налаштування бази даних з використанням SQLAlchemy та AsyncIO
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)

# Моделі ORM
class User(Base):
    __tablename__ = 'users'
    UserId = Column(Integer, primary_key=True, index=True)
    UserPhoneNumber = Column(String(20), unique=True, nullable=False)
    TelegramChatId = Column(Integer, unique=True, nullable=True)

class Project(Base):
    __tablename__ = 'projects'
    ProjectId = Column(Integer, primary_key=True, index=True)
    ProjectName = Column(String(100), nullable=False)
    ProjectUserId = Column(Integer, ForeignKey('users.UserId'), nullable=False)
    UserDev = Column(String(100), nullable=True)

class Task(Base):
    __tablename__ = 'tasks'
    TaskId = Column(Integer, primary_key=True, index=True)
    TaskName = Column(String(100), nullable=False)
    TaskLeader = Column(String(100), nullable=True)
    TaskDescription = Column(String(500), nullable=False)
    ImagePath = Column(String(255), nullable=True)
    TaskProjectId = Column(Integer, ForeignKey('projects.ProjectId'), nullable=False)

# Створення таблиць (виконується асинхронно)
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("База даних ініціалізована.")

# Валідація номера телефону
def is_valid_phone(phone: str) -> bool:
    pattern = re.compile(r'^\+?\d{10,15}$')
    return bool(pattern.match(phone))

# Отримання користувача за TelegramChatId
async def get_user_by_chat_id(chat_id: int, session: AsyncSession):
    try:
        result = await session.execute(select(User).where(User.TelegramChatId == chat_id))
        user = result.scalars().first()
        logger.debug(f"Отримано користувача за ChatId {chat_id}: {user}")
        return user
    except SQLAlchemyError as e:
        logger.error(f"Помилка при отриманні користувача за ChatId {chat_id}: {e}", exc_info=True)
        return None

# Додавання або оновлення користувача
async def add_or_update_user(chat_id: int, user_phone: str, session: AsyncSession):
    try:
        result = await session.execute(select(User).where(User.UserPhoneNumber == user_phone))
        user = result.scalars().first()
        if user:
            user.TelegramChatId = chat_id
            logger.info(f"Оновлено TelegramChatId для користувача з номером {user_phone}")
        else:
            new_user = User(UserPhoneNumber=user_phone, TelegramChatId=chat_id)
            session.add(new_user)
            logger.info(f"Додано нового користувача з номером {user_phone}")
        await session.commit()
        return True
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Помилка при додаванні/оновленні користувача: {e}", exc_info=True)
        return False

# Отримання проектів за номером телефону
async def get_projects_by_phone(user_phone: str, session: AsyncSession):
    try:
        result = await session.execute(
            select(Project).join(User).where(User.UserPhoneNumber == user_phone)
        )
        projects = result.scalars().all()
        logger.debug(f"Отримано проєкти для номера {user_phone}: {projects}")
        return projects
    except SQLAlchemyError as e:
        logger.error(f"Помилка при отриманні проєктів для номера {user_phone}: {e}", exc_info=True)
        return []

# Створення задачі через Worksection API
async def create_task_with_file_async(project_id: int, task_name: str, description: str, file_path: str = None) -> str:
    action = "post_task"
    query_params = (
        f"action={action}"
        f"&id_project={project_id}"
        f"&title={task_name}"
        f"&text={description}"
    )
    hash_hex = hashlib.md5((query_params + API_KEY).encode()).hexdigest()
    request_url = f"{WORKSECTION_API_URL}?{query_params}&hash={hash_hex}"
    
    files = {}
    if file_path and os.path.exists(file_path):
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        files = {"attach[0]": (os.path.basename(file_path), file_bytes, "image/jpeg")}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(request_url, data=files) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info("Задача успішно створена у Worksection.")
                        return "Задача успішно створена у Worksection."
                    else:
                        error_msg = data.get("error", "Невідома помилка API.")
                        logger.error(f"Помилка API Worksection: {error_msg}")
                        return f"Помилка API Worksection: {error_msg}"
                else:
                    logger.error(f"HTTP помилка Worksection: {resp.status}")
                    return f"HTTP помилка Worksection: {resp.status}"
    except aiohttp.ClientError as e:
        logger.error(f"Сталася помилка при відправленні задачі: {e}", exc_info=True)
        return f"Сталася помилка при відправленні задачі: {e}"

# Отримання імені керівника проекту
async def get_project_manager_name(project_id: int) -> str:
    action = "get_project"
    query_params = f"action={action}&id_project={project_id}"
    hash_hex = hashlib.md5((query_params + API_KEY).encode()).hexdigest()
    request_url = f"{WORKSECTION_API_URL}?{query_params}&hash={hash_hex}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(request_url) as resp:
                if resp.status != 200:
                    logger.error(f"HTTP помилка при отриманні керівника: {resp.status}")
                    return f"Помилка HTTP: {resp.status}"
                data = await resp.json()
                if "user_to" in data and "name" in data["user_to"]:
                    manager_name = data["user_to"]["name"]
                    logger.info(f"Керівник проекту {project_id}: {manager_name}")
                    return manager_name
                else:
                    logger.warning(f"Не вдалося знайти інформацію про керівника проекту {project_id}")
                    return "Не вдалося знайти інформацію про керівника проекту."
    except aiohttp.ClientError as e:
        logger.error(f"Сталася помилка при отриманні керівника проекту: {e}", exc_info=True)
        return f"Сталася помилка при отриманні керівника проекту: {e}"
    except ValueError as e:
        logger.error(f"Помилка розбору JSON: {e}", exc_info=True)
        return "Сталася помилка при обробці відповіді: некоректний JSON."
    except Exception as e:
        logger.critical(f"Несподівана помилка: {e}", exc_info=True)
        return f"Несподівана помилка: {e}"

# Збереження задачі в базу даних
async def save_task_to_db(user_id: int, task_name: str, description: str, file_path: str, project_id: int, task_leader: str, session: AsyncSession):
    try:
        new_task = Task(
            TaskName=task_name,
            TaskLeader=task_leader,
            TaskDescription=description,
            ImagePath=file_path,
            TaskProjectId=project_id
        )
        session.add(new_task)
        await session.commit()
        logger.info(f"Задача '{task_name}' успішно збережена у базу даних.")
        return "Задача успішно збережена у базу даних."
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Помилка збереження задачі у базу даних: {e}", exc_info=True)
        return f"Помилка збереження задачі у базу даних: {e}"

# Створення клавіатури проектів
def build_projects_keyboard(projects, page=0) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    total_projects = len(projects)
    total_pages = (total_projects + PAGE_SIZE - 1) // PAGE_SIZE
    start_index = page * PAGE_SIZE
    end_index = start_index + PAGE_SIZE
    page_projects = projects[start_index:end_index]

    for prj in page_projects:
        callback_data = f"select_project_{prj.ProjectId}"
        markup.add(InlineKeyboardButton(prj.ProjectName, callback_data=callback_data))

    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton("« Попередня", callback_data=f"page_{page - 1}")
            )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton("Наступна »", callback_data=f"page_{page + 1}")
            )
        if nav_buttons:
            markup.row(*nav_buttons)
    return markup

# Обробник команди /start
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    try:
        logger.info(f"Користувач {message.from_user.id} надіслав команду /start")
        markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add(
            KeyboardButton("Надати номер телефону", request_contact=True)
        )
        await message.answer(
            "Привіт! Натисніть кнопку, щоб поділитися своїм номером телефону.",
            reply_markup=markup
        )
        logger.debug(f"Відповідь на /start надіслана користувачу {message.from_user.id}")
    except Exception as e:
        logger.error(f"Помилка у функції send_welcome: {e}", exc_info=True)
        await message.answer("Сталася помилка при обробці вашої команди.")

# Обробник контактів
@dp.message_handler(content_types=["contact"])
async def handle_phone_number(message: types.Message):
    try:
        contact = message.contact
        if not contact:
            logger.warning(f"Користувач {message.from_user.id} надіслав пустий контакт.")
            await message.answer("Сталась помилка, контакт не розпізнано.")
            return

        user_phone = contact.phone_number
        logger.info(f"Користувач {message.from_user.id} надав номер телефону: {user_phone}")

        if not is_valid_phone(user_phone):
            logger.warning(f"Користувач {message.from_user.id} надав некоректний номер телефону: {user_phone}")
            await message.answer("Некоректний формат номера телефону. Спробуйте ще раз.")
            return

        async with async_session() as session:
            success = await add_or_update_user(message.chat.id, user_phone, session)
            if not success:
                await message.answer("Сталася помилка при збереженні даних. Спробуйте пізніше.")
                return

            projects = await get_projects_by_phone(user_phone, session)
            if not projects:
                await message.answer("Не знайдено проєктів для цього номера телефону.")
                logger.info(f"Користувач {message.from_user.id} не має проєктів для номера {user_phone}")
                return

            markup = build_projects_keyboard(projects, page=0)
            await message.answer("Оберіть проєкт:", reply_markup=markup)
            logger.debug(f"Клавіатура проєктів надіслана користувачу {message.from_user.id}")
    except Exception as e:
        logger.error(f"Помилка у функції handle_phone_number: {e}", exc_info=True)
        await message.answer("Сталася помилка при обробці вашого номера телефону.")

# Обробник текстових повідомлень
@dp.message_handler(content_types=["text"])
async def handle_text(message: types.Message):
    try:
        logger.info(f"Отримано текстове повідомлення від користувача {message.from_user.id}: {message.text}")
        if message.text.lower() == "привіт":
            await message.answer("Привіт!")
            logger.debug(f"Відповідь 'Привіт!' надіслана користувачу {message.from_user.id}")
        else:
            today = datetime.today().strftime('%Y%m%d')
            bank_api = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
            logger.debug(f"Запит до API банку: {bank_api}")
            async with aiohttp.ClientSession() as session:
                async with session.get(bank_api, timeout=10) as resp:
                    if resp.status != 200:
                        logger.error(f"HTTP помилка при запиті до API банку: {resp.status}")
                        await message.answer("Сталася помилка при отриманні курсу валюти. Спробуйте пізніше.")
                        return
                    data = await resp.json()
                    logger.debug(f"Отримані дані від API банку: {data}")
                    
                    # Шукаємо курс валюти за кодом (наприклад, USD, EUR)
                    currency_code = message.text.upper()
                    currency = next((item for item in data if item["cc"] == currency_code), None)
                    
                    if currency:
                        value = currency["rate"]
                        await message.answer(f"Привіт, курс {currency['txt']} на сьогодні: {value} UAH")
                        logger.info(f"Курс валюти {currency_code} дорівнює {value}")
                        logger.debug(f"Відповідь з курсом валюти {currency_code} надіслана користувачу {message.from_user.id}")
                    else:
                        await message.answer("Помилка, таку валюту не знайдено")
                        logger.warning(f"Валюта {currency_code} не знайдена для користувача {message.from_user.id}")
    except aiohttp.ClientError as e:
        logger.error(f"Помилка при запиті до API банку: {e}", exc_info=True)
        await message.answer("Сталася помилка при отриманні курсу валюти. Спробуйте пізніше.")
    except (ValueError, KeyError) as e:
        logger.error(f"Помилка обробки даних від API банку: {e}", exc_info=True)
        await message.answer("Сталася помилка при обробці даних. Спробуйте пізніше.")
    except Exception as e:
        logger.critical(f"Невідома помилка у функції handle_text: {e}", exc_info=True)
        await message.answer("Сталася невідома помилка. Зверніться до адміністратора.")

# Обробник колбеків від клавіатури
@dp.callback_query_handler(lambda c: c.data and (c.data.startswith('select_project_') or c.data.startswith('page_')))
async def process_callback(callback_query: types.CallbackQuery):
    try:
        chat_id = callback_query.message.chat.id
        data = callback_query.data
        logger.info(f"Отримано колбек від користувача {chat_id}: {data}")

        async with async_session() as session:
            user = await get_user_by_chat_id(chat_id, session)
            if not user:
                await bot.answer_callback_query(callback_query.id, text="Користувача не знайдено.", show_alert=True)
                logger.warning(f"Користувач {chat_id} не знайдений у базі даних.")
                return

            if data.startswith("select_project_"):
                pid_str = data.replace("select_project_", "")
                try:
                    pid = int(pid_str)
                except ValueError:
                    await bot.answer_callback_query(callback_query.id, text="Некоректний ProjectId.", show_alert=True)
                    logger.error(f"Некоректний ProjectId: {pid_str} від користувача {chat_id}")
                    return

                result = await session.execute(select(Project).where(Project.ProjectId == pid))
                project = result.scalars().first()
                if not project:
                    await bot.answer_callback_query(callback_query.id, text="Проєкт не знайдено.", show_alert=True)
                    logger.warning(f"Проєкт з ID {pid} не знайдено для користувача {chat_id}")
                    return

                # Запит назви та опису задачі
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=callback_query.message.message_id,
                    text=f"Ви обрали проєкт: {project.ProjectName}"
                )
                await bot.send_message(chat_id, "Введіть назву задачі:", reply_markup=ReplyKeyboardRemove())
                logger.debug(f"Користувачу {chat_id} запропоновано ввести назву задачі.")

            elif data.startswith("page_"):
                new_page_str = data.replace("page_", "")
                try:
                    new_page = int(new_page_str)
                except ValueError:
                    await bot.answer_callback_query(callback_query.id, text="Некоректний номер сторінки.", show_alert=True)
                    logger.error(f"Некоректний номер сторінки: {new_page_str} від користувача {chat_id}")
                    return

                projects = await get_projects_by_phone(user.UserPhoneNumber, session)
                markup = build_projects_keyboard(projects, new_page)
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=callback_query.message.message_id,
                    reply_markup=markup
                )
                await bot.answer_callback_query(callback_query.id)
                logger.debug(f"Користувачу {chat_id} показана сторінка {new_page} проєктів.")
    except Exception as e:
        logger.critical(f"Невідома помилка у функції process_callback: {e}", exc_info=True)
        await bot.answer_callback_query(callback_query.id, text="Сталася невідома помилка.", show_alert=True)

# Обробник назви задачі
@dp.message_handler(lambda message: True)
async def handle_task_input(message: types.Message):
    try:
        chat_id = message.chat.id
        text = message.text.strip()
        logger.info(f"Користувач {chat_id} ввів: {text}")

        # Перевірка, чи користувач очікує вводу назви задачі
        async with async_session() as session:
            user = await get_user_by_chat_id(chat_id, session)
            if not user:
                logger.warning(f"Користувач {chat_id} не знайдений у базі даних.")
                return

            # Тут потрібно реалізувати механізм стейту (можна використовувати FSM або простий словник)
            # Для простоти, пропустимо цей крок і зосередимося на логуванні

            # Наприклад, якщо ви використовуєте простий словник для стейту:
            # user_state = {}
            # Але для асинхронності краще використовувати Redis або FSM

            # Тимчасово відповімо користувачу
            await message.answer("Функціонал для обробки задач ще не реалізовано.")
            logger.debug(f"Функціонал для обробки задач ще не реалізовано для користувача {chat_id}")
    except Exception as e:
        logger.error(f"Помилка у функції handle_task_input: {e}", exc_info=True)
        await message.answer("Сталася помилка при обробці вашого повідомлення.")

# Функція для запуску бота
def main():
    logger.info("Бот запускається...")
    try:
        executor.start_polling(dp, skip_updates=True)
    except Exception as e:
        logger.critical(f"Неможливо запустити бота: {e}", exc_info=True)

if __name__ == "__main__":
    main()
