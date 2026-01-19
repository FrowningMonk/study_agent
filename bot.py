"""
Telegram-бот для агента изучения ИИ.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README репозиториев)
    - infostart.ru (статьи и публикации по 1С)

Поддерживаемые модели:
    - gemma3:12b (локальная, Ollama) — по умолчанию
    - gpt-3.5-turbo (OpenAI)
    - gpt-4 (OpenAI)

Команды бота:
    /start — Приветствие и инструкция
    /help  — Справка по использованию
    /model — Выбор модели для генерации
    <URL>  — Отправить ссылку → получить конспект

Example:
    python bot.py
"""

import os

import telebot
from telebot import types
from dotenv import load_dotenv

from pipeline import ensure_directories, process_article
from summarizer import AVAILABLE_MODELS, DEFAULT_MODEL, check_model_availability

# Загружаем переменные окружения
load_dotenv()

# Публичный API модуля
__all__ = ['bot', 'main']

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

# Токен бота
TELEGRAM_TOKEN: str | None = os.getenv('TELEGRAM_BOT_TOKEN')

# Поддерживаемые источники
SUPPORTED_SOURCES: dict[str, str] = {
    'habr.com': 'habr',
    'github.com': 'github',
    'infostart.ru': 'infostart',
}

# Хранилище выбранных моделей пользователей: {user_id: model_name}
user_models: dict[int, str] = {}

# Читаемые названия моделей для отображения
MODEL_DISPLAY_NAMES: dict[str, str] = {
    'gemma3:12b': '🏠 Gemma 3 12B (локальная)',
    'gpt-3.5-turbo': '⚡ GPT-3.5 Turbo',
    'gpt-4': '🧠 GPT-4',
}

# =============================================================================
# ТЕКСТЫ СООБЩЕНИЙ
# =============================================================================

MSG_WELCOME: str = """👋 Привет, {name}!

Я — агент для изучения ИИ. Помогу создать структурированный конспект из статьи или README репозитория.

🔧 Как использовать:
1. Отправь ссылку
2. Подожди 30-60 секунд
3. Получи готовый конспект!

📌 Поддерживаемые источники:
• habr.com — технические статьи
• github.com — README репозиториев

🤖 Текущая модель: {model}
Сменить модель: /model

💡 Попробуй отправить ссылку прямо сейчас!

/help — справка по командам"""

MSG_HELP: str = """📖 Справка по боту

Команды:
/start — Приветствие
/help — Эта справка
/model — Выбор модели

Использование:
Отправь ссылку, например:
• https://habr.com/ru/articles/984968/
• https://github.com/anthropics/anthropic-cookbook

Поддерживаемые источники:
✅ Хабр (habr.com) — статьи
✅ GitHub (github.com) — README репозиториев

Доступные модели:
🏠 gemma3:12b — локальная (Ollama), по умолчанию
⚡ gpt-3.5-turbo — OpenAI, быстрая
🧠 gpt-4 — OpenAI, качественная

Время обработки:
~30-60 секунд (зависит от размера контента и модели)

Возможные ошибки:
• Контент не найден — проверь ссылку
• Таймаут — попробуй ещё раз
• Ошибка API — подожди минуту и повтори
• Ollama не запущена — запусти ollama serve"""

MSG_UNSUPPORTED_SOURCE: str = """⚠️ Источник не поддерживается.

Поддерживаемые источники:
• habr.com — статьи
• github.com — README репозиториев

Отправь ссылку с одного из этих сайтов."""

MSG_PROCESSING_HABR: str = """⏳ Обрабатываю статью...

🤖 Модель: {model}
Это займёт 30-60 секунд."""

MSG_PROCESSING_GITHUB: str = """⏳ Анализирую репозиторий...

🤖 Модель: {model}
Это займёт 30-60 секунд."""

MSG_SUCCESS: str = '✅ Конспект готов!'

MSG_ERROR_GENERIC: str = """❌ Не удалось обработать контент.

Возможные причины:
• Контент не найден
• Ошибка парсинга
• Проблема с API
• Ollama не запущена (если используется локальная модель)

Проверь ссылку и попробуй ещё раз."""

