import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
import random
import string
from dotenv import load_dotenv, dotenv_values
from typing import Dict, Any, Callable, Awaitable, Union
import os
from aiogram import Bot, Dispatcher, Router, BaseMiddleware, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup,
                           KeyboardButton, InlineKeyboardMarkup,
                           InlineKeyboardButton, ReplyKeyboardRemove)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from environs import Env
from dataclasses import dataclass
from gradio_client import Client

load_dotenv()


# Конфигурация

@dataclass
class Config:
    token: str
    admin_ids: list[int]
    openai_api_key: str
    qwen_url: str


def load_config() -> Config:
    return Config(
        token=os.getenv("BOT_TOKEN"),
        admin_ids=list(map(int, os.getenv("ADMIN_IDS").split(','))),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        qwen_url=os.getenv("QWEN_URL", "Qwen/Qwen2.5")  # Добавляем URL по умолчанию
    )


# База данных
class Database:

    def __init__(self, db_file: str):
        self.connection = sqlite3.connect(db_file)
        self.cursor = self.connection.cursor()
        self._create_tables()

    def _create_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL,
                manager_id INTEGER NOT NULL
            )
        ''')
        self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    telegram_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT,
                    project_id INTEGER,
                    is_active INTEGER DEFAULT 0,
                    FOREIGN KEY (project_id) REFERENCES projects (id)
                )
            ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE NOT NULL,
                name TEXT NOT NULL,
                role TEXT,
                project_id INTEGER,
                FOREIGN KEY (project_id) REFERENCES projects (id)
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS project_roles (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects (id),
                UNIQUE(project_id, role_name)
            )
        ''')
        

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                deadline DATETIME NOT NULL,
                assigned_to INTEGER,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects (id),
                FOREIGN KEY (assigned_to) REFERENCES users (id)
            )
        ''')


        

        

        self.connection.commit()

    def add_project(self, name: str, code: str, manager_id: int) -> int:
        self.cursor.execute(
            'INSERT INTO projects (name, code, manager_id) VALUES (?, ?, ?)',
            (name, code, manager_id))
        self.connection.commit()
        return self.cursor.lastrowid

    def get_active_project(self, telegram_id: int) -> tuple:
        """Get user's currently active project"""
        self.cursor.execute('''
            SELECT p.* FROM projects p
            JOIN users u ON p.id = u.project_id
            WHERE u.telegram_id = ? AND u.is_active = 1
        ''', (telegram_id,))
        return self.cursor.fetchone()
    
    def get_active_user(self, telegram_id: int) -> tuple:
        """Get user's currently active project"""
        self.cursor.execute('''
            SELECT id FROM users
            WHERE telegram_id = ? AND is_active = 1
        ''', (telegram_id,))
        return self.cursor.fetchone()
    
    def get_active_role(self, telegram_id: int) -> tuple:
        """Get user's currently active project"""
        self.cursor.execute('''
            SELECT role FROM users
            WHERE telegram_id = ? AND is_active = 1
        ''', (telegram_id,))
        return self.cursor.fetchone()

    def add_user(self, telegram_id: int, name: str, project_id: int = None, role: str = None) -> int:
        """Add user to a project. If user exists in other projects, add new project entry."""
        try:
            # First check if user exists in this specific project
            self.cursor.execute(
                'SELECT id FROM users WHERE telegram_id = ? AND project_id = ?',
                (telegram_id, project_id)
            )
            existing_user = self.cursor.fetchone()

            if existing_user:
                # User already exists in this project, return existing ID
                return existing_user[0]

            # Add new user entry for this project
            self.cursor.execute(
                'INSERT INTO users (telegram_id, name, project_id, role) VALUES (?, ?, ?, ?)',
                (telegram_id, name, project_id, role)
            )
            self.connection.commit()
            return self.cursor.lastrowid

        except sqlite3.Error as e:
            logging.error(f"Error in add_user: {e}")
            raise

    def add_task(self,
                 project_id: int,
                 description: str,
                 deadline: datetime,
                 assigned_to: int = None) -> int:
        self.cursor.execute(
            'INSERT INTO tasks (project_id, description, deadline, assigned_to) VALUES (?, ?, ?, ?)',
            (project_id, description, deadline, assigned_to))
        self.connection.commit()
        return self.cursor.lastrowid

    def get_user(self, telegram_id: int):
        self.cursor.execute('SELECT * FROM users WHERE telegram_id = ?',
                            (telegram_id, ))
        return self.cursor.fetchone()

    def get_project(self, code: str):
        self.cursor.execute('SELECT * FROM projects WHERE code = ?', (code, ))
        return self.cursor.fetchone()

    def get_project_by_id(self, project_id: int):
        if project_id is None:
            return None
        self.cursor.execute('SELECT * FROM projects WHERE id = ?', (project_id,))
        return self.cursor.fetchone()
    def get_user_by_id(self, user_id: int):
        self.cursor.execute('SELECT * FROM users WHERE id = ?', (user_id, ))
        return self.cursor.fetchone()

    def get_task_by_id(self, task_id: int):
        self.cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id, ))
        return self.cursor.fetchone()

    def get_tasks_by_user(self, user_id: int):
        self.cursor.execute(
            '''
            SELECT * FROM tasks 
            WHERE assigned_to = ? AND status != 'completed'
            ORDER BY deadline
        ''', (user_id, ))
        return self.cursor.fetchall()

    def get_project_users(self, project_id: int):
        self.cursor.execute('SELECT * FROM users WHERE project_id = ?',
                            (project_id, ))
        return self.cursor.fetchall()

    def update_task_status(self, task_id: int, status: str):
        self.cursor.execute('UPDATE tasks SET status = ? WHERE id = ?',
                            (status, task_id))
        self.connection.commit()

    def update_user_role(self, user_id: int, role: str):
        self.cursor.execute('UPDATE users SET role = ? WHERE id = ?',
                            (role, user_id))
        self.connection.commit()


    def add_project_role(self, project_id: int, role_name: str):
        try:
            self.cursor.execute(
                'INSERT INTO project_roles (project_id, role_name) VALUES (?, ?)',
                (project_id, role_name)
            )
            self.connection.commit()
        except sqlite3.IntegrityError:
            pass  # Игнорируем дубликаты ролей

    def get_project_roles(self, project_id: int) -> list:
        self.cursor.execute(
            'SELECT role_name FROM project_roles WHERE project_id = ?',
            (project_id,)
        )
        return [role[0] for role in self.cursor.fetchall()]

    def get_user_active_project(self, telegram_id: int) -> tuple:
        """Get user's currently active project along with their role"""
        self.cursor.execute('''
            SELECT p.*, u.role, u.id as user_id
            FROM projects p
            JOIN users u ON p.id = u.project_id
            WHERE u.telegram_id = ? AND u.is_active = 1
        ''', (telegram_id,))
        return self.cursor.fetchone()
    
    def get_project_participants(self, project_id: int) -> list:
        """Get all participants of a project with their roles"""
        self.cursor.execute('''
            SELECT u.name, u.role, u.telegram_id
            FROM users u
            WHERE u.project_id = ?
            ORDER BY u.role, u.name
        ''', (project_id,))
        return self.cursor.fetchall()

    # Add to the Database class
    def get_user_projects(self, telegram_id: int) -> list:
        """Get all projects where user is a member"""
        self.cursor.execute('''
            SELECT DISTINCT p.* 
            FROM projects p
            JOIN users u ON p.id = u.project_id
            WHERE u.telegram_id = ?
        ''', (telegram_id,))
        return self.cursor.fetchall()

    # Изменить метод в классе Database
    def switch_user_project(self, telegram_id: int, project_id: int) -> bool:
        """Switch user's active project"""
        try:
            # First verify the project exists
            project = self.get_project_by_id(project_id)
            if not project:
                return False

            # Update last_active status for all user's projects
            self.cursor.execute('''
                UPDATE users 
                SET is_active = 0 
                WHERE telegram_id = ?
            ''', (telegram_id,))

            # Set the selected project as active
            self.cursor.execute('''
                UPDATE users 
                SET is_active = 1 
                WHERE telegram_id = ? AND project_id = ?
            ''', (telegram_id, project_id))

            self.connection.commit()
            return True
        except Exception as e:
            logging.error(f"Error in switch_user_project: {e}")
            return False

    def delete_project(self, project_id: int) -> bool:
        """Delete project and all related data"""
        try:
            self.cursor.execute('DELETE FROM tasks WHERE project_id = ?', (project_id,))
            self.cursor.execute('DELETE FROM project_roles WHERE project_id = ?', (project_id,))
            self.cursor.execute('DELETE FROM users WHERE project_id = ?', (project_id,))
            self.cursor.execute('DELETE FROM projects WHERE id = ?', (project_id,))
            self.connection.commit()
            return True
        except Exception:
            return False
        

    def add_feedback(self, task_id: int, feedback: str, rating: int):
        """Add feedback for completed task"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_feedback (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                feedback TEXT NOT NULL,
                rating INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES tasks (id)
            )
        ''')
        
        self.cursor.execute(
            'INSERT INTO task_feedback (task_id, feedback, rating) VALUES (?, ?, ?)',
            (task_id, feedback, rating)
        )
        self.connection.commit() 

    def add_bot_feedback(self, user_id: int, feedback: str):
        """Add feedback about bot"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_feedback (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                feedback TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.cursor.execute(
            'INSERT INTO bot_feedback (user_id, feedback) VALUES (?, ?)',
            (user_id, feedback)
        )
        self.connection.commit()    
        

# Состояния FSM
class RegistrationStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_project_code = State()
    waiting_for_role = State()


class ProjectCreationStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_roles = State()

class TaskCreationStates(StatesGroup):
    waiting_for_description = State()
    waiting_for_deadline = State()
    waiting_for_assignee = State()

# Add to the States section
class ProjectManagementStates(StatesGroup):
    waiting_for_new_project_code = State()
    confirm_project_deletion = State()


# Клавиатуры
def get_home_button() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="На главную")]],
        resize_keyboard=True,
        persistent=True  # Кнопка будет постоянно видна
    )

def get_role_keyboard() -> ReplyKeyboardMarkup:
    buttons = [[KeyboardButton(text="Программист")],
               [KeyboardButton(text="Дизайнер")],
               [KeyboardButton(text="Аналитик")]]
    return ReplyKeyboardMarkup(keyboard=buttons,
                               resize_keyboard=True,
                               one_time_keyboard=True) 


def get_main_keyboard(is_manager: bool = False) -> InlineKeyboardMarkup:
    buttons = [[
        InlineKeyboardButton(text="📋 Мои задачи", callback_data="show_tasks")
    ],
    [
        InlineKeyboardButton(text="🔄 Сменить проект", callback_data="switch_project")
    ],
    [
        InlineKeyboardButton(text="➕ Присоединиться к проекту", callback_data="join_project")
    ],
    [
        InlineKeyboardButton(text="📝 Создать новый проект", callback_data="create_new_project")
    ],
    [
        InlineKeyboardButton(text="📢 Оставить отзыв о боте", callback_data="bot_feedback")
    ]]
    
    if is_manager:
        buttons.extend([
            [InlineKeyboardButton(text="✏️ Создать задачу", callback_data="create_task")],
            [InlineKeyboardButton(text="📊 Отчет по проекту", callback_data="project_report")],
            [InlineKeyboardButton(text="👥 Участники проекта", callback_data="view_participants")],
            [InlineKeyboardButton(text="🔑 Узнать код проекта", callback_data="get_project_code")],
            [InlineKeyboardButton(text="❌ Удалить проект", callback_data="delete_project")]
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)




def get_project_code_keyboard(project_code: str) -> InlineKeyboardMarkup:
    buttons = [[
        InlineKeyboardButton(text="🔙 Вернуться", callback_data="back_to_main")
    ]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_task_inline_keyboard(task_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="✅ Отметить выполненной",
                callback_data=f"complete_task_{task_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="📋 Детали",
                callback_data=f"task_details_{task_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="⭐ Оставить отзыв",
                callback_data=f"leave_feedback_{task_id}"
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Middleware
class UserCheckMiddleware(BaseMiddleware):
    def __init__(self, database: Database):
        self.database = database
        super().__init__()

    async def __call__(
        self, 
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        # Get the current state from FSMContext
        state: FSMContext = data.get("state")
        if state:
            current_state = await state.get_state()
        else:
            current_state = None

        user = self.database.get_user(event.from_user.id)

        # Allow messages if:
        # 1. It's the /start command
        # 2. User exists
        # 3. User is in registration state
        # 4. User is in project creation state
        if (
            event.text == "/start" 
            or user is not None 
            or (current_state and current_state.startswith("RegistrationStates:"))
            or (current_state and current_state.startswith("ProjectCreationStates:"))
        ):
            data["user"] = user
            data["db"] = self.database
            return await handler(event, data)

        await event.answer(
            "Пожалуйста, сначала зарегистрируйтесь с помощью команды /start"
        )
        return


class CallbackMiddleware(BaseMiddleware):
    def __init__(self, database: Database):
        self.database = database
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, Dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: Dict[str, Any]
    ) -> Any:
        # Get the current state from FSMContext
        state: FSMContext = data.get("state")
        if state:
            current_state = await state.get_state()
        else:
            current_state = None

        user = self.database.get_user(event.from_user.id)

        # Allow callbacks if:
        # 1. User exists
        # 2. User is in registration state
        # 3. User is in project creation state
        if (
            user is not None 
            or (current_state and current_state.startswith("RegistrationStates:"))
            or (current_state and current_state.startswith("ProjectCreationStates:"))
        ):
            data["user"] = user
            data["db"] = self.database
            return await handler(event, data)

        await event.answer(
            "Пожалуйста, сначала зарегистрируйтесь с помощью команды /start",
            show_alert=True
        )
        return


# Утилиты
def generate_project_code() -> str:
    """Генерирует уникальный код проекта"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


