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

load_dotenv()

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

user_models: dict[int, str] = {}

# Сообщения
MSG_START: str = """👋 Привет!

Я пока не знаю что я такое и зачем.
Но мне дали возможность парсить страницы из источников и пересказывать их содержание.

🔧 Как использовать:
1. Отправь ссылку
2. Подожди 30-60 секунд
3. Я расскажу о чем ссылка.

📌 Поддерживаемые источники:
• habr.com — технические статьи
• github.com — README репозиториев
• infostart.ru — база знаний

Сменить модель: /model

💡 Попробуй отправить ссылку прямо сейчас!

/help — справка по командам"""
MSG_HELP = "Команды:\n/start - начало\n/model - выбор модели\n\nПоддерживаемые источники: habr.com, github.com, infostart.ru"
MSG_PROCESSING = "Обрабатываю..."
MSG_ERROR = "Ошибка: {error}"
MSG_UNSUPPORTED = "Я такие ссылки пока не понимаю. Поддерживаемые источники: habr.com, github.com, infostart.ru"
MSG_MODEL_UNAVAILABLE = "Модель {model} недоступна: {error}\n\nВыбери другую модель: /model"
MSG_UNKNOWN = "Без ссылки работать бессмысленно. /help для справки."
MSG_MODEL_SELECT = "Текущая модель: {current_model}\n\nВыбери модель:"
MSG_MODEL_CHANGED = "Модель изменена: {model}"


# Проверяем наличие токена перед запуском
if not TELEGRAM_TOKEN:
    print('=' * 60)
    print('ОШИБКА: Не найден TELEGRAM_BOT_TOKEN в файле .env')
    print('=' * 60)
    print('Создайте файл .env и добавьте строку:')
    print('TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather')
    print('=' * 60)
    exit(1)

# Создаем бота (токен точно существует)
bot = telebot.TeleBot(TELEGRAM_TOKEN)


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


def get_user_model(user_id: int) -> str:
    """
    Возвращает выбранную пользователем модель.

    Если пользователь еще не выбирал модель - возвращает модель по умолчанию.
    Это позволяет легко изменить способ хранения предпочтений в будущем
    (например, сохранение в базу данных).

    Args:
        user_id: Telegram ID пользователя

    Returns:
        Название модели для генерации конспектов
    """
    return user_models.get(user_id, DEFAULT_MODEL)


def create_model_keyboard(current_model: str) -> types.InlineKeyboardMarkup:
    """
    Создает inline-клавиатуру для выбора модели.

    Отображает список доступных моделей с галочкой ✓ у текущей выбранной модели.
    Каждая кнопка содержит callback_data в формате "model:название_модели".

    Args:
        current_model: Название текущей модели пользователя

    Returns:
        InlineKeyboardMarkup с кнопками выбора моделей
    """
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for model_name in AVAILABLE_MODELS.keys():
        display_name = model_name
        if model_name == current_model:
            display_name = f'✓ {display_name}'
        button = types.InlineKeyboardButton(
            text=display_name,
            callback_data=f'model:{model_name}',
        )
        keyboard.add(button)
    return keyboard


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


# Обработчики команд и сообщений
@bot.message_handler(commands=['start'])
def handle_start(message: telebot.types.Message) -> None:
    """Обрабатывает команду /start - приветствие и инструкции."""
    bot.reply_to(message, MSG_START)


@bot.message_handler(commands=['help'])
def handle_help(message: telebot.types.Message) -> None:
    """Обрабатывает команду /help - справка по использованию бота."""
    bot.reply_to(message, MSG_HELP)