MSG_ERROR_WITH_DETAILS: str = """❌ Произошла ошибка: {error}

Попробуй ещё раз через минуту."""

MSG_MODEL_UNAVAILABLE: str = """❌ Модель недоступна

🤖 Модель: {model}
⚠️ {error}

Что делать:
• Выбрать другую модель: /model
• Проверить, что Ollama запущена (для локальных моделей)
• Проверить API ключ в .env (для OpenAI моделей)"""

MSG_UNKNOWN_COMMAND: str = """🤔 Не понял команду.

Отправь ссылку на статью или репозиторий, например:
• https://habr.com/ru/articles/984968/
• https://github.com/anthropics/anthropic-cookbook

/help — справка по командам"""

MSG_MODEL_SELECT: str = """🤖 Выбор модели

Текущая модель: {current_model}

Выбери модель для генерации конспектов:"""

MSG_MODEL_CHANGED: str = """✅ Модель изменена!

Новая модель: {model}

Теперь конспекты будут генерироваться с помощью этой модели."""


# =============================================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# =============================================================================


def _init_bot() -> telebot.TeleBot | None:
    """
    Инициализирует бота с проверкой токена.

    Returns:
        Экземпляр бота или None если токен не найден.
    """
    if not TELEGRAM_TOKEN:
        print('❌ Ошибка: TELEGRAM_BOT_TOKEN не найден в .env файле!')
        print('   Добавьте строку: TELEGRAM_BOT_TOKEN=ваш_токен')
        return None
    return telebot.TeleBot(TELEGRAM_TOKEN)


# Создаём экземпляр бота
bot = _init_bot()


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================


def extract_url(text: str | None) -> str | None:
    """
    Извлекает URL из текста сообщения.

    Args:
        text: Текст для проверки.

    Returns:
        Найденный URL или None.
    """
    if not text:
        return None

    # Разбиваем текст на слова
    words = text.split()

    # Ищем первое слово, начинающееся с http:// или https://
    for word in words:
        word = word.strip()
        if word.startswith('http://') or word.startswith('https://'):
            return word

    return None


def is_url(text: str | None) -> bool:
    """
    Проверяет, содержит ли текст URL-адрес.

    Args:
        text: Текст для проверки.

    Returns:
        True если текст содержит URL, иначе False.
    """
    return extract_url(text) is not None


def is_supported_url(url: str) -> bool:
    """
    Проверяет, поддерживается ли источник URL.

    Args:
        url: URL для проверки.

    Returns:
        True если источник поддерживается, иначе False.
    """
    return any(source in url for source in SUPPORTED_SOURCES)


def get_source_type(url: str) -> str:
    """
    Определяет тип источника по URL.

    Args:
        url: URL статьи или репозитория.

    Returns:
        Тип источника ('habr', 'github') или 'unknown'.
    """
    for domain, source_type in SUPPORTED_SOURCES.items():
        if domain in url:
            return source_type
    return 'unknown'


def get_user_model(user_id: int) -> str:
    """
    Возвращает выбранную модель пользователя.

    Args:
        user_id: ID пользователя Telegram.

    Returns:
        Название модели (или дефолтная, если не выбрана).
    """
    return user_models.get(user_id, DEFAULT_MODEL)


def get_model_display_name(model: str) -> str:
    """
    Возвращает читаемое название модели.

    Args:
        model: Техническое название модели.

    Returns:
        Читаемое название для отображения.
    """
    return MODEL_DISPLAY_NAMES.get(model, model)


def get_processing_message(url: str, model: str) -> str:
    """
    Возвращает сообщение о начале обработки в зависимости от источника.

    Args:
        url: URL для обработки.
        model: Используемая модель.

    Returns:
        Текст сообщения.
    """
    model_name = get_model_display_name(model)
    source_type = get_source_type(url)

    if source_type == 'github':
        return MSG_PROCESSING_GITHUB.format(model=model_name)
    return MSG_PROCESSING_HABR.format(model=model_name)


