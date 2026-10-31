"""
Telegram-бот для агента изучения ИИ.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README репозиториев)
    - infostart.ru (статьи и публикации по 1С)

Поддерживаемые провайдеры: polza (основной), ollama (резерв).
Название модели вводится пользователем.

Команды бота:
    /start — Приветствие и инструкция
    /help  — Справка по использованию
    /model — Выбор провайдера и модели
    /users — Пользователи и заявки на доступ (только владелец)
    <URL>  — Отправить ссылку → получить конспект

Доступ: бот приватный. Владелец (OWNER_ID) пущен всегда, остальные
запрашивают доступ кнопкой и ждут одобрения владельца.

Example:
    python bot.py
"""

import os

import telebot
from telebot import types
from dotenv import load_dotenv

import atexit
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

from pipeline import ensure_directories, process_article, save_article_to_db
from summarizer import (
    DEFAULT_MODEL, DEFAULT_PROVIDER, check_model_availability, check_providers_status,
    DEFAULT_MD_MODEL, DEFAULT_MD_PROVIDER,
    generate_idea_md, revise_idea_md,
)
import access
from access import Gate, DecideOutcome, RequestOutcome
from database import (
    init_db,
    article_exists,
    get_cached_summary,
    get_article_by_id,
    create_idea,
    get_user_ideas,
    get_idea_by_id,
    update_idea,
    delete_idea,
    delete_article,
    link_article_to_idea,
    unlink_article_from_idea,
    get_articles_by_idea,
    get_user_articles,
    get_ideas_by_article,
    get_idea_md,
    update_idea_md,
    list_users,
)

load_dotenv(override=True)

__all__ = ['bot', 'main']

# Конфигурация
TELEGRAM_TOKEN: str | None = os.getenv('TELEGRAM_BOT_TOKEN')

# Константы Telegram
TELEGRAM_MAX_MESSAGE_LENGTH = 4096  # Максимальная длина одного сообщения в Telegram
MESSAGE_CHUNK_SIZE = 4000  # Размер части при разбивке длинных сообщений (оставляем запас)

# Словарь поддерживаемых источников: domain → имя парсера
SUPPORTED_SOURCES: dict[str, str] = {
    'habr.com': 'habr',
    'github.com': 'github',
    'infostart.ru': 'infostart',
}

# Формат: {user_id: {'provider': 'ollama', 'model': 'gemma3:12b'}}
user_models: dict[int, dict[str, str]] = {}
user_md_models: dict[int, dict[str, str]] = {}

# Промежуточное состояние: провайдер выбран, ждём ввод названия модели
# Формат: {user_id: {'purpose': 'summary'|'md', 'provider': str}}
pending_model_selection: dict[int, dict[str, str]] = {}

# Примеры моделей для каждого провайдера (подсказка при вводе)
PROVIDER_EXAMPLES: dict[str, str] = {
    'polza': 'deepseek/deepseek-v4.1-flash-1017, google/gemini-3.8-flash, anthropic/claude-sonnet-5.5',
    'ollama': 'gemma3:12b, llama3:8b, qwen3:8b',
}

PROVIDER_DISPLAY: dict[str, str] = {
    'polza': 'Polza.ai',
    'ollama': 'Ollama (локальная, резерв)',
}

# Сообщения
MSG_START: str = """👋 Привет!

Я пока не знаю что я такое и зачем.
Но мне дали возможность парсить страницы из источников и пересказывать их содержание.

🔧 Как использовать:
1. Отправь ссылку
2. Подожди 30-60 секунд
3. Я расскажу о чем ссылка.

💡 Идеи:
• /new_idea — создать новую идею
• /ideas — посмотреть свои идеи

📌 Поддерживаемые источники:
• habr.com — технические статьи
• github.com — README репозиториев
• infostart.ru — база знаний

Сменить модель: /model

💡 Попробуй отправить ссылку прямо сейчас!

/help — справка по командам"""
MSG_HELP = """Команды:
/start - начало
/model - выбор модели
/new_idea - создать идею
/ideas - посмотреть идеи
/articles - все статьи

Поддерживаемые источники: habr.com, github.com, infostart.ru"""
MSG_HELP_OWNER = MSG_HELP + "\n\nВладельцу:\n/users - пользователи и заявки на доступ"
MSG_PROCESSING = "Обрабатываю..."
MSG_ERROR = "Ошибка: {error}"
MSG_UNSUPPORTED = "Я такие ссылки пока не понимаю. Поддерживаемые источники: habr.com, github.com, infostart.ru"
MSG_MODEL_UNAVAILABLE = "Модель {model} ({provider}) недоступна: {error}\n\nВыбери другую модель: /model"
MSG_UNKNOWN = "Без ссылки работать бессмысленно. /help для справки."
MSG_CURRENT_MODELS = ("Текущие настройки:\n"
                      "Конспекты: {summary_model} ({summary_provider})\n"
                      ".md описания: {md_model} ({md_provider})")
MSG_PROVIDER_SELECT = "Выбери провайдера для {purpose}:"
MSG_MODEL_INPUT = "Провайдер: {provider}\n\nВведи название модели (например: {example}):"
MSG_MODEL_CHECKING = "Проверяю модель {model} ({provider})..."
MSG_MODEL_SET = "Модель установлена: {model} ({provider})"
MSG_MODEL_CHECK_FAILED = "Модель {model} ({provider}) недоступна: {error}\n\nПопробуй другое название или /model"
MSG_DUPLICATE_FOUND = "📦 Эта статья уже обработана.\n\nЧто сделать?"

# Сообщения для идей
MSG_IDEA_ASK_NAME = "Пришли название своей идеи."
MSG_IDEA_ASK_DESCRIPTION = "Теперь пришли описание для этой идеи (можно использовать Markdown)."
MSG_IDEA_CREATED = "Идея '{name}' успешно создана!"
MSG_IDEAS_EMPTY = "У тебя пока нет идей."
MSG_IDEAS_TITLE = "Твои идеи:"
MSG_IDEA_NOT_FOUND = "Идея не найдена или доступ к ней запрещён."
MSG_IDEA_CONFIRM_DELETE = "Ты уверен, что хочешь удалить эту идею? Нажми 'Да' для подтверждения."
MSG_IDEA_DELETED = "Идея удалена."
MSG_IDEA_UPDATED = "Идея '{name}' успешно обновлена!"

# Сообщения для привязки статей к идеям
MSG_LINK_SELECT_IDEAS = "Выбери идеи для привязки статьи (можно несколько):"
MSG_LINK_DONE = "Статья привязана к идеям: {ideas}"
MSG_LINK_SKIPPED = "Статья не привязана ни к одной идее."
MSG_IDEA_ARTICLES_TITLE = "Статьи, привязанные к идее «{name}»:"
MSG_IDEA_NO_ARTICLES = "К этой идее пока не привязано ни одной статьи."
MSG_ARTICLE_UNLINKED = "Статья отвязана от идеи."
MSG_ARTICLES_EMPTY = "В базе нет статей."
MSG_ARTICLES_TITLE = "Все статьи ({count}):"
MSG_REASSIGN_SELECT = "Выбери идеи для переноса статьи (можно несколько):"
MSG_REASSIGN_DONE = "Статья перенесена."
MSG_REASSIGN_CANCELLED = "Перенос отменен."
MSG_REASSIGN_NO_IDEAS = "Нет других идей для переноса."
MSG_ASSIGN_SELECT = "Выбери идеи для привязки статьи (можно несколько):"
MSG_ASSIGN_DONE = "Статья привязана."
MSG_ASSIGN_CANCELLED = "Привязка отменена."
MSG_ASSIGN_NO_IDEAS = "Нет идей. Создай идею: /new_idea"
MSG_GENERATE_MD = "Генерирую описание идеи..."
MSG_MD_READY = ("Описание готово. Варианты:\n1. Утвердить\n"
                "2. Отправить замечания текстом\n"
                "3. Отправить свой вариант целиком (начиная с #)")
MSG_MD_APPROVED = "Описание сохранено."
MSG_MD_REVISING = "Переделываю с учетом замечаний..."

# Сообщения доступа
MSG_ACCESS_OFFER = (
    "👋 Это личный бот: он делает конспекты технических статей "
    "(habr.com, github.com, infostart.ru) и собирает их в базу знаний.\n\n"
    "Бот приватный — доступ выдаёт владелец. Если он вам нужен, нажмите кнопку ниже."
)
MSG_ACCESS_REQUEST_BTN = "📝 Запросить доступ"
MSG_ACCESS_REQUESTED = "Заявка отправлена. Вам придёт сообщение, когда владелец её рассмотрит."
MSG_ACCESS_PENDING = "Заявка на рассмотрении. Вам придёт сообщение, когда владелец примет решение."
MSG_ACCESS_ALREADY = "Доступ уже открыт, отправьте /start"
MSG_ACCESS_DENIED_CALLBACK = "Нет доступа."
MSG_ACCESS_GRANTED = "✅ Доступ открыт, отправьте /start"
MSG_ACCESS_REJECTED = "В доступе отказано."
MSG_ACCESS_OWNER_REQUEST = "Запрос доступа: {user}"
MSG_ACCESS_OWNER_ONLY = "Команда только для владельца."
MSG_ACCESS_USER_NOT_FOUND = "Пользователь не найден"
BTN_ACCESS_APPROVE = "✅ Одобрить"
BTN_ACCESS_REJECT = "❌ Отклонить"
BTN_ACCESS_REVOKE = "🚫 Отозвать доступ"
MSG_ACCESS_NOTIFY_FAILED = "Не удалось уведомить {user}: {error}"
MSG_USERS_EMPTY = "Заявок на доступ не было."
MSG_USERS_TITLE = "Пользователи ({count}): ожидают {pending}, одобрены {approved}, отклонены {rejected}."
USER_STATUS_LABEL: dict[str, str] = {
    'pending': '⏳ ждёт решения',
    'approved': '✅ одобрен',
    'rejected': '❌ отклонён',
}


