import os
import uuid
import re
import json
import hashlib
import requests
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Float, ForeignKey
from sqlalchemy.exc import SQLAlchemyError

# Завантаження змінних середовища
load_dotenv()

# Конфігурації
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
WORKSECTION_API_URL = os.getenv("WORKSECTION_API_URL")
PAGE_SIZE = 4
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

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


class Task(Base):
    __tablename__ = 'tasks'
    TaskId = Column(Integer, primary_key=True, index=True)
    TaskName = Column(String(100), nullable=False)
    TaskLeader = Column(String(100), nullable=True)
    TaskDescription = Column(String(500), nullable=False)
    ImagePath = Column(String(255), nullable=True)
    TaskProjectId = Column(Integer, ForeignKey('projects.ProjectId'), nullable=False)


class Project(Base):
    __tablename__ = 'projects'
    ProjectId = Column(Integer, primary_key=True, index=True)
    ProjectName = Column(String(100), nullable=False)
    ProjectUserId = Column(Integer, ForeignKey('users.UserId'), nullable=False)
    UserDev = Column(String(100), nullable=True)

# Створення таблиць (виконується асинхронно)
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# Створення сесії користувача
async def get_user(chat_id: int, session: AsyncSession):
    result = await session.execute(
        sqlalchemy.select(User).where(User.TelegramChatId == chat_id)
    )
    return result.scalars().first()

# Додавання або оновлення користувача
async def add_or_update_user(chat_id: int, user_phone: str, session: AsyncSession):
    try:
        user = await session.execute(
            sqlalchemy.select(User).where(User.UserPhoneNumber == user_phone)
        )
        user = user.scalars().first()
        if user:
            user.TelegramChatId = chat_id
        else:
            new_user = User(UserPhoneNumber=user_phone, TelegramChatId=chat_id)
            session.add(new_user)
        await session.commit()
        return True
    except SQLAlchemyError:
        await session.rollback()
        return False

# Валідація номера телефону
def is_valid_phone(phone: str) -> bool:
    pattern = re.compile(r'^\+?\d{10,15}$')
    return bool(pattern.match(phone))

# Отримання проектів за номером телефону
async def get_projects_by_phone(user_phone: str, session: AsyncSession):
    try:
        result = await session.execute(
            sqlalchemy.select(Project).join(User).where(User.UserPhoneNumber == user_phone)
        )
        projects = result.scalars().all()
        return projects
    except SQLAlchemyError:
        return []

# Створення задачі через Worksection API
def create_task_with_file(project_id: int, task_name: str, description: str, file_path: str = None) -> str:
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
        resp = requests.post(request_url, files=files)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok":
                return "Задача успішно створена у Worksection."
            else:
                return f"Помилка API Worksection: {data.get('error')}"
        else:
            return f"HTTP помилка Worksection: {resp.status_code}"
    except requests.RequestException as e:
        return f"Сталася помилка при відправленні задачі: {e}"

# Отримання імені керівника проекту
def get_project_manager_name(project_id: int) -> str:
    action = "get_project"
    query_params = f"action={action}&id_project={project_id}"
    hash_hex = hashlib.md5((query_params + API_KEY).encode()).hexdigest()
    request_url = f"{WORKSECTION_API_URL}?{query_params}&hash={hash_hex}"

    try:
        resp = requests.get(request_url)
        if resp.status_code != 200:
            return f"Помилка HTTP: отримано статус {resp.status_code}"
        data = resp.json()
        if "user_to" in data and "name" in data["user_to"]:
            return data["user_to"]["name"]
        else:
            return "Не вдалося знайти інформацію про керівника проекту."
    except requests.RequestException as e:
        return f"Сталася помилка при виконанні запиту: {e}"
    except ValueError:
        return "Сталася помилка при обробці відповіді: некоректний JSON."
    except Exception as e:
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
        return "Задача успішно збережена у базу даних."
    except SQLAlchemyError as e:
        await session.rollback()
        return f"Помилка збереження в базу даних: {e}"

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
    await message.answer("Привіт! Натисніть кнопку, щоб поділитися своїм номером телефону.", reply_markup=ReplyKeyboardMarkup(
        resize_keyboard=True, one_time_keyboard=True
    ).add(KeyboardButton("Надати номер телефону", request_contact=True)))