def format_task_info(task: tuple) -> str:
    """Форматирует информацию о задаче для вывода"""
    task_id, project_id, description, deadline, assigned_to, status, created_at = task
    deadline_dt = datetime.strptime(deadline, '%Y-%m-%d %H:%M:%S')
    return (f"Задача #{task_id}\n"
            f"Описание: {description}\n"
            f"Дедлайн: {deadline_dt.strftime('%d.%m.%Y %H:%M')}\n"
            f"Статус: {status}")


async def get_best_assignee(description: str, project_roles: list, db: Database, project_id: int) -> int:
    try:
        # Инициализируем клиент Qwen
        client = Client("Qwen/Qwen2.5")

        # Формируем prompt для модели
        prompt = f"""Проанализируйте это описание задачи и определите, какая роль лучше всего подходит для ее выполнения.
Описание задачи: {description}
Роли: {', '.join(project_roles)}

Учесть следующее:
1. Технические требования
2. Необходимые навыки
3. Тип выполняемой работы

Укажите в ответ только одно название роли из списка доступных ролей, которое наилучшим образом соответствует данной задаче."""

        # Получаем ответ от модели
        result = client.predict(
            query=prompt,
            history=[],
            system="Вы являетесь ассистентом по управлению проектами. Ваша задача - проанализировать задачи проекта и определить наиболее подходящую роль для выполнения.",
            radio="72B",
            api_name="/model_chat"
        )

        # Получаем рекомендованную роль из ответа
        recommended_role = result[1][0][1]['text'].strip()

        # Находим пользователя с этой ролью в проекте
        cursor = db.cursor.execute(
            '''
            SELECT id FROM users 
            WHERE project_id = ? AND role = ?
            ORDER BY RANDOM() LIMIT 1
            ''', (project_id, recommended_role)
        )
        user = cursor.fetchone()

        if user:
            return user[0]
        else:
            # Если не нашли пользователя с рекомендованной ролью,
            # выбираем случайного пользователя из проекта
            cursor = db.cursor.execute(
                'SELECT id FROM users WHERE project_id = ? ORDER BY RANDOM() LIMIT 1',
                (project_id,)
            )
            user = cursor.fetchone()
            return user[0] if user else None

    except Exception as e:
        logging.error(f"Error in get_best_assignee: {e}")
        return None