# Проверяем наличие токена перед запуском
if not TELEGRAM_TOKEN:
    logger.critical('Не найден TELEGRAM_BOT_TOKEN в файле .env')
    logger.critical('Создайте файл .env и добавьте строку: TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather')
    exit(1)

# Владелец бота: без него бот не стартует (fail closed — иначе бот открыт всем)
try:
    OWNER_ID: int = access.parse_owner_id(os.getenv('OWNER_ID'))
except ValueError as owner_error:
    logger.critical('%s. Укажите в .env строку OWNER_ID=<ваш числовой Telegram ID>', owner_error)
    logger.critical('Свой ID можно узнать у @userinfobot')
    exit(1)

# Команды в меню Telegram; владельцу дополнительно /users
BOT_COMMANDS: list[tuple[str, str]] = [
    ('start', 'начало работы'),
    ('help', 'справка'),
    ('model', 'выбор модели'),
    ('new_idea', 'создать идею'),
    ('ideas', 'мои идеи'),
    ('articles', 'все статьи'),
]
OWNER_COMMANDS: list[tuple[str, str]] = BOT_COMMANDS + [('users', 'пользователи и заявки')]


class AccessControlledBot(telebot.TeleBot):
    """
    TeleBot с проверкой доступа до любой диспетчеризации.

    Проверка стоит в process_new_updates, а не в middleware: в pyTelegramBotAPI
    next-step-обработчики (register_next_step_handler) вызываются раньше
    middleware и обошли бы её. Апдейты прочих типов отбрасываются.
    """

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.owner_menu_set = False

    def process_new_updates(self, updates: list[types.Update]) -> None:
        # Offset двигает только super(): без этого отброшенные апдейты приходили бы
        # при каждом опросе снова (и незнакомцу повторно слалось бы приглашение)
        if updates:
            self.last_update_id = max(self.last_update_id, max(u.update_id for u in updates))
        allowed = [update for update in updates if _admit_update(update)]
        if allowed:
            super().process_new_updates(allowed)


def _admit_update(update: types.Update) -> bool:
    """Решает судьбу апдейта: True — пустить к обработчикам. Любая ошибка → не пускать."""
    try:
        if update.message is not None:
            message = update.message
            user_id = message.from_user.id if message.from_user else None
            decision = access.gate(user_id, message.chat.type, None, OWNER_ID)
            if decision == Gate.OFFER:
                _send_access_offer(message.chat.id)
            elif decision == Gate.PENDING:
                bot.send_message(message.chat.id, MSG_ACCESS_PENDING)
        elif update.callback_query is not None:
            call = update.callback_query
            user_id = call.from_user.id
            if not call.data:
                # Обработчики ждут строку в call.data (game-кнопки и т.п. не используем)
                return False
            chat_type = call.message.chat.type if call.message else None
            decision = access.gate(user_id, chat_type, call.data, OWNER_ID)
            if decision == Gate.DENY_CALLBACK:
                bot.answer_callback_query(call.id, MSG_ACCESS_DENIED_CALLBACK, show_alert=True)
        else:
            # edited_message, my_chat_member, inline и пр. — обработчиков нет, не пускаем
            return False
    except Exception:
        logger.exception('Ошибка проверки доступа для update_id=%s', update.update_id)
        return False

    if decision != Gate.PASS:
        # IGNORE — на debug: отклонённый может флудить, лог не должен расти от этого
        level = logging.DEBUG if decision == Gate.IGNORE else logging.INFO
        logger.log(level, 'Доступ закрыт: user_id=%s, решение=%s', user_id, decision.value)
        return False
    if user_id == OWNER_ID:
        _ensure_owner_menu()
    return True


def _ensure_owner_menu() -> None:
    """
    Меню команд владельца (с /users) ставится при его первом апдейте:
    до того как владелец написал боту, Telegram отвечает «chat not found».
    """
    if bot.owner_menu_set:
        return
    try:
        bot.set_my_commands(
            [types.BotCommand(name, desc) for name, desc in OWNER_COMMANDS],
            scope=types.BotCommandScopeChat(OWNER_ID),
        )
        bot.owner_menu_set = True
    except Exception as e:
        logger.warning('Не удалось установить меню владельца: %s', e)


def _send_access_offer(chat_id: int) -> None:
    """Объяснение для незнакомца + кнопка «Запросить доступ»."""
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton(text=MSG_ACCESS_REQUEST_BTN, callback_data=access.REQUEST_CALLBACK))
    bot.send_message(chat_id, MSG_ACCESS_OFFER, reply_markup=keyboard)


