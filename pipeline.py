"""
Единый пайплайн: URL → Парсинг → Конспект.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README репозиториев)
    - infostart.ru (статьи и публикации по 1С)

Поддерживаемые модели:
    - gemma3:12b (локальная, Ollama) — по умолчанию
    - gpt-3.5-turbo (OpenAI)
    - gpt-4 (OpenAI)

Example:
    Командная строка:
        python pipeline.py https://habr.com/ru/articles/123456/
        python pipeline.py https://habr.com/ru/articles/123456/ gpt-4
        python pipeline.py https://github.com/anthropics/anthropic-cookbook gemma3:12b
        python pipeline.py https://infostart.ru/public/886103/

    Интерактивный режим:
        python pipeline.py
"""

import json
import os
import sys
from datetime import datetime

# Импортируем наши модули
from scraper import get_article
from summarizer import generate_summary, save_summary_to_file, AVAILABLE_MODELS, DEFAULT_MODEL
from database import init_db, article_exists, save_article, update_article

# Публичный API модуля
__all__ = ['process_article', 'ensure_directories']

# Конфигурация путей
DATA_DIR: str = 'data'
PARSED_DIR: str = os.path.join(DATA_DIR, 'parsed_articles')
CONSPECT_DIR: str = 'conspect'

# Поддерживаемые источники
SUPPORTED_SOURCES: dict[str, str] = {
    'habr.com': 'habr',
    'github.com': 'github',
    'infostart.ru': 'infostart',
}


def ensure_directories() -> None:
    """
    Создаёт необходимые папки, если их нет.

    Создаваемые директории:
        - data/
        - data/parsed_articles/
        - conspect/
    """
    for directory in [DATA_DIR, PARSED_DIR, CONSPECT_DIR]:
        if not os.path.exists(directory):
            os.makedirs(directory)
            print(f'📁 Создана папка: {directory}')


def is_supported_url(url: str) -> bool:
    """
    Проверяет, поддерживается ли URL.

    Args:
        url: URL для проверки.

    Returns:
        True если источник поддерживается, иначе False.
    """
    return any(source in url for source in SUPPORTED_SOURCES)


def get_source_name(url: str) -> str:
    """
    Определяет название источника по URL.

    Args:
        url: URL статьи или репозитория.

    Returns:
        Название источника ('habr', 'github') или 'unknown'.
    """
    for domain, name in SUPPORTED_SOURCES.items():
        if domain in url:
            return name
    return 'unknown'


def generate_filename_from_url(url: str) -> str:
    """
    Генерирует имя файла из URL.

    Args:
        url: URL статьи или репозитория.

    Returns:
        Имя файла без расширения.

    Examples:
        >>> generate_filename_from_url('https://habr.com/ru/articles/984968/')
        'habr_984968'
        >>> generate_filename_from_url('https://github.com/anthropics/cookbook')
        'github_anthropics_cookbook'
        >>> generate_filename_from_url('https://infostart.ru/public/886103/')
        'infostart_886103'
    """
    source = get_source_name(url)

    if source == 'habr':
        # Извлекаем ID статьи из URL Хабра
        parts = url.rstrip('/').split('/')
        article_id = parts[-1] if parts[-1].isdigit() else 'unknown'
        return f'{source}_{article_id}'

    elif source == 'github':
        # Извлекаем owner/repo из URL GitHub
        parts = url.rstrip('/').split('/')
        if len(parts) >= 2:
            owner = parts[-2]
            repo = parts[-1]
            return f'{source}_{owner}_{repo}'
        return f'{source}_unknown'

    elif source == 'infostart':
        # Извлекаем ID статьи из URL InfoStart
        # Примеры: /public/123456/ или /1c/articles/123456/
        parts = url.rstrip('/').split('/')
        article_id = parts[-1] if parts[-1].isdigit() else 'unknown'
        return f'{source}_{article_id}'

    return f'{source}_unknown'