def create_model_keyboard(current_model: str) -> types.InlineKeyboardMarkup:
    """
    Создаёт inline-клавиатуру для выбора модели.

    Args:
        current_model: Текущая выбранная модель.

    Returns:
        Объект InlineKeyboardMarkup.
    """
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for model_name in AVAILABLE_MODELS.keys():
        display_name = get_model_display_name(model_name)

        # Добавляем галочку к текущей модели
        if model_name == current_model:
            display_name = f'✓ {display_name}'

        button = types.InlineKeyboardButton(
            text=display_name,
            callback_data=f'model:{model_name}',
        )
        keyboard.add(button)

    return keyboard


def send_long_message(chat_id: int, text: str, chunk_size: int = 4000) -> None:
    """
    Отправляет длинное сообщение частями.

    Args:
        chat_id: ID чата.
        text: Текст для отправки.
        chunk_size: Максимальный размер части.
    """
    paragraphs = text.split('\n\n')
    current_chunk = ''

    for paragraph in paragraphs:
        if len(current_chunk) + len(paragraph) + 2 > chunk_size:
            if current_chunk:
                bot.send_message(chat_id, current_chunk)
                current_chunk = ''

        if current_chunk:
            current_chunk += '\n\n' + paragraph
        else:
            current_chunk = paragraph

    if current_chunk:
        bot.send_message(chat_id, current_chunk)


# =============================================================================
# ОБРАБОТЧИКИ КОМАНД
# =============================================================================


if bot:

    @bot.message_handler(commands=['start'])
    def handle_start(message: telebot.types.Message) -> None:
        """
        Обработчик команды /start.

        Args:
            message: Входящее сообщение от пользователя.
        """
        user_name = message.from_user.first_name or 'друг'
        user_id = message.from_user.id

        current_model = get_user_model(user_id)
        model_name = get_model_display_name(current_model)

        welcome_text = MSG_WELCOME.format(name=user_name, model=model_name)
        bot.reply_to(message, welcome_text)

    @bot.message_handler(commands=['help'])
    def handle_help(message: telebot.types.Message) -> None:
        """
        Обработчик команды /help.

        Args:
            message: Входящее сообщение от пользователя.
        """
        bot.reply_to(message, MSG_HELP)

    @bot.message_handler(commands=['model'])
    def handle_model(message: telebot.types.Message) -> None:
        """
        Обработчик команды /model — показывает меню выбора модели.

        Args:
            message: Входящее сообщение от пользователя.
        """
        user_id = message.from_user.id
        current_model = get_user_model(user_id)
        current_model_name = get_model_display_name(current_model)

        keyboard = create_model_keyboard(current_model)

        bot.send_message(
            message.chat.id,
            MSG_MODEL_SELECT.format(current_model=current_model_name),
            reply_markup=keyboard,
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('model:'))
    def handle_model_callback(call: telebot.types.CallbackQuery) -> None:
        """
        Обработчик нажатия на кнопку выбора модели.

        Args:
            call: Callback-запрос от inline-кнопки.
        """
        user_id = call.from_user.id

        # Извлекаем название модели из callback_data
        model = call.data.split(':', 1)[1]

        # Сохраняем выбор пользователя
        user_models[user_id] = model

        model_name = get_model_display_name(model)

        # Обновляем сообщение
        bot.edit_message_text(
            MSG_MODEL_CHANGED.format(model=model_name),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )

        # Отвечаем на callback (убирает часики на кнопке)
        bot.answer_callback_query(call.id, f'Выбрана модель: {model}')

        print(f'🔄 Пользователь {user_id} выбрал модель: {model}')

    @bot.message_handler(func=lambda message: is_url(message.text))
    def handle_url(message: telebot.types.Message) -> None:
        """
        Обработчик ссылок — основная логика.

        Args:
            message: Входящее сообщение со ссылкой.
        """
        # Извлекаем URL из сообщения
        url = extract_url(message.text)
        if not url:
            bot.reply_to(message, MSG_UNKNOWN_COMMAND)
            return

        user_id = message.from_user.id

        # Получаем модель пользователя
        model = get_user_model(user_id)

        print(f'📨 Получена ссылка от {user_id}: {url}')
        print(f'   Модель: {model}')

        # Проверяем поддержку источника
        if not is_supported_url(url):
            bot.reply_to(message, MSG_UNSUPPORTED_SOURCE)
            return

        # Проверяем доступность модели
        is_available, error_message = check_model_availability(model)
        if not is_available:
            model_name = get_model_display_name(model)
            error_text = MSG_MODEL_UNAVAILABLE.format(
                model=model_name,
                error=error_message,
            )
            bot.reply_to(message, error_text)
            print(f'❌ Модель {model} недоступна для {user_id}: {error_message}')
            return

        # Отправляем статус "печатает..."
        bot.send_chat_action(message.chat.id, 'typing')

        # Уведомляем о начале обработки
        processing_msg = get_processing_message(url, model)
        status_msg = bot.reply_to(message, processing_msg)

        try:
            # Запускаем пайплайн с выбранной моделью
            result_path = process_article(url, model=model, save_json=True)

            if result_path is None:
                bot.edit_message_text(
                    MSG_ERROR_GENERIC,
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                )
                return

            # Читаем готовый конспект
            with open(result_path, 'r', encoding='utf-8') as f:
                summary = f.read()

            # Удаляем статусное сообщение
            _safe_delete_message(message.chat.id, status_msg.message_id)

            # Отправляем конспект
            _send_summary(message.chat.id, summary, url, model)

            print(f'✅ Конспект отправлен пользователю {user_id}')

        except Exception as e:
            _handle_error(message.chat.id, status_msg.message_id, str(e), user_id)

    @bot.message_handler(func=lambda message: True)
    def handle_unknown(message: telebot.types.Message) -> None:
        """
        Обработчик всех остальных сообщений.

        Args:
            message: Входящее сообщение.
        """
        bot.reply_to(message, MSG_UNKNOWN_COMMAND)


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ОБРАБОТЧИКОВ
# =============================================================================