class _RedactTokenFilter(logging.Filter):
    """Вырезает токен бота из логов: тексты сетевых ошибок содержат URL вида /bot<TOKEN>/..."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._token in message:
            record.msg = message.replace(self._token, '<TOKEN>')
            record.args = None
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RedactTokenFilter(TELEGRAM_TOKEN))


def _error_text(error: Exception) -> str:
    """Безопасный текст ошибки Telegram для пользователя/владельца — без URL с токеном."""
    if isinstance(error, telebot.apihelper.ApiTelegramException):
        return error.description
    return type(error).__name__


# Создаем бота (токен точно существует)
bot = AccessControlledBot(TELEGRAM_TOKEN)


def extract_url(text: str | None) -> str | None:
    """Извлекает URL из текста (пользователь может отправить текст + ссылку)."""
    if not text:
        return None
    for word in text.split():
        word = word.strip()
        if word.startswith('http://') or word.startswith('https://'):
            return word
    return None


def is_supported_url(url: str) -> bool:
    """
    Проверяет, поддерживается ли источник по URL.

    Проверяет наличие доменов из SUPPORTED_SOURCES в URL.

    Args:
        url: URL для проверки

    Returns:
        True если источник поддерживается, иначе False
    """
    return any(source in url for source in SUPPORTED_SOURCES)


def get_user_model(user_id: int) -> tuple[str, str]:
    """
    Возвращает (модель, провайдер) для генерации конспектов.

    Args:
        user_id: Telegram ID пользователя

    Returns:
        Кортеж (model, provider)
    """
    cfg = user_models.get(user_id)
    if cfg:
        return cfg['model'], cfg['provider']
    return DEFAULT_MODEL, DEFAULT_PROVIDER


def get_user_md_model(user_id: int) -> tuple[str, str]:
    """Возвращает (модель, провайдер) для генерации .md."""
    cfg = user_md_models.get(user_id)
    if cfg:
        return cfg['model'], cfg['provider']
    return DEFAULT_MD_MODEL, DEFAULT_MD_PROVIDER


def create_cache_keyboard(url: str) -> types.InlineKeyboardMarkup:
    """
    Создает inline-клавиатуру для выбора действия при дубликате.

    Args:
        url: URL статьи для передачи в callback_data

    Returns:
        InlineKeyboardMarkup с кнопками выбора действия
    """
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    # Кодируем URL для callback_data (ограничение 64 байта)
    # Используем хеш URL для идентификации
    url_hash = str(hash(url))[-10:]

    show_btn = types.InlineKeyboardButton(
        text='📦 Показать сохранённый',
        callback_data=f'cache:show:{url_hash}',
    )
    regen_btn = types.InlineKeyboardButton(
        text='🔄 Сгенерировать заново',
        callback_data=f'cache:regen:{url_hash}',
    )
    keyboard.add(show_btn, regen_btn)
    return keyboard


# Временное хранилище URL по хешу (для callback)
pending_cache_urls: dict[str, str] = {}

# Хранилище состояния multiselect для привязки статей к идеям
# Формат: {user_id: {'article_data': dict, 'summary': str, 'model': str, 'selected_ideas': set[int]}}
pending_article_links: dict[int, dict] = {}

# Хранилище состояния перепривязки статей
pending_reassign: dict[int, dict] = {}

# Хранилище состояния привязки из общего списка статей
pending_assign_list: dict[int, dict] = {}

# Хранилище состояния генерации .md идей
pending_md_generation: dict[int, dict] = {}


def create_provider_keyboard(purpose: str) -> types.InlineKeyboardMarkup:
    """
    Создаёт inline-клавиатуру для выбора провайдера.

    Args:
        purpose: 'summary' для конспектов, 'md' для генерации .md

    Returns:
        InlineKeyboardMarkup с кнопками выбора провайдера
    """
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for provider_id, display_name in PROVIDER_DISPLAY.items():
        button = types.InlineKeyboardButton(
            text=display_name,
            callback_data=f'provider:{purpose}:{provider_id}',
        )
        keyboard.add(button)
    return keyboard


def create_link_ideas_keyboard(
    ideas: list[dict],
    selected_ids: set[int],
) -> types.InlineKeyboardMarkup:
    """
    Создаёт inline-клавиатуру для multiselect привязки к идеям.

    Args:
        ideas: список идей пользователя
        selected_ids: множество ID уже выбранных идей

    Returns:
        InlineKeyboardMarkup с toggle-кнопками идей
    """
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for idea in ideas:
        idea_id = idea['id']
        is_selected = idea_id in selected_ids
        prefix = "✅ " if is_selected else "⬜ "
        button = types.InlineKeyboardButton(
            text=f"{prefix}{idea['name'][:40]}",
            callback_data=f"toggle_link:{idea_id}",
        )
        keyboard.add(button)

    # Кнопки "Готово" и "Не привязывать"
    done_btn = types.InlineKeyboardButton(
        text="✔️ Готово",
        callback_data="link_done",
    )
    skip_btn = types.InlineKeyboardButton(
        text="❌ Не привязывать",
        callback_data="link_skip",
    )
    keyboard.row(done_btn, skip_btn)

    return keyboard


def create_assign_list_keyboard(
    ideas: list[dict],
    selected_ids: set[int],
) -> types.InlineKeyboardMarkup:
    """Клавиатура multiselect для привязки статьи из общего списка."""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for idea in ideas:
        prefix = "V " if idea['id'] in selected_ids else "_ "
        keyboard.add(types.InlineKeyboardButton(
            text=f"{prefix}{idea['name'][:40]}",
            callback_data=f"toggle_assign_list:{idea['id']}",
        ))
    done_btn = types.InlineKeyboardButton(text="Готово", callback_data="assign_list_done")
    cancel_btn = types.InlineKeyboardButton(text="Отмена", callback_data="assign_list_cancel")
    keyboard.row(done_btn, cancel_btn)
    return keyboard


def create_reassign_keyboard(ideas: list[dict], selected_ids: set[int]) -> types.InlineKeyboardMarkup:
    """Клавиатура multiselect для перепривязки."""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for idea in ideas:
        prefix = "V " if idea['id'] in selected_ids else "_ "
        keyboard.add(types.InlineKeyboardButton(
            text=f"{prefix}{idea['name'][:40]}",
            callback_data=f"toggle_reassign:{idea['id']}",
        ))
    done_btn = types.InlineKeyboardButton(text="Готово", callback_data="reassign_done")
    cancel_btn = types.InlineKeyboardButton(text="Отмена", callback_data="reassign_cancel")
    keyboard.row(done_btn, cancel_btn)
    return keyboard


def _auto_generate_md(
    chat_id: int,
    user_id: int,
    idea_id: int,
    idea_name: str,
    idea_description: str | None,
) -> None:
    """Автоматически генерирует .md после создания/редактирования идеи."""
    if not idea_description:
        return
    md_model, md_provider = get_user_md_model(user_id)
    # Проверяем доступность модели для .md
    is_available, error_message = check_model_availability(md_model, md_provider)
    if not is_available:
        logger.warning(
            'Модель .md %s (%s) недоступна для %s: %s', md_model, md_provider, user_id, error_message,
        )
        bot.send_message(
            chat_id,
            MSG_MODEL_UNAVAILABLE.format(model=md_model, provider=md_provider, error=error_message),
        )
        return
    bot.send_message(chat_id, MSG_GENERATE_MD)
    bot.send_chat_action(chat_id, 'typing')
    logger.info(
        'Начинаю генерацию .md: idea_id=%d, model=%s, provider=%s, user_id=%s',
        idea_id, md_model, md_provider, user_id,
    )
    try:
        md_text = generate_idea_md(idea_name, idea_description, md_model, md_provider)
    except Exception as e:
        logger.error('Ошибка генерации .md для idea_id=%d: %s', idea_id, e)
        bot.send_message(chat_id, MSG_ERROR.format(error=str(e)))
        return
    pending_md_generation[user_id] = {'idea_id': idea_id, 'draft_md': md_text}
    send_long_message(chat_id, md_text)
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        types.InlineKeyboardButton(
            text="Утвердить", callback_data=f"approve_md:{idea_id}",
        ),
        types.InlineKeyboardButton(
            text="Замечания", callback_data=f"revise_md:{idea_id}",
        ),
    )
    bot.send_message(chat_id, MSG_MD_READY, reply_markup=keyboard)


def _offer_link_to_ideas(chat_id: int, user_id: int, article_id: int) -> None:
    """
    Предлагает пользователю привязать статью к идеям.

    Показывает multiselect клавиатуру если у пользователя есть идеи.
    """
    ideas = get_user_ideas(user_id)

    if not ideas:
        # У пользователя нет идей — не показываем выбор
        return

    # Инициализируем состояние multiselect
    pending_article_links[user_id] = {
        'article_id': article_id,
        'selected_ideas': set(),
    }

    keyboard = create_link_ideas_keyboard(ideas, set())
    bot.send_message(chat_id, MSG_LINK_SELECT_IDEAS, reply_markup=keyboard)


def send_long_message(chat_id: int, text: str, chunk_size: int = MESSAGE_CHUNK_SIZE) -> None:
    """
    Отправляет длинное сообщение частями.

    Разбивает текст по параграфам (двойной перенос строки).
    Каждая часть не превышает chunk_size символов.

    Args:
        chat_id: ID чата для отправки
        text: Текст сообщения
        chunk_size: Максимальный размер одной части (по умолчанию 4000)
    """
    # Простой случай - весь текст влезает в одно сообщение
    if len(text) <= chunk_size:
        bot.send_message(chat_id, text)
        return

    # Разбиваем на параграфы
    paragraphs = text.split('\n\n')
    current_chunk = ''
    PARAGRAPH_SEPARATOR = '\n\n'

    for paragraph in paragraphs:
        # Проверяем, поместится ли параграф в текущую часть
        separator_length = len(PARAGRAPH_SEPARATOR) if current_chunk else 0
        would_fit = len(current_chunk) + separator_length + len(paragraph) <= chunk_size

        if would_fit:
            # Добавляем параграф к текущей части
            if current_chunk:
                current_chunk += PARAGRAPH_SEPARATOR + paragraph
            else:
                current_chunk = paragraph
        else:
            # Текущая часть заполнена - отправляем и начинаем новую
            if current_chunk:
                bot.send_message(chat_id, current_chunk)
            current_chunk = paragraph

    # Отправляем последнюю часть
    if current_chunk:
        bot.send_message(chat_id, current_chunk)


def create_main_keyboard(is_owner: bool = False) -> types.ReplyKeyboardMarkup:
    """
    Создаёт постоянную клавиатуру с основными командами.

    Args:
        is_owner: добавить /users для владельца

    Returns:
        ReplyKeyboardMarkup с кнопками команд
    """
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row('/ideas', '/new_idea', '/articles')
    if is_owner:
        keyboard.row('/model', '/users', '/help')
    else:
        keyboard.row('/model', '/help')
    return keyboard


# Обработчики команд и сообщений
@bot.message_handler(commands=['start'])
def handle_start(message: telebot.types.Message) -> None:
    """Обрабатывает команду /start - приветствие и инструкции."""
    keyboard = create_main_keyboard(is_owner=message.from_user.id == OWNER_ID)
    bot.send_message(message.chat.id, MSG_START, reply_markup=keyboard)


@bot.message_handler(commands=['help'])
def handle_help(message: telebot.types.Message) -> None:
    """Обрабатывает команду /help - справка по использованию бота."""
    bot.reply_to(message, MSG_HELP_OWNER if message.from_user.id == OWNER_ID else MSG_HELP)


# ========================
# Доступ: заявки и решения владельца
# ========================


def _decision_keyboard(user_id: int, status: str) -> types.InlineKeyboardMarkup:
    """Кнопки смены статуса: для заявки — оба варианта, иначе — противоположный текущему."""
    approve_btn = types.InlineKeyboardButton(
        text=BTN_ACCESS_APPROVE, callback_data=access.decision_callback(user_id, True),
    )
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    if status == 'approved':
        keyboard.add(types.InlineKeyboardButton(
            text=BTN_ACCESS_REVOKE, callback_data=access.decision_callback(user_id, False),
        ))
    elif status == 'rejected':
        keyboard.add(approve_btn)
    else:
        keyboard.row(approve_btn, types.InlineKeyboardButton(
            text=BTN_ACCESS_REJECT, callback_data=access.decision_callback(user_id, False),
        ))
    return keyboard


def _forget_user_state(user_id: int) -> None:
    """Сбрасывает незавершённые диалоги пользователя (при отзыве доступа)."""
    bot.clear_step_handler_by_chat_id(user_id)
    for state in (pending_model_selection, pending_article_links, pending_reassign,
                  pending_assign_list, pending_md_generation):
        state.pop(user_id, None)


@bot.callback_query_handler(func=lambda call: call.data == access.REQUEST_CALLBACK)
def handle_access_request(call: telebot.types.CallbackQuery) -> None:
    """Кнопка «Запросить доступ»: создаёт заявку и уведомляет владельца (однократно)."""
    user = call.from_user
    outcome = access.request_access(user.id, user.username, user.first_name, OWNER_ID)
    if outcome == RequestOutcome.REQUESTED:
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(MSG_ACCESS_REQUESTED, call.message.chat.id, call.message.message_id)
        except Exception as e:
            logger.warning('Не удалось обновить сообщение заявки для %s: %s', user.id, e)
            bot.send_message(call.message.chat.id, MSG_ACCESS_REQUESTED)
        record = {'user_id': user.id, 'username': user.username, 'first_name': user.first_name}
        try:
            bot.send_message(
                OWNER_ID,
                MSG_ACCESS_OWNER_REQUEST.format(user=access.describe_user(record)),
                reply_markup=_decision_keyboard(user.id, 'pending'),
            )
        except Exception as e:
            # Заявка сохранена — владелец увидит её в /users
            logger.error('Не удалось отправить заявку владельцу (user_id=%s): %s', user.id, e)
    elif outcome == RequestOutcome.ALREADY_PENDING:
        bot.answer_callback_query(call.id, MSG_ACCESS_PENDING, show_alert=True)
    elif outcome == RequestOutcome.ALREADY_ALLOWED:
        bot.answer_callback_query(call.id, MSG_ACCESS_ALREADY, show_alert=True)
    else:
        bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: (call.data or '').startswith('access:'))
def handle_access_decision(call: telebot.types.CallbackQuery) -> None:
    """Кнопки владельца: access:approve:{user_id} / access:reject:{user_id}."""
    parsed = access.parse_decision_callback(call.data)
    if parsed is None:
        bot.answer_callback_query(call.id)
        return
    approve, target_id = parsed
    result = access.decide(target_id, approve, call.from_user.id, OWNER_ID)

    if result.outcome == DecideOutcome.NOT_OWNER:
        logger.warning('Решение по доступу не от владельца: user_id=%s', call.from_user.id)
        bot.answer_callback_query(call.id, MSG_ACCESS_OWNER_ONLY, show_alert=True)
        return
    if result.outcome == DecideOutcome.UNKNOWN_USER:
        bot.answer_callback_query(call.id, MSG_ACCESS_USER_NOT_FOUND, show_alert=True)
        return

    label = USER_STATUS_LABEL[result.record['status']]
    bot.answer_callback_query(call.id, label)
    # Кнопки убираем: решение принято. Повторное нажатие — идемпотентно (UNCHANGED)
    try:
        bot.edit_message_text(
            f"{access.describe_user(result.record)} — {label}",
            call.message.chat.id,
            call.message.message_id,
        )
    except Exception as e:
        logger.debug('Сообщение решения не изменено: %s', e)

    if result.outcome == DecideOutcome.UNCHANGED:
        return
    logger.info('Доступ: user_id=%s -> %s', target_id, result.record['status'])
    # Сбрасываем и при одобрении: next-step, зарегистрированный до отзыва, не должен
    # перехватить первое сообщение после повторного одобрения
    _forget_user_state(target_id)
    try:
        bot.send_message(target_id, MSG_ACCESS_GRANTED if approve else MSG_ACCESS_REJECTED)
    except Exception as e:
        # Пользователь мог заблокировать бота — решение всё равно в силе
        logger.warning('Не удалось уведомить user_id=%s: %s', target_id, _error_text(e))
        try:
            bot.send_message(
                OWNER_ID,
                MSG_ACCESS_NOTIFY_FAILED.format(user=access.describe_user(result.record), error=_error_text(e)),
            )
        except Exception as owner_error:
            logger.error('Не удалось сообщить владельцу об ошибке уведомления: %s', owner_error)


@bot.message_handler(commands=['users'])
def handle_users(message: telebot.types.Message) -> None:
    """Только владелец: сводка и по сообщению с кнопками на каждого пользователя."""
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, MSG_ACCESS_OWNER_ONLY)
        return
    users = list_users()
    if not users:
        bot.send_message(message.chat.id, MSG_USERS_EMPTY)
        return
    counts = {status: sum(1 for u in users if u['status'] == status) for status in USER_STATUS_LABEL}
    bot.send_message(message.chat.id, MSG_USERS_TITLE.format(count=len(users), **counts))
    for record in users:
        bot.send_message(
            message.chat.id,
            f"{access.describe_user(record)} — {USER_STATUS_LABEL[record['status']]}",
            reply_markup=_decision_keyboard(record['user_id'], record['status']),
        )


@bot.message_handler(commands=['model'])
def handle_model(message: telebot.types.Message) -> None:
    """Обрабатывает команду /model — показывает текущие настройки и меню выбора."""
    user_id = message.from_user.id
    summary_model, summary_provider = get_user_model(user_id)
    md_model, md_provider = get_user_md_model(user_id)

    text = MSG_CURRENT_MODELS.format(
        summary_model=summary_model,
        summary_provider=summary_provider,
        md_model=md_model,
        md_provider=md_provider,
    )

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton(
            text='Сменить модель конспектов',
            callback_data='choose_provider:summary',
        ),
        types.InlineKeyboardButton(
            text='Сменить модель .md описаний',
            callback_data='choose_provider:md',
        ),
    )
    bot.send_message(message.chat.id, text, reply_markup=keyboard)


@bot.callback_query_handler(func=lambda call: call.data.startswith('choose_provider:'))
def handle_choose_provider(call: telebot.types.CallbackQuery) -> None:
    """Показывает кнопки выбора провайдера."""
    purpose = call.data.split(':')[1]  # 'summary' или 'md'
    purpose_label = 'конспектов' if purpose == 'summary' else '.md описаний'
    keyboard = create_provider_keyboard(purpose)
    bot.edit_message_text(
        MSG_PROVIDER_SELECT.format(purpose=purpose_label),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=keyboard,
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('provider:'))
def handle_provider_callback(call: telebot.types.CallbackQuery) -> None:
    """Обрабатывает выбор провайдера, запрашивает ввод названия модели."""
    user_id = call.from_user.id
    parts = call.data.split(':')
    purpose = parts[1]   # 'summary' или 'md'
    provider = parts[2]  # 'polza', 'ollama'

    pending_model_selection[user_id] = {
        'purpose': purpose,
        'provider': provider,
    }

    example = PROVIDER_EXAMPLES.get(provider, '')
    bot.edit_message_text(
        MSG_MODEL_INPUT.format(provider=PROVIDER_DISPLAY.get(provider, provider), example=example),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )
    bot.answer_callback_query(call.id)
    bot.register_next_step_handler(call.message, process_model_name_input, user_id)


def process_model_name_input(message: telebot.types.Message, user_id: int) -> None:
    """Обрабатывает ввод названия модели от пользователя."""
    if user_id not in pending_model_selection:
        bot.send_message(message.chat.id, 'Сессия истекла. Используй /model заново.')
        return

    model_name = message.text.strip() if message.text else ''
    if not model_name or model_name.startswith('/'):
        pending_model_selection.pop(user_id, None)
        bot.send_message(message.chat.id, 'Ввод модели отменён.')
        return

    session = pending_model_selection[user_id]
    provider = session['provider']
    purpose = session['purpose']
    provider_label = PROVIDER_DISPLAY.get(provider, provider)

    status_msg = bot.send_message(
        message.chat.id,
        MSG_MODEL_CHECKING.format(model=model_name, provider=provider_label),
    )
    bot.send_chat_action(message.chat.id, 'typing')

    is_available, error_message = check_model_availability(model_name, provider)

    if not is_available:
        bot.edit_message_text(
            MSG_MODEL_CHECK_FAILED.format(model=model_name, provider=provider_label, error=error_message),
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
        )
        pending_model_selection.pop(user_id, None)
        return

    model_config = {'provider': provider, 'model': model_name}
    if purpose == 'summary':
        user_models[user_id] = model_config
    else:
        user_md_models[user_id] = model_config

    bot.edit_message_text(
        MSG_MODEL_SET.format(model=model_name, provider=provider_label),
        chat_id=message.chat.id,
        message_id=status_msg.message_id,
    )
    pending_model_selection.pop(user_id, None)


@bot.callback_query_handler(func=lambda call: call.data.startswith('cache:'))
def handle_cache_callback(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает выбор действия при дубликате.

    Callback data имеет формат: "cache:show:url_hash" или "cache:regen:url_hash"
    """
    user_id = call.from_user.id
    parts = call.data.split(':')
    action = parts[1]
    url_hash = parts[2]

    # Получаем URL из временного хранилища
    url = pending_cache_urls.get(url_hash)
    if not url:
        bot.answer_callback_query(call.id, 'Ссылка устарела, отправьте заново')
        return

    model, provider = get_user_model(user_id)

    if action == 'show':
        # Показать сохранённый конспект
        summary = get_cached_summary(url)
        if summary:
            bot.edit_message_text(
                '📦 Сохранённый конспект:',
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
            send_long_message(call.message.chat.id, summary)
            bot.answer_callback_query(call.id, 'Показан сохранённый конспект')
        else:
            bot.answer_callback_query(call.id, 'Конспект не найден')
        # Очищаем временное хранилище
        pending_cache_urls.pop(url_hash, None)

    elif action == 'regen':
        # Сгенерировать заново
        bot.edit_message_text(
            '🔄 Генерирую заново...',
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
        bot.answer_callback_query(call.id, 'Начинаю генерацию')

        # Проверяем доступность модели
        is_available, error_message = check_model_availability(model, provider)
        if not is_available:
            error_text = MSG_MODEL_UNAVAILABLE.format(model=model, provider=provider, error=error_message)
            bot.send_message(call.message.chat.id, error_text)
            logger.warning('Модель %s (%s) недоступна для %s: %s', model, provider, user_id, error_message)
            return

        logger.info('Начинаю регенерацию: url=%s, model=%s, provider=%s, user_id=%s',
                    url, model, provider, user_id)
        try:
            # skip_cache=True для принудительной перегенерации
            result = process_article(
                url,
                model=model,
                provider=provider,
                user_id=user_id,
                skip_cache=True,
            )

            if result is None:
                bot.send_message(
                    call.message.chat.id,
                    MSG_ERROR.format(error='Не удалось обработать статью'),
                )
                return

            summary, article_data = result
            save_article_to_db(article_data, summary, model, user_id, url)

            header = f'🔄 Перегенерировано!\nМодель: {model} ({provider})\n\n'
            if len(header) + len(summary) <= TELEGRAM_MAX_MESSAGE_LENGTH:
                bot.send_message(call.message.chat.id, header + summary)
            else:
                bot.send_message(call.message.chat.id, f'🔄 Перегенерировано! Модель: {model} ({provider})')
                send_long_message(call.message.chat.id, summary)

        except Exception as e:
            bot.send_message(call.message.chat.id, MSG_ERROR.format(error=str(e)))
            logger.error('Ошибка перегенерации для %s: %s', user_id, e)

        # Очищаем временное хранилище
        pending_cache_urls.pop(url_hash, None)


# ========================
# Обработчики для привязки статей к идеям (multiselect)
# ========================


@bot.callback_query_handler(func=lambda call: call.data.startswith('toggle_link:'))
def handle_toggle_link(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает toggle выбора идеи в multiselect.

    Callback data формат: toggle_link:{idea_id}
    """
    user_id = call.from_user.id
    idea_id = int(call.data.split(':')[1])

    # Проверяем наличие активной сессии
    if user_id not in pending_article_links:
        bot.answer_callback_query(call.id, "Сессия истекла, отправь статью заново")
        return

    # Сразу отвечаем на callback, чтобы убрать "часики"
    bot.answer_callback_query(call.id)

    session = pending_article_links[user_id]
    selected = session['selected_ideas']

    # Toggle состояния
    if idea_id in selected:
        selected.discard(idea_id)
    else:
        selected.add(idea_id)

    # Обновляем клавиатуру
    try:
        ideas = get_user_ideas(user_id)
        keyboard = create_link_ideas_keyboard(ideas, selected)

        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error('Ошибка toggle_link для %s: %s', user_id, e)


@bot.callback_query_handler(func=lambda call: call.data == 'link_done')
def handle_link_done(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает завершение выбора идей — сохраняет привязки.
    """
    user_id = call.from_user.id

    if user_id not in pending_article_links:
        bot.answer_callback_query(call.id, "Сессия истекла")
        return

    # Сразу отвечаем на callback, чтобы убрать "часики" в Telegram
    bot.answer_callback_query(call.id)

    session = pending_article_links[user_id]
    article_id = session['article_id']
    selected_ideas = session['selected_ideas']

    try:
        if not selected_ideas:
            # Ничего не выбрано
            bot.edit_message_text(
                MSG_LINK_SKIPPED,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
        else:
            # Сохраняем привязки
            linked_names = []
            for idea_id in selected_ideas:
                success = link_article_to_idea(article_id, idea_id, user_id)
                if success:
                    idea = get_idea_by_id(idea_id, user_id)
                    if idea:
                        linked_names.append(idea['name'])

            if linked_names:
                bot.edit_message_text(
                    MSG_LINK_DONE.format(ideas=", ".join(linked_names)),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
            else:
                bot.edit_message_text(
                    MSG_LINK_SKIPPED,
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
    except Exception as e:
        logger.error('Ошибка привязки статьи к идеям для %s: %s', user_id, e)
        bot.send_message(call.message.chat.id, MSG_ERROR.format(error=str(e)))
    finally:
        # Очищаем сессию в любом случае
        pending_article_links.pop(user_id, None)


@bot.callback_query_handler(func=lambda call: call.data == 'link_skip')
def handle_link_skip(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает отказ от привязки статьи к идеям.
    При отказе статья удаляется из базы данных.
    """
    user_id = call.from_user.id

    # Сразу отвечаем на callback, чтобы убрать "часики"
    bot.answer_callback_query(call.id)

    # Проверяем наличие активной сессии для пользователя
    if user_id not in pending_article_links:
        logger.warning('Сессия не найдена для user_id %s при link_skip', user_id)
    else:
        # Извлекаем article_id из сессии
        article_id = pending_article_links[user_id].get('article_id')

        # Проверяем, что article_id существует (не None и не 0)
        if article_id is None or article_id == 0:
            logger.warning('article_id не найден в сессии для user_id %s', user_id)
        else:
            # Удаляем статью из БД
            try:
                delete_article(article_id)
                logger.info('Статья ID %s удалена из БД по запросу user_id %s', article_id, user_id)
            except Exception as exc:
                logger.error('Ошибка при удалении статьи ID %s для user_id %s: %s', article_id, user_id, exc)

    try:
        bot.edit_message_text(
            MSG_LINK_SKIPPED,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
    except Exception as e:
        logger.error('Ошибка link_skip для %s: %s', user_id, e)
    finally:
        # Очищаем сессию в любом случае
        pending_article_links.pop(user_id, None)


@bot.message_handler(func=lambda message: extract_url(message.text) is not None)
def handle_url(message: telebot.types.Message) -> None:
    """
    Обрабатывает сообщения с URL - создает конспект статьи.

    Извлекает URL из сообщения, проверяет поддержку источника,
    проверяет наличие в кеше, обрабатывает статью с помощью
    выбранной модели и отправляет конспект.
    """
    url = extract_url(message.text)
    if not url:
        bot.reply_to(message, MSG_UNKNOWN)
        return

    user_id = message.from_user.id
    model, provider = get_user_model(user_id)

    if not is_supported_url(url):
        bot.reply_to(message, MSG_UNSUPPORTED)
        return

    # Проверяем наличие в кеше
    if article_exists(url):
        logger.info('Дубликат статьи обнаружен: url=%s, user_id=%s', url, user_id)
        url_hash = str(hash(url))[-10:]
        pending_cache_urls[url_hash] = url
        keyboard = create_cache_keyboard(url)
        bot.send_message(
            message.chat.id,
            MSG_DUPLICATE_FOUND,
            reply_markup=keyboard,
        )
        return

    # Проверяем доступность модели
    is_available, error_message = check_model_availability(model, provider)
    if not is_available:
        error_text = MSG_MODEL_UNAVAILABLE.format(model=model, provider=provider, error=error_message)
        bot.reply_to(message, error_text)
        logger.warning('Модель %s (%s) недоступна для %s: %s', model, provider, user_id, error_message)
        return

    bot.send_chat_action(message.chat.id, 'typing')
    status_msg = bot.reply_to(message, MSG_PROCESSING)

    logger.info('Начинаю обработку статьи: url=%s, model=%s, provider=%s, user_id=%s',
                url, model, provider, user_id)
    try:
        result = process_article(url, model=model, provider=provider, user_id=user_id)

        if result is None:
            bot.edit_message_text(
                MSG_ERROR.format(error='Не удалось обработать статью'),
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
            )
            return

        summary, article_data = result
        # Сохраняем статью в БД и получаем ID
        article_id = save_article_to_db(article_data, summary, model, user_id, url)

        # Удаляем статусное сообщение
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        except Exception:
            pass

        # Отправляем конспект
        header = f'Готово!\nМодель: {model} ({provider})\nИсточник: {url}\n\n'
        if len(header) + len(summary) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            bot.send_message(message.chat.id, header + summary)
        else:
            bot.send_message(message.chat.id, f'Готово! Модель: {model} ({provider})')
            send_long_message(message.chat.id, summary)

        # Предлагаем привязать статью к идеям
        if article_id:
            _offer_link_to_ideas(message.chat.id, user_id, article_id)

    except Exception as e:
        error_text = MSG_ERROR.format(error=str(e))
        try:
            bot.edit_message_text(
                error_text,
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
            )
        except Exception:
            bot.send_message(message.chat.id, error_text)
        logger.error('Ошибка обработки URL для %s: %s', user_id, e)


@bot.message_handler(commands=['new_idea'])
def handle_new_idea(message: telebot.types.Message) -> None:
    """
    Обрабатывает команду /new_idea - начало создания новой идеи.

    Запрашивает у пользователя название идеи, затем описание.
    Использует register_next_step_handler для цепочки сообщений.
    """
    user_id = message.from_user.id
    bot.reply_to(message, MSG_IDEA_ASK_NAME)
    # Регистрируем следующий шаг - получение названия
    bot.register_next_step_handler(message, process_idea_name, user_id)


def process_idea_name(message: telebot.types.Message, user_id: int) -> None:
    """
    Обрабатывает название идеи, запрашивает описание.

    Args:
        message: сообщение с названием идеи
        user_id: ID пользователя
    """
    idea_name = message.text.strip()
    if not idea_name:
        bot.send_message(message.chat.id, "Название не может быть пустым. Пришли название идеи:")
        bot.register_next_step_handler(message, process_idea_name, user_id)
        return

    bot.send_message(message.chat.id, MSG_IDEA_ASK_DESCRIPTION)
    # Регистрируем следующий шаг - получение описания
    bot.register_next_step_handler(message, process_idea_description, user_id, idea_name)


def process_idea_description(message: telebot.types.Message, user_id: int, idea_name: str) -> None:
    """
    Обрабатывает описание идеи, создаёт идею в базе и запускает генерацию .md.

    Args:
        message: сообщение с описанием идеи
        user_id: ID пользователя
        idea_name: название идеи, полученное на предыдущем шаге
    """
    idea_description = message.text.strip() if message.text else ""

    try:
        idea_id = create_idea(idea_name, idea_description if idea_description else None, user_id)
        bot.send_message(message.chat.id, MSG_IDEA_CREATED.format(name=idea_name))
        # Автоматическая генерация .md по описанию
        _auto_generate_md(
            message.chat.id, user_id, idea_id, idea_name, idea_description or None,
        )
    except Exception as e:
        bot.send_message(message.chat.id, MSG_ERROR.format(error=str(e)))
        logger.error('Ошибка создания идеи для %s: %s', user_id, e)


@bot.message_handler(commands=['ideas'])
def handle_ideas(message: telebot.types.Message) -> None:
    """
    Обрабатывает команду /ideas - показывает список идей пользователя.

    Получает все идеи пользователя и отображает их в виде inline-клавиатуры.
    """
    user_id = message.from_user.id
    ideas = get_user_ideas(user_id)

    if not ideas:
        bot.send_message(message.chat.id, MSG_IDEAS_EMPTY)
        return

    # Создаём inline-клавиатуру со списком идей
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for idx, idea in enumerate(ideas, 1):
        button = types.InlineKeyboardButton(
            text=f"{idx}. {idea['name'][:50]}",  # Ограничиваем длину названия
            callback_data=f"view_idea:{idea['id']}",
        )
        keyboard.add(button)

    bot.send_message(message.chat.id, MSG_IDEAS_TITLE, reply_markup=keyboard)
    

@bot.message_handler(commands=['articles'])
def handle_articles(message: telebot.types.Message) -> None:
    """Показывает все статьи пользователя с привязками к идеям и кнопкой привязки."""
    user_id = message.from_user.id
    articles = get_user_articles(user_id)
    if not articles:
        bot.send_message(message.chat.id, MSG_ARTICLES_EMPTY)
        return
    # Формируем текст и inline-кнопки привязки
    lines: list[str] = []
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    for idx, art in enumerate(articles, 1):
        ideas = get_ideas_by_article(art['id'], user_id)
        idea_names = ", ".join(i['name'] for i in ideas) if ideas else "(без идеи)"
        lines.append(f"{idx}. [{art['source']}] {art['title'][:50]}\n   Идеи: {idea_names}")
        read_btn = types.InlineKeyboardButton(
            text=f"📄 {idx}. Читать",
            callback_data=f"show_summary:{art['id']}:0",
        )
        assign_btn = types.InlineKeyboardButton(
            text=f"-> {idx}. В идею",
            callback_data=f"assign_list:{art['id']}",
        )
        keyboard.row(read_btn, assign_btn)
    text = MSG_ARTICLES_TITLE.format(count=len(articles)) + "\n\n" + "\n".join(lines)
    # Разбиваем длинный текст, клавиатуру добавляем к последнему сообщению
    if len(text) <= MESSAGE_CHUNK_SIZE:
        bot.send_message(message.chat.id, text, reply_markup=keyboard)
    else:
        send_long_message(message.chat.id, text)
        bot.send_message(message.chat.id, "Привязка статей к идеям:", reply_markup=keyboard)


@bot.callback_query_handler(func=lambda call: call.data.startswith('reassign:'))
def handle_reassign_start(call: telebot.types.CallbackQuery) -> None:
    """Начало перепривязки. Callback: reassign:{article_id}:{source_idea_id}"""
    user_id = call.from_user.id
    parts = call.data.split(':')
    article_id, source_idea_id = int(parts[1]), int(parts[2])
    logger.info(
        'Перепривязка: user_id=%s, article_id=%d, source_idea_id=%d',
        user_id, article_id, source_idea_id,
    )
    ideas = [i for i in get_user_ideas(user_id) if i['id'] != source_idea_id]
    if not ideas:
        bot.answer_callback_query(call.id, MSG_REASSIGN_NO_IDEAS)
        return
    pending_reassign[user_id] = {
        'article_id': article_id,
        'source_idea_id': source_idea_id,
        'selected_ideas': set(),
    }
    bot.send_message(
        call.message.chat.id,
        MSG_REASSIGN_SELECT,
        reply_markup=create_reassign_keyboard(ideas, set()),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('toggle_reassign:'))
def handle_toggle_reassign(call: telebot.types.CallbackQuery) -> None:
    """Toggle выбора идеи при перепривязке."""
    user_id = call.from_user.id
    session = pending_reassign.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "Сессия истекла")
        return
    idea_id = int(call.data.split(':')[1])
    selected = session['selected_ideas']
    if idea_id in selected:
        selected.discard(idea_id)
    else:
        selected.add(idea_id)
    ideas = [i for i in get_user_ideas(user_id) if i['id'] != session['source_idea_id']]
    bot.edit_message_reply_markup(
        call.message.chat.id,
        call.message.message_id,
        reply_markup=create_reassign_keyboard(ideas, selected),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == 'reassign_done')
def handle_reassign_done(call: telebot.types.CallbackQuery) -> None:
    """Завершение перепривязки: link к новым идеям, unlink из старой."""
    user_id = call.from_user.id
    session = pending_reassign.pop(user_id, None)
    if not session or not session['selected_ideas']:
        bot.answer_callback_query(call.id, MSG_REASSIGN_CANCELLED)
        return
    for idea_id in session['selected_ideas']:
        link_article_to_idea(session['article_id'], idea_id, user_id)
    unlink_article_from_idea(session['article_id'], session['source_idea_id'], user_id)
    logger.info(
        'Перепривязка завершена: user_id=%s, article_id=%d, из idea_id=%d в ideas=%s',
        user_id, session['article_id'], session['source_idea_id'],
        list(session['selected_ideas']),
    )
    bot.edit_message_text(MSG_REASSIGN_DONE, call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == 'reassign_cancel')
def handle_reassign_cancel(call: telebot.types.CallbackQuery) -> None:
    """Отмена перепривязки."""
    user_id = call.from_user.id
    pending_reassign.pop(user_id, None)
    bot.edit_message_text(MSG_REASSIGN_CANCELLED, call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('assign_list:'))
def handle_assign_list_start(call: telebot.types.CallbackQuery) -> None:
    """Начало привязки статьи к идеям из общего списка /articles."""
    user_id = call.from_user.id
    article_id = int(call.data.split(':')[1])
    logger.info('Привязка из /articles: user_id=%s, article_id=%d', user_id, article_id)
    ideas = get_user_ideas(user_id)
    if not ideas:
        bot.answer_callback_query(call.id, MSG_ASSIGN_NO_IDEAS)
        return
    pending_assign_list[user_id] = {
        'article_id': article_id,
        'selected_ideas': set(),
    }
    bot.send_message(
        call.message.chat.id,
        MSG_ASSIGN_SELECT,
        reply_markup=create_assign_list_keyboard(ideas, set()),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('toggle_assign_list:'))
def handle_toggle_assign_list(call: telebot.types.CallbackQuery) -> None:
    """Toggle выбора идеи при привязке из общего списка."""
    user_id = call.from_user.id
    session = pending_assign_list.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "Сессия истекла")
        return
    idea_id = int(call.data.split(':')[1])
    selected = session['selected_ideas']
    if idea_id in selected:
        selected.discard(idea_id)
    else:
        selected.add(idea_id)
    ideas = get_user_ideas(user_id)
    bot.edit_message_reply_markup(
        call.message.chat.id,
        call.message.message_id,
        reply_markup=create_assign_list_keyboard(ideas, selected),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == 'assign_list_done')
def handle_assign_list_done(call: telebot.types.CallbackQuery) -> None:
    """Завершение привязки статьи к идеям из общего списка."""
    user_id = call.from_user.id
    session = pending_assign_list.pop(user_id, None)
    if not session or not session['selected_ideas']:
        bot.answer_callback_query(call.id, MSG_ASSIGN_CANCELLED)
        return
    for idea_id in session['selected_ideas']:
        link_article_to_idea(session['article_id'], idea_id, user_id)
    logger.info(
        'Привязка из /articles завершена: user_id=%s, article_id=%d, ideas=%s',
        user_id, session['article_id'], list(session['selected_ideas']),
    )
    bot.edit_message_text(MSG_ASSIGN_DONE, call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == 'assign_list_cancel')
def handle_assign_list_cancel(call: telebot.types.CallbackQuery) -> None:
    """Отмена привязки из общего списка."""
    user_id = call.from_user.id
    pending_assign_list.pop(user_id, None)
    bot.edit_message_text(MSG_ASSIGN_CANCELLED, call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('gen_md:'))
def handle_generate_md(call: telebot.types.CallbackQuery) -> None:
    """Показ существующего .md или генерация нового."""
    user_id = call.from_user.id
    idea_id = int(call.data.split(':')[1])
    idea = get_idea_by_id(idea_id, user_id)
    if not idea:
        bot.answer_callback_query(call.id, MSG_IDEA_NOT_FOUND)
        return
    if not idea['description']:
        bot.answer_callback_query(call.id, "Добавь описание идеи для генерации .md")
        return
    bot.answer_callback_query(call.id)
    # Проверяем, есть ли уже сохранённый .md
    existing_md = get_idea_md(idea_id, user_id)
    if existing_md:
        logger.info('Показ существующего .md: idea_id=%d, user_id=%s', idea_id, user_id)
        send_long_message(call.message.chat.id, existing_md)
        # Загружаем в сессию для возможности правок
        pending_md_generation[user_id] = {'idea_id': idea_id, 'draft_md': existing_md}
        keyboard = types.InlineKeyboardMarkup(row_width=3)
        keyboard.row(
            types.InlineKeyboardButton(
                text="Перегенерировать", callback_data=f"regen_md:{idea_id}",
            ),
            types.InlineKeyboardButton(
                text="Замечания", callback_data=f"revise_md:{idea_id}",
            ),
        )
        bot.send_message(call.message.chat.id, MSG_MD_READY, reply_markup=keyboard)
        return
    # Нет сохранённого — генерируем
    _auto_generate_md(
        call.message.chat.id, user_id, idea_id,
        idea['name'], idea['description'],
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('regen_md:'))
def handle_regen_md(call: telebot.types.CallbackQuery) -> None:
    """Принудительная перегенерация .md."""
    user_id = call.from_user.id
    idea_id = int(call.data.split(':')[1])
    idea = get_idea_by_id(idea_id, user_id)
    if not idea:
        bot.answer_callback_query(call.id, MSG_IDEA_NOT_FOUND)
        return
    bot.answer_callback_query(call.id)
    logger.info('Перегенерация .md: idea_id=%d, user_id=%s', idea_id, user_id)
    _auto_generate_md(
        call.message.chat.id, user_id, idea_id,
        idea['name'], idea['description'],
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('approve_md:'))
def handle_approve_md(call: telebot.types.CallbackQuery) -> None:
    """Сохраняет утвержденный .md."""
    user_id = call.from_user.id
    idea_id = int(call.data.split(':')[1])
    session = pending_md_generation.get(user_id)
    if not session or session['idea_id'] != idea_id:
        bot.answer_callback_query(call.id, "Сессия истекла")
        return
    update_idea_md(idea_id, user_id, session['draft_md'])
    pending_md_generation.pop(user_id, None)
    bot.edit_message_text(MSG_MD_APPROVED, call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('revise_md:'))
def handle_revise_md(call: telebot.types.CallbackQuery) -> None:
    """Запрос замечаний для переработки .md."""
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "Отправь замечания текстом или свой вариант .md (начиная с #):",
    )
    bot.register_next_step_handler(call.message, process_md_feedback, call.from_user.id)


def process_md_feedback(message: telebot.types.Message, user_id: int) -> None:
    """Обработка замечаний/правок .md."""
    session = pending_md_generation.get(user_id)
    if not session:
        bot.send_message(message.chat.id, "Сессия истекла, начни генерацию заново.")
        return
    idea_id = session['idea_id']
    feedback = message.text.strip()
    if feedback.startswith('#'):
        session['draft_md'] = feedback
        send_long_message(message.chat.id, feedback)
    else:
        bot.send_message(message.chat.id, MSG_MD_REVISING)
        bot.send_chat_action(message.chat.id, 'typing')
        md_model, md_provider = get_user_md_model(user_id)
        try:
            revised = revise_idea_md(session['draft_md'], feedback, md_model, md_provider)
        except Exception as e:
            bot.send_message(message.chat.id, MSG_ERROR.format(error=str(e)))
            return
        session['draft_md'] = revised
        send_long_message(message.chat.id, revised)
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        types.InlineKeyboardButton(text="Утвердить", callback_data=f"approve_md:{idea_id}"),
        types.InlineKeyboardButton(text="Замечания", callback_data=f"revise_md:{idea_id}"),
    )
    bot.send_message(message.chat.id, MSG_MD_READY, reply_markup=keyboard)


@bot.message_handler(func=lambda message: True)
def handle_unknown(message: telebot.types.Message) -> None:
    """Обрабатывает все остальные сообщения - отправляет подсказку."""
    bot.reply_to(message, MSG_UNKNOWN)


# ========================
# Обработчики команд для работы с идеями
# ========================


@bot.callback_query_handler(func=lambda call: call.data.startswith('view_idea:'))
def handle_view_idea(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает нажатие кнопки просмотра идеи.

    Показывает детали идеи с возможностью редактирования и удаления.
    """
    user_id = call.from_user.id
    idea_id = int(call.data.split(':')[1])

    idea = get_idea_by_id(idea_id, user_id)

    if not idea:
        bot.answer_callback_query(call.id, MSG_IDEA_NOT_FOUND)
        return

    # Формируем сообщение с деталями идеи
    idea_text = f"**{idea['name']}**\n\n{idea['description'] or '(нет описания)'}"

    # Создаём клавиатуру с кнопками
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    articles_btn = types.InlineKeyboardButton(
        text="📚 Статьи",
        callback_data=f"idea_articles:{idea_id}",
    )
    edit_btn = types.InlineKeyboardButton(
        text="✏️ Редактировать",
        callback_data=f"edit_idea:{idea_id}",
    )
    delete_btn = types.InlineKeyboardButton(
        text="🗑️ Удалить",
        callback_data=f"delete_idea:{idea_id}",
    )
    generate_md_btn = types.InlineKeyboardButton(
        text="Описание (.md)",
        callback_data=f"gen_md:{idea_id}",
    )
    keyboard.row(articles_btn, generate_md_btn)
    keyboard.row(edit_btn, delete_btn)

    bot.edit_message_text(
        idea_text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=keyboard,
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('idea_articles:'))
def handle_idea_articles(call: telebot.types.CallbackQuery) -> None:
    """
    Показывает список статей, привязанных к идее.

    Callback data формат: idea_articles:{idea_id}
    """
    user_id = call.from_user.id
    idea_id = int(call.data.split(':')[1])

    idea = get_idea_by_id(idea_id, user_id)
    if not idea:
        bot.answer_callback_query(call.id, MSG_IDEA_NOT_FOUND)
        return

    articles = get_articles_by_idea(idea_id, user_id)

    if not articles:
        # Нет статей — показываем сообщение с кнопкой "Назад"
        keyboard = types.InlineKeyboardMarkup()
        back_btn = types.InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"view_idea:{idea_id}",
        )
        keyboard.add(back_btn)

        bot.edit_message_text(
            MSG_IDEA_NO_ARTICLES,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=keyboard,
        )
        bot.answer_callback_query(call.id)
        return

    # Формируем список статей с кнопками
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    for idx, article in enumerate(articles, 1):
        article_id = article['id']
        title = article['title'][:25] + "..." if len(article['title']) > 25 else article['title']

        # Кнопка "Конспект" и "Отвязать" для каждой статьи
        summary_btn = types.InlineKeyboardButton(
            text=f"📄 {idx}. {title}",
            callback_data=f"show_summary:{article_id}:{idea_id}",
        )
        unlink_btn = types.InlineKeyboardButton(
            text="🔗❌",
            callback_data=f"unlink:{article_id}:{idea_id}",
        )
        reassign_btn = types.InlineKeyboardButton(
            text="->",
            callback_data=f"reassign:{article_id}:{idea_id}",
        )
        keyboard.row(summary_btn, unlink_btn, reassign_btn)

    # Кнопка "Назад"
    back_btn = types.InlineKeyboardButton(
        text="⬅️ Назад к идее",
        callback_data=f"view_idea:{idea_id}",
    )
    keyboard.add(back_btn)

    bot.edit_message_text(
        MSG_IDEA_ARTICLES_TITLE.format(name=idea['name']),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=keyboard,
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('show_summary:'))
def handle_show_summary(call: telebot.types.CallbackQuery) -> None:
    """
    Показывает конспект статьи.

    Callback data формат: show_summary:{article_id}:{idea_id}
    """
    parts = call.data.split(':')
    article_id = int(parts[1])

    article = get_article_by_id(article_id)
    if not article:
        bot.answer_callback_query(call.id, "Статья не найдена")
        return

    summary = article.get('summary', '(конспект отсутствует)')
    title = article.get('title', 'Без названия')
    url = article.get('url', '')

    # Отправляем конспект отдельным сообщением
    header = f"📄 {title}\n🔗 {url}\n\n"
    bot.answer_callback_query(call.id)

    if len(header) + len(summary) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        bot.send_message(call.message.chat.id, header + summary)
    else:
        bot.send_message(call.message.chat.id, header)
        send_long_message(call.message.chat.id, summary)


@bot.callback_query_handler(func=lambda call: call.data.startswith('unlink:'))
def handle_unlink_article(call: telebot.types.CallbackQuery) -> None:
    """
    Отвязывает статью от идеи.

    Callback data формат: unlink:{article_id}:{idea_id}
    """
    user_id = call.from_user.id
    parts = call.data.split(':')
    article_id = int(parts[1])
    idea_id = int(parts[2])

    success = unlink_article_from_idea(article_id, idea_id, user_id)

    if success:
        bot.answer_callback_query(call.id, MSG_ARTICLE_UNLINKED)
        # Обновляем список статей — симулируем нажатие на "Статьи"
        call.data = f"idea_articles:{idea_id}"
        handle_idea_articles(call)
    else:
        bot.answer_callback_query(call.id, "Не удалось отвязать статью")


@bot.callback_query_handler(func=lambda call: call.data.startswith('edit_idea:'))
def handle_edit_idea(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает нажатие кнопки редактирования идеи.

    Запрашивает новое название, затем новое описание.
    """
    user_id = call.from_user.id
    idea_id = int(call.data.split(':')[1])

    # Получаем текущую идею для отображения
    idea = get_idea_by_id(idea_id, user_id)
    if not idea:
        bot.answer_callback_query(call.id, MSG_IDEA_NOT_FOUND)
        return

    # Показываем текущее название и запрашиваем новое
    current_text = (
        f"Текущее название: *{idea['name']}*\n\n"
        "Пришли новое название идеи:"
    )
    bot.send_message(call.message.chat.id, current_text, parse_mode='Markdown')
    bot.register_next_step_handler(call.message, process_edit_name, user_id, idea_id)


def process_edit_name(message: telebot.types.Message, user_id: int, idea_id: int) -> None:
    """
    Обрабатывает новое название при редактировании.

    Args:
        message: сообщение с новым названием
        user_id: ID пользователя
        idea_id: ID идеи
    """
    new_name = message.text.strip()
    if not new_name:
        bot.send_message(message.chat.id, "Название не может быть пустым. Пришли новое название:")
        bot.register_next_step_handler(message, process_edit_name, user_id, idea_id)
        return

    # Получаем текущее описание для отображения
    idea = get_idea_by_id(idea_id, user_id)
    if idea:
        current_desc = idea['description'] or '(нет описания)'
        current_text = (
            f"Новое название: *{new_name}*\n\n"
            f"Текущее описание:\n{current_desc}\n\n"
            "Пришли новое описание (или /skip чтобы оставить текущее):"
        )
        bot.send_message(message.chat.id, current_text, parse_mode='Markdown')

    # Регистрируем следующий шаг - получение нового описания
    bot.register_next_step_handler(message, process_edit_description, user_id, idea_id, new_name)


def process_edit_description(message: telebot.types.Message, user_id: int, idea_id: int, new_name: str) -> None:
    """
    Обрабатывает новое описание при редактировании.

    Args:
        message: сообщение с новым описанием
        user_id: ID пользователя
        idea_id: ID идеи
        new_name: новое название (уже введённое)
    """
    new_description = None
    if message.text and message.text.strip() != '/skip':
        new_description = message.text.strip()

    try:
        success = update_idea(idea_id, user_id, name=new_name, description=new_description)
        if success:
            bot.send_message(message.chat.id, MSG_IDEA_UPDATED.format(name=new_name))
            # Перегенерация .md при изменении описания
            if new_description:
                _auto_generate_md(
                    message.chat.id, user_id, idea_id, new_name, new_description,
                )
        else:
            bot.send_message(message.chat.id, MSG_IDEA_NOT_FOUND)
    except Exception as e:
        bot.send_message(message.chat.id, MSG_ERROR.format(error=str(e)))
        logger.error('Ошибка обновления идеи для %s: %s', user_id, e)


@bot.callback_query_handler(func=lambda call: call.data.startswith('delete_idea:'))
def handle_delete_idea(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает нажатие кнопки удаления идеи.

    Запрашивает подтверждение перед удалением.
    """
    user_id = call.from_user.id
    idea_id = int(call.data.split(':')[1])

    # Проверяем существование идеи
    idea = get_idea_by_id(idea_id, user_id)
    if not idea:
        bot.answer_callback_query(call.id, MSG_IDEA_NOT_FOUND)
        return

    # Создаём клавиатуру подтверждения
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    confirm_btn = types.InlineKeyboardButton(
        text="Да",
        callback_data=f"confirm_delete:{idea_id}",
    )
    cancel_btn = types.InlineKeyboardButton(
        text="Отмена",
        callback_data=f"cancel_delete:{idea_id}",
    )
    keyboard.add(confirm_btn, cancel_btn)

    bot.edit_message_text(
        MSG_IDEA_CONFIRM_DELETE,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=keyboard,
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_delete:'))
def handle_confirm_delete(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает подтверждение удаления идеи.
    """
    user_id = call.from_user.id
    idea_id = int(call.data.split(':')[1])

    try:
        success = delete_idea(idea_id, user_id)
        if success:
            bot.edit_message_text(
                MSG_IDEA_DELETED,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
        else:
            bot.edit_message_text(
                MSG_IDEA_NOT_FOUND,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
    except Exception as e:
        bot.edit_message_text(
            MSG_ERROR.format(error=str(e)),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
        logger.error('Ошибка удаления идеи для %s: %s', user_id, e)


@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_delete:'))
def handle_cancel_delete(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает отмену удаления идеи.
    """
    idea_id = int(call.data.split(':')[1])

    # Получаем обновлённую информацию об идее
    user_id = call.from_user.id
    idea = get_idea_by_id(idea_id, user_id)

    if idea:
        # Возвращаемся к просмотру деталей идеи
        idea_text = f"**{idea['name']}**\n\n{idea['description'] or '(нет описания)'}"

        keyboard = types.InlineKeyboardMarkup(row_width=2)
        articles_btn = types.InlineKeyboardButton(
            text="📚 Статьи",
            callback_data=f"idea_articles:{idea_id}",
        )
        edit_btn = types.InlineKeyboardButton(
            text="✏️ Редактировать",
            callback_data=f"edit_idea:{idea_id}",
        )
        delete_btn = types.InlineKeyboardButton(
            text="🗑️ Удалить",
            callback_data=f"delete_idea:{idea_id}",
        )
        keyboard.row(articles_btn)
        keyboard.row(edit_btn, delete_btn)

        bot.edit_message_text(
            idea_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=keyboard,
        )
    else:
        bot.edit_message_text(
            MSG_IDEA_NOT_FOUND,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )

    bot.answer_callback_query(call.id)


def main() -> None:
    """Запуск бота."""
    ensure_directories()
    init_db()

    # Проверяем подключение к провайдерам
    logger.info('Проверка провайдеров...')
    providers_status = check_providers_status()
    for prov, (is_ok, msg) in providers_status.items():
        if is_ok:
            logger.info('Провайдер %s: OK — %s', prov, msg)
        else:
            logger.warning('Провайдер %s: НЕДОСТУПЕН — %s', prov, msg)

    # Проверяем дефолтные модели
    for label, model, provider in [
        ('конспект', DEFAULT_MODEL, DEFAULT_PROVIDER),
        ('.md', DEFAULT_MD_MODEL, DEFAULT_MD_PROVIDER),
    ]:
        is_ok, err = check_model_availability(model, provider)
        if is_ok:
            logger.info('Модель по умолчанию (%s): %s (%s) — OK', label, model, provider)
        else:
            logger.warning('Модель по умолчанию (%s): %s (%s) — НЕДОСТУПНА: %s', label, model, provider, err)

    try:
        bot.set_my_commands([types.BotCommand(name, desc) for name, desc in BOT_COMMANDS])
    except Exception as e:
        logger.warning('Не удалось установить меню команд: %s', e)

    logger.info("Бот запущен, владелец: %s", OWNER_ID)

    atexit.register(lambda: logger.info("Бот остановлен"))
    bot.infinity_polling(timeout=60, long_polling_timeout=60)


if __name__ == '__main__':
    main()