def save_parsed_data(article_data: dict, filename: str) -> str:
    """
    Сохраняет распарсенные данные в JSON.

    Args:
        article_data: Словарь с данными статьи.
        filename: Имя файла без расширения.

    Returns:
        Путь к сохранённому файлу.
    """
    # Добавляем метаданные
    article_data['parsed_at'] = datetime.now().isoformat()

    # Формируем путь
    file_path = os.path.join(PARSED_DIR, f'{filename}.json')

    # Сохраняем
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(article_data, f, ensure_ascii=False, indent=2)

    print(f'💾 Данные сохранены: {file_path}')
    return file_path


def format_article_info(article_data: dict) -> None:
    """
    Выводит информацию о распарсенной статье.

    Args:
        article_data: Словарь с данными статьи.
    """
    source = article_data.get('source', 'unknown')

    print(f'✅ Контент распарсен: {article_data.get("title", "Без названия")}')
    print(f'   Источник: {source}')
    print(f'   Автор: {article_data.get("author", "Не указан")}')

    # Дополнительные поля в зависимости от источника
    if source == 'habr':
        print(f'   Дата: {article_data.get("date", "Не указана")}')
    elif source == 'github':
        print(f'   Звёзды: {article_data.get("stars", "0")}')
        print(f'   Язык: {article_data.get("language", "Не определён")}')
        if article_data.get('description'):
            print(f'   Описание: {article_data.get("description")[:50]}...')
    elif source == 'infostart':
        # Для InfoStart особых полей не выводим, только основные
        pass

    print(f'   Длина текста: {article_data.get("content_length", 0)} символов')


def get_provider_name(model: str) -> str:
    """
    Возвращает читаемое название провайдера.

    Args:
        model: Название модели.

    Returns:
        Название провайдера для отображения.
    """
    provider = AVAILABLE_MODELS.get(model, 'ollama')
    if provider == 'ollama':
        return 'Ollama (локальная)'
    return 'OpenAI'


def process_article(
    url: str,
    model: str = DEFAULT_MODEL,
    save_json: bool = True,
    user_id: int | None = None,
    skip_cache: bool = False,
) -> str | None:
    """
    Основная функция пайплайна: URL → Конспект.

    Args:
        url: URL статьи или репозитория для обработки.
        model: Модель для генерации (по умолчанию — локальная Ollama).
        save_json: Сохранять ли промежуточный JSON с данными.
        user_id: Telegram user_id для привязки статьи.
        skip_cache: Пропустить проверку кеша (для принудительной перегенерации).

    Returns:
        Путь к файлу конспекта или None при ошибке.
    """
    # Инициализация БД (идемпотентная операция)
    init_db()
    print('\n' + '=' * 60)
    print('🚀 ЗАПУСК ПАЙПЛАЙНА')
    print('=' * 60)

    # Проверка поддержки URL
    if not is_supported_url(url):
        print(f'❌ Источник не поддерживается: {url}')
        print(f'   Поддерживаемые: {", ".join(SUPPORTED_SOURCES.keys())}')
        return None

    # ШАГ 1: Парсинг
    print('\n📥 ШАГ 1: Парсинг контента...')
    print('-' * 40)

    article_data = get_article(url)

    # Проверяем на ошибки парсинга
    if 'error' in article_data:
        print(f'❌ Ошибка парсинга: {article_data["error"]}')
        return None

    format_article_info(article_data)

    # ШАГ 2: Сохранение промежуточных данных (опционально)
    if save_json:
        filename = generate_filename_from_url(url)
        save_parsed_data(article_data, filename)

    # ШАГ 3: Генерация конспекта
    provider_name = get_provider_name(model)
    print(f'\n🧠 ШАГ 2: Генерация конспекта...')
    print(f'   Модель: {model} ({provider_name})')
    print('-' * 40)

    summary = generate_summary(article_data, model)

    # Проверяем на ошибки генерации
    if summary.startswith('❌'):
        print(f'Ошибка генерации: {summary}')
        return None

    print('✅ Конспект сгенерирован!')

    # ШАГ 4: Сохранение конспекта
    print('\n💾 ШАГ 3: Сохранение конспекта...')
    print('-' * 40)

    article_title = article_data.get('title', 'Без_названия')
    saved_path = save_summary_to_file(summary, article_title, CONSPECT_DIR)

    # ШАГ 5: Сохранение в БД
    print('\n🗄️ ШАГ 4: Сохранение в базу данных...')
    print('-' * 40)

    # Проверяем, есть ли уже запись (при skip_cache=True)
    if article_exists(url):
        # Обновляем существующую запись
        update_article(url=url, summary=summary, model=model)
        print('✅ Обновлено в БД')
    else:
        # Создаём новую запись
        article_id = save_article(
            article_data=article_data,
            summary=summary,
            model=model,
            user_id=user_id,
        )
        print(f'✅ Сохранено в БД: article_id={article_id}')

    # Итог
    print('\n' + '=' * 60)
    print('✨ ПАЙПЛАЙН ЗАВЕРШЁН УСПЕШНО')
    print('=' * 60)
    print(f'📄 Исходный URL: {url}')
    print(f'🤖 Использована модель: {model}')
    print(f'📚 Конспект сохранён: {saved_path}')

    return saved_path


