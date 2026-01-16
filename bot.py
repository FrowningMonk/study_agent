"""
Telegram-бот для агента изучения ИИ.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README репозиториев)

Команды бота:
    /start — Приветствие и инструкция
    /help  — Справка по использованию
    <URL>  — Отправить ссылку → получить конспект

Example:
    python bot.py
"""

import os

import telebot
from dotenv import load_dotenv

from pipeline import ensure_directories, process_article

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

💡 Попробуй отправить ссылку прямо сейчас!

/help — справка по командам"""

MSG_HELP: str = """📖 Справка по боту

Команды:
/start — Приветствие
/help — Эта справка

Использование:
Отправь ссылку, например:
• https://habr.com/ru/articles/984968/
• https://github.com/anthropics/anthropic-cookbook

Поддерживаемые источники:
✅ Хабр (habr.com) — статьи
✅ GitHub (github.com) — README репозиториев

Время обработки:
~30-60 секунд (зависит от размера контента)

Возможные ошибки:
• Контент не найден — проверь ссылку
• Таймаут — попробуй ещё раз
• Ошибка API — подожди минуту и повтори"""

MSG_UNSUPPORTED_SOURCE: str = """⚠️ Источник не поддерживается.

Поддерживаемые источники:
• habr.com — статьи
• github.com — README репозиториев

Отправь ссылку с одного из этих сайтов."""

MSG_PROCESSING_HABR: str = """⏳ Обрабатываю статью...

Это займёт 30-60 секунд."""

MSG_PROCESSING_GITHUB: str = """⏳ Анализирую репозиторий...

Это займёт 30-60 секунд."""

MSG_SUCCESS: str = '✅ Конспект готов!'

MSG_ERROR_GENERIC: str = """❌ Не удалось обработать контент.

Возможные причины:
• Контент не найден
• Ошибка парсинга
• Проблема с API

Проверь ссылку и попробуй ещё раз."""

MSG_ERROR_WITH_DETAILS: str = """❌ Произошла ошибка: {error}

Попробуй ещё раз через минуту."""

MSG_UNKNOWN_COMMAND: str = """🤔 Не понял команду.

Отправь ссылку на статью или репозиторий, например:
• https://habr.com/ru/articles/984968/
• https://github.com/anthropics/anthropic-cookbook

/help — справка по командам"""


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


def is_url(text: str | None) -> bool:
    """
    Проверяет, является ли текст URL-адресом.

    Args:
        text: Текст для проверки.

    Returns:
        True если текст — URL, иначе False.
    """
    if not text:
        return False
    text = text.strip()
    return text.startswith('http://') or text.startswith('https://')


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


def get_processing_message(url: str) -> str:
    """
    Возвращает сообщение о начале обработки в зависимости от источника.

    Args:
        url: URL для обработки.

    Returns:
        Текст сообщения.
    """
    source_type = get_source_type(url)
    if source_type == 'github':
        return MSG_PROCESSING_GITHUB
    return MSG_PROCESSING_HABR


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
        welcome_text = MSG_WELCOME.format(name=user_name)
        bot.reply_to(message, welcome_text)

    @bot.message_handler(commands=['help'])
    def handle_help(message: telebot.types.Message) -> None:
        """
        Обработчик команды /help.

        Args:
            message: Входящее сообщение от пользователя.
        """
        bot.reply_to(message, MSG_HELP)

    @bot.message_handler(func=lambda message: is_url(message.text))
    def handle_url(message: telebot.types.Message) -> None:
        """
        Обработчик ссылок — основная логика.

        Args:
            message: Входящее сообщение со ссылкой.
        """
        url = message.text.strip()
        user_id = message.from_user.id

        print(f'📨 Получена ссылка от {user_id}: {url}')

        # Проверяем поддержку источника
        if not is_supported_url(url):
            bot.reply_to(message, MSG_UNSUPPORTED_SOURCE)
            return

        # Отправляем статус "печатает..."
        bot.send_chat_action(message.chat.id, 'typing')

        # Уведомляем о начале обработки
        processing_msg = get_processing_message(url)
        status_msg = bot.reply_to(message, processing_msg)

        try:
            # Запускаем пайплайн
            result_path = process_article(url, model='gpt-3.5-turbo', save_json=True)

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
            _send_summary(message.chat.id, summary, url)

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


def _send_summary(chat_id: int, summary: str, url: str) -> None:
    """
    Отправляет конспект пользователю.

    Args:
        chat_id: ID чата.
        summary: Текст конспекта.
        url: Ссылка на исходную статью.
    """
    header = MSG_SUCCESS + f'\nИсточник: {url}\n\n'

    if len(header) + len(summary) <= 4096:
        bot.send_message(chat_id, header + summary)
    else:
        bot.send_message(chat_id, MSG_SUCCESS)
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
    print('📱 Найди бота в Telegram и отправь /start')
    print('🛑 Для остановки нажми Ctrl+C')
    print('=' * 60)

    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        print('\n👋 Бот остановлен')


if __name__ == '__main__':
    main()