@bot.message_handler(commands=['model'])
def handle_model(message: telebot.types.Message) -> None:
    """Обрабатывает команду /model - показывает меню выбора модели."""
    user_id = message.from_user.id
    current_model = get_user_model(user_id)
    keyboard = create_model_keyboard(current_model)
    bot.send_message(
        message.chat.id,
        MSG_MODEL_SELECT.format(current_model=current_model),
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('model:'))
def handle_model_callback(call: telebot.types.CallbackQuery) -> None:
    """
    Обрабатывает выбор модели через inline keyboard.

    Callback data имеет формат: "model:название_модели"
    Например: "model:gemma3:12b"
    """
    user_id = call.from_user.id
    # Извлекаем название модели из callback data (формат: "model:gemma3:12b")
    # Разбиваем по первому ':', берем вторую часть
    model = call.data.split(':', 1)[1]
    user_models[user_id] = model

    bot.edit_message_text(
        MSG_MODEL_CHANGED.format(model=model),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )
    bot.answer_callback_query(call.id, f'Выбрана модель: {model}')
    print(f'Пользователь {user_id} выбрал модель: {model}')


@bot.message_handler(func=lambda message: extract_url(message.text) is not None)
def handle_url(message: telebot.types.Message) -> None:
    """
    Обрабатывает сообщения с URL - создает конспект статьи.

    Извлекает URL из сообщения, проверяет поддержку источника,
    обрабатывает статью с помощью выбранной модели и отправляет конспект.
    """
    url = extract_url(message.text)
    if not url:
        bot.reply_to(message, MSG_UNKNOWN)
        return

    user_id = message.from_user.id
    model = get_user_model(user_id)

    print(f'Получена ссылка от {user_id}: {url}')
    print(f'Модель: {model}')

    if not is_supported_url(url):
        bot.reply_to(message, MSG_UNSUPPORTED)
        return

    # Проверяем доступность модели
    is_available, error_message = check_model_availability(model)
    if not is_available:
        error_text = MSG_MODEL_UNAVAILABLE.format(model=model, error=error_message)
        bot.reply_to(message, error_text)
        print(f'Модель {model} недоступна для {user_id}: {error_message}')
        return

    bot.send_chat_action(message.chat.id, 'typing')
    status_msg = bot.reply_to(message, MSG_PROCESSING)

    try:
        result_path = process_article(url, model=model, save_json=True)

        if result_path is None:
            bot.edit_message_text(
                MSG_ERROR.format(error='Не удалось обработать статью'),
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
            )
            return

        with open(result_path, 'r', encoding='utf-8') as f:
            summary = f.read()

        # Удаляем статусное сообщение
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        except Exception:
            pass

        # Отправляем конспект
        header = f'Готово!\nМодель: {model}\nИсточник: {url}\n\n'
        if len(header) + len(summary) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            bot.send_message(message.chat.id, header + summary)
        else:
            bot.send_message(message.chat.id, f'Готово! Модель: {model}')
            send_long_message(message.chat.id, summary)

        print(f'Конспект отправлен пользователю {user_id}')

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
        print(f'Ошибка для {user_id}: {e}')


@bot.message_handler(func=lambda message: True)
def handle_unknown(message: telebot.types.Message) -> None:
    """Обрабатывает все остальные сообщения - отправляет подсказку."""
    bot.reply_to(message, MSG_UNKNOWN)


def main() -> None:
    """Запуск бота."""
    ensure_directories()

    print('=' * 60)
    print('TELEGRAM-БОТ АГЕНТА ДЛЯ ИЗУЧЕНИЯ ИИ')
    print('=' * 60)
    print('Бот запущен и готов к работе!')
    print('Поддерживаемые источники:')
    for domain in SUPPORTED_SOURCES:
        print(f'  - {domain}')
    print('Доступные модели:')
    for model in AVAILABLE_MODELS.keys():
        default_mark = ' (по умолчанию)' if model == DEFAULT_MODEL else ''
        print(f'  - {model}{default_mark}')
    print('Найди бота в Telegram и отправь /start')
    print('Для остановки нажми Ctrl+C')
    print('=' * 60)

    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        print('\nБот остановлен')


if __name__ == '__main__':
    main()