# Обработчики
router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, db: Database):
    user = db.get_user(message.from_user.id)
    if user:
        is_manager = db.get_project_by_id(user[4])[3] == message.from_user.id
        await message.answer("С возвращением! Выберите действие:",
                             reply_markup=get_main_keyboard(is_manager))
        return

    await state.set_state(RegistrationStates.waiting_for_name)
    await message.answer("Добро пожаловать! Пожалуйста, введите ваше имя:")


@router.message(F.text == "На главную")
async def handle_home_button(message: Message, state: FSMContext, db: Database):
    user = db.get_user(message.from_user.id)
    if user:
        is_manager = db.get_project_by_id(user[4])[3] == message.from_user.id
        await message.answer("С возвращением! Выберите действие:",
                             reply_markup=get_main_keyboard(is_manager))
        return
    
    await state.set_state(RegistrationStates.waiting_for_name)
    await message.answer("Добро пожаловать! Пожалуйста, введите ваше имя:")


@router.callback_query(F.data.startswith("task_details_"))
async def cb_task_details(callback: CallbackQuery, db: Database):
    task_id = int(callback.data.split("_")[-1])
    task = db.get_task_by_id(task_id)  # Добавьте этот метод в класс Database
    if not task:
        await callback.answer("Задача не найдена", show_alert=True)
        return

    project = db.get_project_by_id(task[1])
    assignee = db.get_user_by_id(task[4])

    details = (
        f"🔍 Подробная информация о задаче #{task[0]}\n\n"
        f"📝 Описание: {task[2]}\n"
        f"⏰ Дедлайн: {datetime.strptime(task[3], '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M')}\n"
        f"📋 Проект: {project[1]}\n"
        f"👤 Исполнитель: {assignee[2]} ({assignee[3]})\n"
        f"📊 Статус: {task[5]}\n"
        f"📅 Создано: {datetime.strptime(task[6], '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M')}"
    )

    await callback.message.edit_text(
        details, reply_markup=get_task_inline_keyboard(task_id))
    await callback.answer()


