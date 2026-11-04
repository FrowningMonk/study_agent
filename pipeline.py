"""
Единый пайплайн: URL → Парсинг → Конспект.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README репозиториев)
    - infostart.ru (статьи и публикации по 1С)

Поддерживаемые провайдеры: polza (основной), ollama (резерв).
Название модели задаётся пользователем.

Example:
    python pipeline.py https://habr.com/ru/articles/123456/
    python pipeline.py https://habr.com/ru/articles/123456/ deepseek/deepseek-v4.1-flash-1017 polza
    python pipeline.py https://github.com/anthropics/anthropic-cookbook gemma3:12b ollama
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
from summarizer import generate_summary, DEFAULT_MODEL, DEFAULT_PROVIDER
from database import (
    init_db, article_exists, save_article, update_article, get_article_by_url,
    get_article_by_habr_id, update_article_by_id,
)
import habr_api

# Публичный API модуля
__all__ = ['process_article', 'ensure_directories', 'save_article_to_db']

# Конфигурация путей
DATA_DIR: str = 'data'

# Поддерживаемые источники. Habr is recognized only by the strict
# habr_api.parse_article_id (Decision 3); the others keep substring detection.
SUPPORTED_SOURCES: dict[str, str] = {
    'habr.com': 'habr',
    'github.com': 'github',
    'infostart.ru': 'infostart',
}
_HABR_DOMAIN = 'habr.com'


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
    return get_source_name(url) != 'unknown'


def get_source_name(url: str) -> str:
    """
    Определяет название источника по URL.

    Args:
        url: URL статьи или репозитория.

    Returns:
        Название источника ('habr', 'github') или 'unknown'.
    """
    if habr_api.parse_article_id(url) is not None:
        return 'habr'
    # A URL that mentions habr.com but is not a strict Habr article URL
    # (spoofed host, hub page, /sandbox/, ...) is unsupported, never fetched.
    # Deliberately fail-closed for any host: scraper.get_article routes on the
    # 'habr.com' substring first, so e.g. a github.com path mentioning habr.com
    # would otherwise reach the Habr parser (it was routed as Habr before, too).
    if _HABR_DOMAIN in url.lower():
        return 'unknown'
    for domain, name in SUPPORTED_SOURCES.items():
        if domain != _HABR_DOMAIN and domain in url:
            return name
    return 'unknown'


def process_article(
    url: str,
    model: str = DEFAULT_MODEL,
    provider: str = DEFAULT_PROVIDER,
    user_id: int | None = None,
    skip_cache: bool = False,
) -> tuple[str, dict] | None:
    """
    Основная функция пайплайна: URL → Конспект.

    Примечание: сохранение в БД НЕ выполняется. Используйте save_article_to_db()
    для явного сохранения после подтверждения пользователем.

    Args:
        url: URL статьи или репозитория для обработки.
        model: Модель для генерации.
        provider: Провайдер ('polza', 'ollama').
        user_id: Telegram user_id для привязки статьи.
        skip_cache: Пропустить проверку кеша (для принудительной перегенерации).

    Returns:
        Кортеж (summary: str, article_data: dict) или None при ошибке.
    """
    start_time = time.perf_counter()

    if not is_supported_url(url):
        logger.warning("Неподдерживаемый источник: %s", url)
        return None

    # Habr: fetch only the canonical URL built from the integer id; the
    # user-supplied URL is never fetched.
    habr_id = habr_api.parse_article_id(url)
    fetch_url = habr_api.canonical_url(habr_id) if habr_id is not None else url
    article_data = get_article(fetch_url)

    if 'error' in article_data:
        logger.error("Ошибка парсинга: %s", article_data["error"])
        return None

    content_length = len(article_data.get('content', ''))
    logger.info('Страница спарсена: url=%s, title=%s, content_length=%d',
                url, article_data.get('title', 'N/A')[:50], content_length)

    summary = generate_summary(article_data, model, provider)

    if summary.startswith('❌'):
        logger.error("Ошибка генерации: %s", summary)
        return None

    elapsed = time.perf_counter() - start_time
    logger.info('Summary сгенерирован: model=%s, provider=%s, url=%s, length=%d, time=%.2fs',
                model, provider, url, len(summary), elapsed)
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
        ID сохранённой (или обновлённой существующей) статьи.
    """
    check_url = url or article_data.get('url')
    habr_id = habr_api.parse_article_id(check_url)
    if habr_id is None:
        habr_id = habr_api.parse_article_id(article_data.get('url'))
    if habr_id is not None:
        # Habr identity is the id: update the earliest row, never add a utm duplicate
        existing = get_article_by_habr_id(habr_id)
        if existing:
            update_article_by_id(existing['id'], summary=summary, model=model)
            logger.info("Обновлено в БД: article_id=%d (habr_id=%d)", existing['id'], habr_id)
            return existing['id']
        # Store the user-supplied URL, not the canonical one; copy, never mutate the caller's dict
        article_data = {**article_data, 'url': check_url}

    # Проверяем, есть ли уже запись
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
        habr_id=habr_id,
    )
    logger.info("Сохранено в БД: article_id=%d", article_id)
    return article_id


def main() -> None:
    """
    Точка входа: python pipeline.py <URL> [model] [provider]
    """
    ensure_directories()
    init_db()

    if len(sys.argv) < 2:
        print("Использование: python pipeline.py <URL> [model] [provider]")
        sys.exit(1)

    url = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
    provider = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_PROVIDER
    process_article(url, model=model, provider=provider)


if __name__ == '__main__':
    main()