def _safe_delete_message(chat_id: int, message_id: int) -> None:
    """
    Безопасно удаляет сообщение (игнорирует ошибки).

    Args:
        chat_id: ID чата.
        message_id: ID сообщения для удаления.
    """
    try:
        bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _send_summary(chat_id: int, summary: str, url: str, model: str) -> None:
    """
    Отправляет конспект пользователю.

    Args:
        chat_id: ID чата.
        summary: Текст конспекта.
        url: Ссылка на исходную статью.
        model: Использованная модель.
    """
    model_name = get_model_display_name(model)
    header = MSG_SUCCESS + f'\n🤖 Модель: {model_name}\n📎 Источник: {url}\n\n'

    if len(header) + len(summary) <= 4096:
        bot.send_message(chat_id, header + summary)
    else:
        bot.send_message(chat_id, MSG_SUCCESS + f'\n🤖 Модель: {model_name}')
        send_long_message(chat_id, summary)


def _handle_error(chat_id: int, status_message_id: int, error: str, user_id: int) -> None:
    """
    Обрабатывает ошибку и уведомляет пользователя.

    Args:
        chat_id: ID чата.
        status_message_id: ID статусного сообщения для редактирования.
        error: Текст ошибки.
        user_id: ID пользователя для логирования.
    """
    error_text = MSG_ERROR_WITH_DETAILS.format(error=error)

    try:
        bot.edit_message_text(
            error_text,
            chat_id=chat_id,
            message_id=status_message_id,
        )
    except Exception:
        bot.send_message(chat_id, error_text)

    print(f'❌ Ошибка для {user_id}: {error}')


# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================


def main() -> None:
    """Запуск бота."""
    if not bot:
        return

    # Создаём необходимые папки
    ensure_directories()

    print('=' * 60)
    print('🤖 TELEGRAM-БОТ АГЕНТА ДЛЯ ИЗУЧЕНИЯ ИИ')
    print('=' * 60)
    print('✅ Бот запущен и готов к работе!')
    print('📌 Поддерживаемые источники:')
    for domain in SUPPORTED_SOURCES:
        print(f'   • {domain}')
    print('🤖 Доступные модели:')
    for model, display_name in MODEL_DISPLAY_NAMES.items():
        default_mark = ' (по умолчанию)' if model == DEFAULT_MODEL else ''
        print(f'   • {display_name}{default_mark}')
    print('📱 Найди бота в Telegram и отправь /start')
    print('🛑 Для остановки нажми Ctrl+C')
    print('=' * 60)

    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        print('\n👋 Бот остановлен')


if __name__ == '__main__':
    main()