@router.message(RegistrationStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(RegistrationStates.waiting_for_project_code)
    await message.answer(
        "Введите код проекта или /create для создания нового проекта:")


@router.message(Command("create"))
@router.callback_query(F.data == "create_new_project")
async def cmd_create_project(event: Union[Message, CallbackQuery], state: FSMContext):
    await state.set_state(ProjectCreationStates.waiting_for_name)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text("Введите название нового проекта:")
    else:
        await event.answer("Введите название нового проекта:")


@router.message(ProjectCreationStates.waiting_for_name)
async def process_project_name(message: Message, state: FSMContext, db: Database):
    project_name = message.text
    project_code = generate_project_code()

    # Сохраняем данные проекта в state
    await state.update_data(
        project_name=project_name,
        project_code=project_code
    )

    await state.set_state(ProjectCreationStates.waiting_for_roles)
    await message.answer(
        "Введите роли для вашего проекта через запятую.\n"
        "Например: Программист, Дизайнер, Аналитик, Тестировщик"
    )

@router.message(ProjectCreationStates.waiting_for_roles)
async def process_project_roles(message: Message, state: FSMContext, db: Database):
    # Получаем роли из сообщения
    roles = [role.strip() for role in message.text.split(',')]

    # Получаем сохраненные данные проекта
    data = await state.get_data()
    project_name = data['project_name']
    project_code = data['project_code']

    # Создаем проект
    project_id = db.add_project(project_name, project_code, message.from_user.id)

    # Добавляем роли проекта
    for role in roles:
        db.add_project_role(project_id, role)

    try:
        # Добавляем пользователя как менеджера проекта
        db.add_user(message.from_user.id, message.from_user.full_name, project_id, "Manager")
    except sqlite3.IntegrityError:
        # Игнорируем ошибку уникального ключа, так как пользователь уже существует
        pass

    await state.clear()    
    
    await message.answer(
        "Для быстрого возврата в главное меню используйте кнопку ниже",
        reply_markup=get_home_button()
    )
    await message.answer(
        f"Проект '{project_name}' успешно создан!\n"
        f"Ваша роль: Manager\n"
        f"Доступные роли в проекте: {', '.join(roles)}\n\n"
        f"Код проекта: `{project_code}`\n\n"
        "Поделитесь этим кодом с участниками команды.",
        reply_markup=get_main_keyboard(True),
        parse_mode="Markdown"
    )




@router.message(RegistrationStates.waiting_for_project_code)
async def process_project_code(message: Message, state: FSMContext, db: Database):
    if message.text == "/create":
        await state.set_state(ProjectCreationStates.waiting_for_name)
        await message.answer("Введите название нового проекта:")
        return

    project = db.get_project(message.text)
    if not project:
        await message.answer(
            "Проект не найден. Попробуйте еще раз или используйте /create для создания нового проекта."
        )
        return

    # Получаем роли проекта
    project_roles = db.get_project_roles(project[0])

    if not project_roles:
        await message.answer(
            "В проекте не определены роли. Обратитесь к менеджеру проекта."
        )
        return

    # Сохраняем данные проекта
    await state.update_data(project_id=project[0])

    # Создаем клавиатуру с ролями
    buttons = [[InlineKeyboardButton(text=role, callback_data=f"set_role_{role}")] 
               for role in project_roles]

    await state.set_state(RegistrationStates.waiting_for_role)
    await message.answer(
        "Выберите вашу роль в проекте:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith("set_role_"))
async def process_role_selection(callback: CallbackQuery, state: FSMContext, db: Database):
    selected_role = callback.data.split("set_role_")[1]
    user_data = await state.get_data()

    # Проверяем, не зарегистрирован ли уже пользователь
    existing_user = db.get_user(callback.from_user.id)
    if existing_user:
        await callback.message.edit_text(
            "Вы уже зарегистрированы в проекте.",
            reply_markup=get_main_keyboard(existing_user[3] == "Manager")
        )
        return

    # Добавляем пользователя
    db.add_user(
        callback.from_user.id,
        callback.from_user.full_name,  # Используем полное имя из Telegram
        user_data["project_id"],
        selected_role
    )

    await state.clear()
    await callback.message.edit_text(
        f"Регистрация успешно завершена!\n"
        f"Ваша роль: {selected_role}"
    )

    await callback.message.answer(
        "Выберите действие:",
        reply_markup=get_main_keyboard(False)
    )



@router.message(RegistrationStates.waiting_for_role)
async def process_role(message: Message, state: FSMContext, db: Database):
    valid_roles = ["Программист", "Дизайнер", "Аналитик"]
    if message.text not in valid_roles:
        await message.answer(
            "Пожалуйста, выберите одну из доступных ролей, используя кнопки ниже:",
            reply_markup=get_role_keyboard())
        return

    user_data = await state.get_data()
    db.add_user(message.from_user.id, user_data["name"],
                user_data["project_id"], message.text)

    await state.clear()
    await message.answer(
        "Регистрация завершена! Теперь вы можете получать и управлять задачами.",
        reply_markup=get_main_keyboard(False))


@router.callback_query(F.data == "create_task")
async def cb_create_task(callback: CallbackQuery, state: FSMContext,
                         db: Database, user: tuple):
    project = db.get_project_by_id(user[4])
    if project[3] != callback.from_user.id:
        await callback.answer(
            "Только руководитель проекта может создавать задачи.",
            show_alert=True)
        return

    await callback.message.delete()  # Удаляем предыдущее сообщение
    await state.set_state(TaskCreationStates.waiting_for_description)
    await callback.message.answer("Опишите задачу:")
    await callback.answer()


@router.message(TaskCreationStates.waiting_for_description)
async def process_task_description(message: Message, state: FSMContext):
    # Сохраняем описание задачи
    await state.update_data(description=message.text)

    # Отправляем подтверждение и запрос дедлайна
    await message.answer(f"✅ Описание задачи получено:\n{message.text}\n\n"
                         "Теперь укажите дедлайн в формате ДД.ММ.ГГГГ ЧЧ:ММ\n"
                         "Например: 31.12.2024 15:00")

    # Переходим к следующему состоянию
    await state.set_state(TaskCreationStates.waiting_for_deadline)


@router.callback_query(F.data.startswith("assign_task_"))
async def cb_assign_task(callback: CallbackQuery, state: FSMContext,
                         db: Database):
    assignee_id = int(callback.data.split("_")[-1])
    task_data = await state.get_data()

    task_id = db.add_task(task_data.get("project_id"),
                          task_data["description"], task_data["deadline"],
                          assignee_id)

    assignee = db.get_user_by_id(
        assignee_id)  # Добавьте этот метод в класс Database

    await state.clear()
    await callback.message.edit_text(
        f"Задача создана и назначена на {assignee[2]}!",
        reply_markup=get_main_keyboard(True))
    await callback.answer()


@router.message(TaskCreationStates.waiting_for_deadline)
async def process_task_deadline(message: Message, state: FSMContext, db: Database, user: tuple, bot: Bot):
    try:
        deadline = datetime.strptime(message.text, '%d.%m.%Y %H:%M')
    except ValueError:
        await message.answer(
            "Неверный формат даты. Попробуйте еще раз в формате ДД.ММ.ГГГГ ЧЧ:ММ"
        )
        return

    task_data = await state.get_data()
    project_id = user[4]

    # Получаем доступные роли проекта
    project_roles = db.get_project_roles(project_id)

    # Получаем рекомендованного исполнителя
    best_assignee = await get_best_assignee(
        task_data["description"],
        project_roles,
        db,
        project_id
    )

    if best_assignee:
        # Создаем задачу с автоматически выбранным исполнителем
        task_id = db.add_task(
            project_id,
            task_data["description"],
            deadline,
            best_assignee
        )

        # Получаем информацию о выбранном исполнителе
        assignee = db.get_user_by_id(best_assignee)

        await bot.send_message(
            assignee[1],  # telegram_id
            f"📢 Вам назначена новая задача!\n\n"
            f"Описание: {task_data['description']}\n"
            f"Дедлайн: {deadline.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Используйте меню 'Мои задачи' для просмотра деталей.",
            reply_markup=get_task_inline_keyboard(task_id)
        )


        await state.clear()
        await message.answer(
            f"✅ Задача автоматически назначена на {assignee[2]} ({assignee[3]})!\n"
            f"Описание: {task_data['description']}\n"
            f"Дедлайн: {deadline.strftime('%d.%m.%Y %H:%M')}",
            reply_markup=get_main_keyboard(True)
        )
    else:
        # Если не удалось автоматически назначить, показываем список исполнителей
        project_users = db.get_project_users(project_id)
        user_buttons = []

        for proj_user in project_users:
            user_buttons.append([
                InlineKeyboardButton(
                    text=f"{proj_user[2]} ({proj_user[3]})",
                    callback_data=f"assign_task_{proj_user[0]}"
                )
            ])

        await message.answer(
            "Не удалось автоматически назначить исполнителя. Выберите исполнителя вручную:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=user_buttons)
        )

        await state.set_state(TaskCreationStates.waiting_for_assignee)


@router.message(TaskCreationStates.waiting_for_assignee)
async def process_task_assignee(message: Message, state: FSMContext,
                                db: Database, user: tuple):
    task_data = await state.get_data()

    # Получаем ID пользователя из текста кнопки
    assignee_name = message.text.split(" (")[0]
    project_users = db.get_project_users(user[4])
    assignee = next(u for u in project_users if u[2] == assignee_name)

    task_id = db.add_task(user[4], task_data["description"],
                          task_data["deadline"], assignee[0])
    await state.clear()
    await message.answer(f"Задача создана и назначена на {assignee_name}!",
                         reply_markup=get_main_keyboard(True))


@router.message(F.text == "Мои задачи")
async def show_tasks(message: Message, db: Database, user: tuple):
    tasks = db.get_tasks_by_user(user[0])
    if not tasks:
        await message.answer("У вас пока нет активных задач.")
        return

    for task in tasks:
        await message.answer(format_task_info(task),
                             reply_markup=get_task_inline_keyboard(task[0]))


@router.callback_query(F.data == "show_tasks")
async def cb_show_tasks(callback: CallbackQuery, db: Database, user: tuple):
    await callback.message.delete()  # Удаляем предыдущее сообщение

    tasks = db.get_tasks_by_user(db.get_active_user(user[1])[0])
    if not tasks:
        await callback.message.answer(
            "У вас пока нет активных задач.",
            reply_markup=get_main_keyboard(db.get_active_role(user[1])[0] == "Manager"))
        return

    for task in tasks:
        await callback.message.answer(format_task_info(task),
                                      reply_markup=get_task_inline_keyboard(
                                          task[0]))
    await callback.answer()


@router.callback_query(F.data.startswith("complete_task_"))
async def complete_task(callback: CallbackQuery, db: Database, user: tuple):
    try:
        task_id = int(callback.data.split("_")[-1])
        db.update_task_status(task_id, "completed")

        # Проверяем, является ли пользователь менеджером
        project = db.get_project_by_id(user[4])
        is_manager = project[3] == callback.from_user.id

        # Показываем сообщение о выполнении и возвращаем в главное меню
        await callback.message.edit_text(
            f"{callback.message.text}\n✅ Задача выполнена!\nВозврат в главное меню...",
            reply_markup=None)

        # Отправляем новое сообщение с главным меню
        await callback.message.answer(
            "Выберите действие:", reply_markup=get_main_keyboard(is_manager))

        await callback.answer("Задача отмечена как выполненная!")

    except Exception as e:
        logging.error(f"Error in complete task: {e}")
        await callback.answer("Произошла ошибка при выполнении задачи.",
                              show_alert=True)


@router.callback_query(F.data == "project_report")
async def cb_project_report(callback: CallbackQuery, db: Database,
                            user: tuple):
    try:
        print(db.get_active_user(user[1]))
        project = db.get_active_project(user[1])
        if not project:
            await callback.answer("Проект не найден.", show_alert=True)
            return

        if project[3] != callback.from_user.id:
            await callback.answer(
                "Только руководитель проекта может просматривать отчеты.",
                show_alert=True)
            return

        cursor = db.cursor.execute(
            '''
            SELECT 
                t.status,
                COUNT(*) as count,
                u.name,
                u.role
            FROM tasks t
            JOIN users u ON t.assigned_to = u.id
            WHERE t.project_id = ?
            GROUP BY t.status, u.name, u.role
        ''', (user[4], ))
        stats = cursor.fetchall()

        if not stats:
            report = f"📊 Отчет по проекту '{project[1]}'\n\nПока нет данных о задачах."
        else:
            report = f"📊 Отчет по проекту '{project[1]}'\n\n"
            for stat in stats:
                status, count, user_name, role = stat
                report += f"{user_name} ({role}):\n"
                report += f"- {status}: {count} задач\n"

        try:
            await callback.message.edit_text(
                report, reply_markup=get_main_keyboard(True))
        except Exception as e:
            # Если не удалось отредактировать сообщение, отправляем новое
            await callback.message.answer(report,
                                          reply_markup=get_main_keyboard(True))

        await callback.answer()

    except Exception as e:
        logging.error(f"Error in project report: {e}")
        await callback.answer("Произошла ошибка при формировании отчета.",
                              show_alert=True)


@router.callback_query(F.data == "get_project_code")
async def cb_get_project_code(callback: CallbackQuery, db: Database, user: tuple):
    # Get active project with role
    active_project = db.get_user_active_project(callback.from_user.id)
    if not active_project:
        await callback.answer(
            "Не удалось получить информацию о текущем проекте. Попробуйте переключить проект.",
            show_alert=True
        )
        return

    project_data = active_project[:-1]  # Project data without role
    user_role = active_project[-1]  # User's role in the project

    # Check if user is manager of this specific project
    if project_data[3] != callback.from_user.id:
        await callback.answer(
            "Только руководитель проекта может просматривать код проекта.",
            show_alert=True
        )
        return

    await callback.message.edit_text(
        f"Код вашего проекта:\n\n`{project_data[2]}`\n\nПоделитесь этим кодом с участниками команды.",
        reply_markup=get_project_code_keyboard(project_data[2]),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_main")
async def cb_back_to_main(callback: CallbackQuery, db: Database, user: tuple):
    is_manager = user[3] == "Manager"
    await callback.message.edit_text(
        "Выберите действие:", reply_markup=get_main_keyboard(is_manager))
    await callback.answer()


@router.callback_query(F.data.startswith("role_"))
async def cb_process_role(callback: CallbackQuery, state: FSMContext):
    # Сообщаем пользователю, что нужно ввести роль текстом
    await callback.message.answer(
        "Пожалуйста, введите вашу роль текстом (Например: Программист, Дизайнер, Аналитик...):"
    )
    await callback.answer()

# Add these handlers to the router section
@router.callback_query(F.data == "switch_project")
async def cb_switch_project(callback: CallbackQuery, db: Database):
    # Получаем список проектов пользователя
    projects = db.get_user_projects(callback.from_user.id)

    if not projects:
        await callback.message.edit_text(
            "У вас пока нет других проектов. Вы можете присоединиться к существующему проекту или создать новый.",
            reply_markup=get_main_keyboard(False)
        )
        return

    # Создаем клавиатуру со списком проектов
    buttons = []
    for project in projects:
        buttons.append([
            InlineKeyboardButton(
                text=f"{project[1]}",
                callback_data=f"select_project_{project[0]}"
            )
        ])

    # Добавляем кнопку возврата
    buttons.append([
        InlineKeyboardButton(
            text="🔙 Вернуться",
            callback_data="back_to_main"
        )
    ])

    await callback.message.edit_text(
        "Выберите проект:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith("select_project_"))
async def cb_select_project(callback: CallbackQuery, db: Database):
    project_id = int(callback.data.split("_")[-1])
    db.switch_user_project(callback.from_user.id, project_id)
    project = db.get_project_by_id(project_id)
    is_manager = project[3] == callback.from_user.id
    
    await callback.message.edit_text(
        f"Проект изменен на: {project[1]}",
        reply_markup=get_main_keyboard(is_manager)
    )

@router.callback_query(F.data == "join_project")
async def cb_join_project(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProjectManagementStates.waiting_for_new_project_code)
    await callback.message.edit_text(
        "Введите код проекта, к которому хотите присоединиться:\n\n"
        "Для отмены нажмите кнопку 'На главную'"
    )


@router.message(ProjectManagementStates.waiting_for_new_project_code)
async def process_new_project_code(message: Message, state: FSMContext, db: Database):
    project = db.get_project(message.text)
    if not project:
        await message.answer(
            "Проект не найден. Попробуйте еще раз или нажмите 'На главную' для возврата в меню."
        )
        return

    # Проверяем, не состоит ли пользователь уже в этом проекте
    user_projects = db.get_user_projects(message.from_user.id)
    if any(p[0] == project[0] for p in user_projects):
        await message.answer(
            "Вы уже состоите в этом проекте. Используйте функцию 'Сменить проект' для переключения между проектами.",
            reply_markup=get_main_keyboard(False)
        )
        await state.clear()
        return

    # Получаем роли проекта
    project_roles = db.get_project_roles(project[0])

    if not project_roles:
        await message.answer(
            "В проекте не определены роли. Обратитесь к менеджеру проекта."
        )
        await state.clear()
        return

    # Создаем клавиатуру с ролями
    buttons = [
        [InlineKeyboardButton(text=role, callback_data=f"join_role_{project[0]}_{role}")]
        for role in project_roles
    ]

    # Добавляем кнопку отмены
    buttons.append([
        InlineKeyboardButton(text="🔙 Отмена", callback_data="back_to_main")
    ])

    await message.answer(
        f"Выберите вашу роль в проекте '{project[1]}':",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("join_role_"))
async def process_join_role(callback: CallbackQuery, state: FSMContext, db: Database):
    # Получаем project_id и role из callback_data
    _, _, project_id, role = callback.data.split("_")
    project_id = int(project_id)

    try:
        # Добавляем пользователя в новый проект
        user_id = db.add_user(
            callback.from_user.id,
            callback.from_user.full_name,
            project_id,
            role
        )

        # Переключаем пользователя на новый проект
        db.switch_user_project(callback.from_user.id, project_id)

        # Получаем информацию о проекте
        project = db.get_project_by_id(project_id)
        is_manager = project[3] == callback.from_user.id

        await state.clear()

        await callback.message.edit_text(
            f"✅ Вы успешно присоединились к проекту '{project[1]}'\n"
            f"Ваша роль: {role}"
        )

        await callback.message.answer(
            "Выберите действие:",
            reply_markup=get_main_keyboard(is_manager)
        )

    except Exception as e:
        logging.error(f"Error in process_join_role: {e}")
        await callback.message.edit_text(
            "Произошла ошибка при присоединении к проекту. Попробуйте еще раз.",
            reply_markup=get_main_keyboard(False)
        )
        await state.clear()




@router.callback_query(F.data == "delete_project")
async def cb_delete_project(callback: CallbackQuery, state: FSMContext, db: Database):
    project = db.get_project_by_id(db.get_user(callback.from_user.id)[4])
    if project[3] != callback.from_user.id:
        await callback.answer("Только владелец проекта может удалить его", show_alert=True)
        return
    
    await state.set_state(ProjectManagementStates.confirm_project_deletion)
    await callback.message.edit_text(
        f"Вы уверены, что хотите удалить проект '{project[1]}'?\n"
        "⚠️ Это действие нельзя отменить!\n\n"
        "Для подтверждения напишите название проекта:"
    )

@router.message(ProjectManagementStates.confirm_project_deletion)
async def confirm_project_deletion(message: Message, state: FSMContext, db: Database):
    project = db.get_project_by_id(db.get_user(message.from_user.id)[4])
    if message.text != project[1]:
        await message.answer("Название проекта введено неверно. Операция отменена.")
        await state.clear()
        return
    
    if db.delete_project(project[0]):
        await message.answer("Проект успешно удален.")
    else:
        await message.answer("Произошла ошибка при удалении проекта.")
    
    await state.clear()


@router.callback_query(F.data == "create_new_project")
async def cb_create_new_project(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProjectCreationStates.waiting_for_name)
    await callback.message.edit_text("Введите название нового проекта:")



@router.callback_query(F.data == "view_participants")
async def cb_view_participants(callback: CallbackQuery, db: Database):
    # Get active project with role
    active_project = db.get_user_active_project(callback.from_user.id)
    if not active_project:
        await callback.answer(
            "Не удалось получить информацию о текущем проекте.",
            show_alert=True
        )
        return
    
    project_data = active_project[:-2]  # Project data without role and user_id
    user_role = active_project[-2]  # User's role
    
    # Check if user is manager of this project
    if project_data[3] != callback.from_user.id:
        await callback.answer(
            "Только руководитель проекта может просматривать список участников.",
            show_alert=True
        )
        return
    
    # Get project participants
    participants = db.get_project_participants(project_data[0])
    
    if not participants:
        message_text = f"В проекте '{project_data[1]}' пока нет участников."
    else:
        message_text = f"👥 Участники проекта '{project_data[1]}':\n\n"
        current_role = None
        
        for name, role, _ in participants:
            if role != current_role:
                message_text += f"\n{role}:\n"
                current_role = role
            message_text += f"- {name}\n"
    
    # Add back button
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Вернуться", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(
        message_text,
        reply_markup=keyboard
    )







class FeedbackStates(StatesGroup):
    waiting_for_bot_feedback = State()


@router.callback_query(F.data == "bot_feedback")
async def cb_bot_feedback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FeedbackStates.waiting_for_bot_feedback)
    await callback.message.edit_text(
        "Пожалуйста, поделитесь вашим мнением о работе бота.\n"
        "Что можно улучшить? Какие функции добавить?\n"
        "Ваш отзыв поможет сделать бота лучше!"
    )

@router.message(FeedbackStates.waiting_for_bot_feedback)
async def process_bot_feedback(message: Message, state: FSMContext, db: Database):
    db.add_bot_feedback(message.from_user.id, message.text)
    await state.clear()
    await message.answer(
        "Спасибо за ваш отзыв! Мы учтем его для улучшения работы бота.",
        reply_markup=get_main_keyboard(
            db.get_project_by_id(db.get_user(message.from_user.id)[4])[3] == message.from_user.id
        )
    )

@router.message()
async def handle_unknown(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        # Если пользователь в процессе регистрации, показываем соответствующее сообщение
        if current_state == RegistrationStates.waiting_for_name.state:
            await message.answer("Пожалуйста, введите ваше имя.")
        elif current_state == RegistrationStates.waiting_for_project_code.state:
            await message.answer(
                "Пожалуйста, введите код проекта или используйте /create для создания нового проекта."
            )
        elif current_state == RegistrationStates.waiting_for_role.state:
            await message.answer(
                "Пожалуйста, выберите роль, используя кнопки ниже:",
                reply_markup=get_role_keyboard())
    else:
        # Если пользователь не в процессе регистрации
        await message.answer(
            "Извините, я не понимаю эту команду. Используйте доступные кнопки меню или /start для начала работы."
        )


# Планировщик задач
class TaskScheduler:

    def __init__(self, bot: Bot, db: Database):
        self.scheduler = AsyncIOScheduler()
        self.bot = bot
        self.db = db

        # Добавляем задачу на проверку дедлайнов каждый час
        self.scheduler.add_job(self.check_deadlines, 'interval', hours=1)

    async def check_deadlines(self):
        """Проверяет приближающиеся дедлайны и отправляет уведомления"""
        now = datetime.now()
        deadline_threshold = now + timedelta(hours=24)

        # Получаем задачи с приближающимися дедлайнами
        cursor = self.db.cursor.execute(
            '''
            SELECT 
                t.id,
                t.description,
                t.deadline,
                u.telegram_id,
                p.manager_id
            FROM tasks t
            JOIN users u ON t.assigned_to = u.id
            JOIN projects p ON t.project_id = p.id
            WHERE t.status != 'completed'
            AND t.deadline <= ?
            AND t.deadline > ?
        ''', (deadline_threshold.strftime('%Y-%m-%d %H:%M:%S'),
              now.strftime('%Y-%m-%d %H:%M:%S')))

        upcoming_tasks = cursor.fetchall()

        for task in upcoming_tasks:
            task_id, description, deadline, user_id, manager_id = task
            deadline_dt = datetime.strptime(deadline, '%Y-%m-%d %H:%M:%S')
            hours_left = int((deadline_dt - now).total_seconds() / 3600)

            # Уведомление исполнителю
            await self.bot.send_message(
                user_id, f"⚠️ Напоминание!\n"
                f"До дедлайна задачи #{task_id} осталось {hours_left} часов!\n"
                f"Описание: {description}")

            # Если осталось менее 2 часов, уведомляем менеджера
            if hours_left <= 2:
                await self.bot.send_message(
                    manager_id, f"🚨 Внимание!\n"
                    f"Задача #{task_id} может быть не выполнена вовремя!\n"
                    f"До дедлайна осталось {hours_left} часов.\n"
                    f"Описание: {description}")

    def start(self):
        """Запускает планировщик"""
        self.scheduler.start()


async def main():
    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    # Загрузка конфигурации
    config = load_config()

    # Инициализация бота и диспетчера
    bot = Bot(token=config.token)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Инициализация базы данных
    db = Database("project_bot.db")

    # Создаем middleware с передачей базы данных
    user_middleware = UserCheckMiddleware(db)
    callback_middleware = CallbackMiddleware(db)

    # Регистрируем middleware
    dp.message.middleware(user_middleware)
    dp.callback_query.middleware(callback_middleware)

    # Регистрация роутера
    dp.include_router(router)

    # Инициализация и запуск планировщика задач
    scheduler = TaskScheduler(bot, db)
    scheduler.start()

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