def interactive_mode() -> None:
    """Интерактивный режим работы с вводом URL и выбором модели."""
    print('\n' + '=' * 60)
    print('🤖 АГЕНТ ДЛЯ ИЗУЧЕНИЯ ИИ — ГЕНЕРАТОР КОНСПЕКТОВ')
    print('=' * 60)

    # Показываем поддерживаемые источники
    print('\n📌 Поддерживаемые источники:')
    for domain in SUPPORTED_SOURCES:
        print(f'   ✅ {domain}')

    # Запрашиваем URL
    url = input('\n🔗 Введите URL: ').strip()

    if not url:
        print('❌ URL не указан. Завершение.')
        return

    # Валидация URL
    if not is_supported_url(url):
        print(f'⚠️ Источник не поддерживается.')
        print(f'   Поддерживаемые: {", ".join(SUPPORTED_SOURCES.keys())}')
        return

    # Выбор модели
    print('\n📊 Выбор модели:')
    print('   1 — gemma3:12b (локальная, Ollama)')
    print('   2 — gpt-3.5-turbo (OpenAI)')
    print('   3 — gpt-4 (OpenAI)')

    model_choice = input('   Ваш выбор (Enter = 1, локальная): ').strip()

    if model_choice == '2':
        model = 'gpt-3.5-turbo'
    elif model_choice == '3':
        model = 'gpt-4'
    else:
        model = DEFAULT_MODEL

    # Запуск пайплайна
    result = process_article(url, model=model)

    if result:
        # Предлагаем просмотреть результат
        view_choice = input('\n👀 Показать конспект? (y/n, Enter = да): ').strip().lower()
        if view_choice in ['', 'y', 'yes', 'да']:
            print('\n' + '=' * 60)
            print('📚 СОДЕРЖИМОЕ КОНСПЕКТА:')
            print('=' * 60)
            with open(result, 'r', encoding='utf-8') as f:
                print(f.read())


def main() -> None:
    """
    Точка входа — поддерживает два режима.

    Режимы запуска:
        1. Командная строка: python pipeline.py <URL> [model]
        2. Интерактивный: python pipeline.py
    """
    # Создаём необходимые папки и инициализируем БД
    ensure_directories()
    init_db()

    # Проверяем аргументы командной строки
    if len(sys.argv) > 1:
        # Режим командной строки
        url = sys.argv[1]

        # Опциональный параметр модели (теперь по умолчанию локальная)
        model = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL

        process_article(url, model=model)
    else:
        # Интерактивный режим
        interactive_mode()


if __name__ == '__main__':
    main()