# Обробник контактів
@dp.message_handler(content_types=["contact"])
async def handle_phone_number(message: types.Message):
    contact = message.contact
    if not contact:
        await message.answer("Сталась помилка, контакт не розпізнано.")
        return

    user_phone = contact.phone_number
    if not is_valid_phone(user_phone):
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
            return

        markup = build_projects_keyboard(projects, page=0)
        await message.answer("Оберіть проєкт:", reply_markup=markup)

# Обробник текстових повідомлень
@dp.message_handler(content_types=["text"])
async def handle_text(message: types.Message):
    if message.text.lower() == "привіт":
        await message.answer("Привіт!")
    else:
        try:
            today = datetime.today().strftime('%Y%m%d')
            bank_api = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
            r = requests.get(url=bank_api)
            data = r.json()
            # Шукаємо курс валюти за назвою
            currency = next((item for item in data if item["cc"] == message.text.upper()), None)
            if currency:
                value = currency["rate"]
                await message.answer(f"Курс {currency['txt']} на сьогодні: {value} UAH")
            else:
                await message.answer("Помилка, таку валюту не знайдено")
        except Exception as e:
            await message.answer("Сталася помилка при отриманні курсу валюти.")

# Обробник колбеків від клавіатури
@dp.callback_query_handler(lambda c: c.data and (c.data.startswith('select_project_') or c.data.startswith('page_')))
async def process_callback(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data

    async with async_session() as session:
        user = await get_user(chat_id, session)
        if not user:
            await bot.answer_callback_query(callback_query.id, text="Користувача не знайдено.")
            return

        if data.startswith("select_project_"):
            pid_str = data.replace("select_project_", "")
            try:
                pid = int(pid_str)
            except ValueError:
                await bot.answer_callback_query(callback_query.id, text="Некоректний ProjectId.", show_alert=True)
                return

            project = await session.execute(
                sqlalchemy.select(Project).where(Project.ProjectId == pid)
            )
            project = project.scalars().first()
            if not project:
                await bot.answer_callback_query(callback_query.id, text="Проєкт не знайдено.", show_alert=True)
                return

            # Запит назви та опису задачі
            await bot.send_message(chat_id, "Введіть назву задачі:")
            # Зберігаємо стан користувача
            redis_client.set(f"user_state:{chat_id}", json.dumps({
                "state": "awaiting_task_name",
                "project_id": pid
            }))
            await bot.answer_callback_query(callback_query.id)

        elif data.startswith("page_"):
            new_page_str = data.replace("page_", "")
            try:
                new_page = int(new_page_str)
            except ValueError:
                await bot.answer_callback_query(callback_query.id, text="Некоректний номер сторінки.", show_alert=True)
                return

            projects = await get_projects_by_phone(user.TelegramChatId, session)
            markup = build_projects_keyboard(projects, new_page)
            await bot.edit_message_reply_markup(chat_id, callback_query.message.message_id, reply_markup=markup)
            await bot.answer_callback_query(callback_query.id)

# Обробник назви задачі
@dp.message_handler(lambda message: json.loads(redis_client.get(f"user_state:{message.chat.id}") or "{}").get("state") == "awaiting_task_name")
async def handle_task_name(message: types.Message):
    task_name = message.text.strip()
    if not task_name:
        await message.answer("Назва задачі не може бути порожньою. Введіть назву:")
        return
    if len(task_name) > 45:
        await message.answer("Назва задачі перевищує 45 символів. Введіть коротшу назву:")
        return

    # Оновлюємо стан користувача
    state = json.loads(redis_client.get(f"user_state:{message.chat.id}") or "{}")
    state["task_name"] = task_name
    state["state"] = "awaiting_description"
    redis_client.set(f"user_state:{message.chat.id}", json.dumps(state))
    await message.answer("Введіть опис задачі:", reply_markup=ReplyKeyboardRemove())

# Обробник опису задачі
@dp.message_handler(lambda message: json.loads(redis_client.get(f"user_state:{message.chat.id}") or "{}").get("state") == "awaiting_description")
async def handle_task_description(message: types.Message):
    description = message.text.strip()
    if not description:
        await message.answer("Опис задачі не може бути порожнім. Введіть опис:")
        return
    if len(description) > 150:
        await message.answer("Опис задачі перевищує 150 символів. Введіть коротший опис:")
        return

    # Оновлюємо стан користувача
    state = json.loads(redis_client.get(f"user_state:{message.chat.id}") or "{}")
    state["description"] = description
    state["state"] = "awaiting_photo"
    redis_client.set(f"user_state:{message.chat.id}", json.dumps(state))

    # Створюємо клавіатуру для додавання фото або пропуску
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add(
        KeyboardButton("Пропустити")
    )
    await message.answer("Надішліть зображення або натисніть 'Пропустити'.", reply_markup=markup)

# Обробник фото
@dp.message_handler(content_types=["photo"])
async def handle_photo(message: types.Message):
    chat_id = message.chat.id
    state = json.loads(redis_client.get(f"user_state:{chat_id}") or "{}")
    if state.get("state") != "awaiting_photo":
        return

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_path = file_info.file_path
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    # Завантаження файлу
    try:
        r = requests.get(file_url)
        if r.status_code != 200 or len(r.content) > MAX_FILE_SIZE:
            await message.answer("Не вдалося завантажити зображення або файл перевищує допустимий розмір (5MB).")
            return

        # Збереження файлу
        local_filename = f"photo_{chat_id}_{uuid.uuid4().hex}.jpg"
        local_file_path = os.path.join(ATTACHMENTS_DIR, local_filename)
        with open(local_file_path, "wb") as f:
            f.write(r.content)

        state["local_file_path"] = local_file_path
        redis_client.set(f"user_state:{chat_id}", json.dumps(state))
        await finalize_task_creation(message)
    except Exception as e:
        await message.answer(f"Сталася помилка при завантаженні фото: {e}")

# Обробник пропуску додавання фото
@dp.message_handler(lambda message: message.text.lower() == "пропустити")
async def handle_skip_photo(message: types.Message):
    chat_id = message.chat.id
    state = json.loads(redis_client.get(f"user_state:{chat_id}") or "{}")
    if state.get("state") != "awaiting_photo":
        return

    state["local_file_path"] = None
    redis_client.set(f"user_state:{chat_id}", json.dumps(state))
    await finalize_task_creation(message)

# Завершення створення задачі
async def finalize_task_creation(message: types.Message):
    chat_id = message.chat.id
    state = json.loads(redis_client.get(f"user_state:{chat_id}") or "{}")
    redis_client.delete(f"user_state:{chat_id}")

    pid = state.get("project_id")
    task_name = state.get("task_name", "Задача через бот")
    description = state.get("description", "Без опису")
    file_path = state.get("local_file_path")

    if not pid:
        await message.answer("ID проєкту не знайдено. Задачу створити неможливо.")
        return

    # Створення задачі через API Worksection
    ws_res = create_task_with_file(pid, task_name, description, file_path)

    if "успішно" in ws_res:
        manager_name = get_project_manager_name(pid)
        task_leader = manager_name if manager_name else None
    else:
        await message.answer("Не вдалося створити задачу у Worksection. Задачу не буде збережено у базу даних.")
        return

    async with async_session() as session:
        db_res = await save_task_to_db(
            user_id=chat_id,
            task_name=task_name,
            description=description,
            file_path=file_path,
            project_id=pid,
            task_leader=task_leader,
            session=session
        )

    await message.answer(f"{ws_res}\n{db_res}", reply_markup=ReplyKeyboardRemove())
    # Пропонуємо створити нову задачу
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add(
        KeyboardButton("Створити завдання")
    )
    await message.answer("Натисніть 'Створити завдання', якщо хочете додати ще одну задачу.", reply_markup=markup)

# Обробник створення нової задачі
@dp.message_handler(lambda message: message.text.lower() == "створити завдання")
async def handle_create_new_task(message: types.Message):
    await send_welcome(message)

# Функція для отримання користувача за TelegramChatId
async def get_user(chat_id: int, session: AsyncSession):
    result = await session.execute(
        sqlalchemy.select(User).where(User.TelegramChatId == chat_id)
    )
    return result.scalars().first()

# Запуск бота та ініціалізація бази даних
if __name__ == "__main__":
    import asyncio
    import sqlalchemy

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    executor.start_polling(dp, skip_updates=True)
