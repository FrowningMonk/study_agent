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
    python pipeline.py https://habr.com/ru/articles/123456/
    python pipeline.py https://habr.com/ru/articles/123456/ gpt-4
    python pipeline.py https://github.com/anthropics/anthropic-cookbook gemma3:12b
    python pipeline.py https://infostart.ru/public/886103/
"""

import logging
import os
import sys
import time

# Логгер модуля
logger = logging.getLogger(__name__)

# Импортируем наши модули
from scraper import get_article
from summarizer import generate_summary, DEFAULT_MODEL
from database import init_db, article_exists, save_article, update_article, get_article_by_url

# Публичный API модуля
__all__ = ['process_article', 'ensure_directories', 'save_article_to_db']

# Конфигурация путей
DATA_DIR: str = 'data'

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
    """
    for directory in [DATA_DIR, 'ideas_md']:
        if not os.path.exists(directory):
            os.makedirs(directory)
            logger.info("Создана папка: %s", directory)


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


def process_article(
    url: str,
    model: str = DEFAULT_MODEL,
    user_id: int | None = None,
    skip_cache: bool = False,
) -> tuple[str, dict] | None:
    """
    Основная функция пайплайна: URL → Конспект.

    Примечание: сохранение в БД НЕ выполняется. Используйте save_article_to_db()
    для явного сохранения после подтверждения пользователем.

    Args:
        url: URL статьи или репозитория для обработки.
        model: Модель для генерации (по умолчанию — локальная Ollama).
        user_id: Telegram user_id для привязки статьи.
        skip_cache: Пропустить проверку кеша (для принудительной перегенерации).

    Returns:
        Кортеж (summary: str, article_data: dict) или None при ошибке.
    """
    start_time = time.perf_counter()

    if not is_supported_url(url):
        logger.warning("Неподдерживаемый источник: %s", url)
        return None

    article_data = get_article(url)

    if 'error' in article_data:
        logger.error("Ошибка парсинга: %s", article_data["error"])
        return None

    content_length = len(article_data.get('content', ''))
    logger.info('Страница спарсена: url=%s, title=%s, content_length=%d',
                url, article_data.get('title', 'N/A')[:50], content_length)

    summary = generate_summary(article_data, model)

    if summary.startswith('❌'):
        logger.error("Ошибка генерации: %s", summary)
        return None

    elapsed = time.perf_counter() - start_time
    logger.info('Summary сгенерирован: model=%s, url=%s, length=%d, time=%.2fs',
                model, url, len(summary), elapsed)
    return summary, article_data


def save_article_to_db(
    article_data: dict,
    summary: str,
    model: str,
    user_id: int | None = None,
    url: str | None = None,
) -> int:
    """
    Сохраняет статью в базу данных.

    Используется для явного сохранения после подтверждения пользователем.

    Args:
        article_data: Словарь с данными статьи из scraper.
        summary: Сгенерированный конспект.
        model: Использованная модель.
        user_id: Telegram user_id.
        url: URL статьи (опционально, для проверки дубликата).

    Returns:
        ID сохранённой статьи или None если статья уже существует.
    """
    # Проверяем, есть ли уже запись
    check_url = url or article_data.get('url')
    if check_url and article_exists(check_url):
        # Статья уже существует - возвращаем ID
        existing = get_article_by_url(check_url)
        if existing:
            # Обновляем конспект
            update_article(url=check_url, summary=summary, model=model)
            logger.info("Обновлено в БД: article_id=%d", existing["id"])
            return existing['id']

    # Создаём новую запись
    article_id = save_article(
        article_data=article_data,
        summary=summary,
        model=model,
        user_id=user_id,
    )
    logger.info("Сохранено в БД: article_id=%d", article_id)
    return article_id


def main() -> None:
    """
    Точка входа: python pipeline.py <URL> [model]
    """
    ensure_directories()
    init_db()

    if len(sys.argv) < 2:
        print("Использование: python pipeline.py <URL> [model]")
        sys.exit(1)

    url = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
    process_article(url, model=model)


if __name__ == '__main__':
    main()