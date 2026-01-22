"""
Модуль работы с SQLite базой данных.

Функции:
    - init_db() - инициализация БД и таблиц
    - article_exists(url) - проверка наличия статьи в БД
    - get_cached_summary(url) - получение сохранённого конспекта
    - save_article(...) - сохранение статьи с конспектом
    - update_article(...) - обновление конспекта существующей статьи

Example:
    >>> from database import init_db, article_exists, save_article
    >>> init_db()
    >>> if not article_exists(url):
    ...     save_article(article_data, summary, model, user_id)
"""

import os
import sqlite3
from datetime import datetime

# Публичный API модуля
__all__ = [
    'init_db',
    'article_exists',
    'get_cached_summary',
    'get_article_by_url',
    'save_article',
    'update_article',
]

# Путь к базе данных
DATA_DIR = 'data'
DB_PATH = os.path.join(DATA_DIR, 'study_agent.db')

# SQL для создания таблицы
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,

    -- Дата публикации (NULL для GitHub)
    published_date TEXT,

    -- GitHub-специфичные поля
    github_stars TEXT,
    github_language TEXT,
    github_description TEXT,

    -- Контент
    content TEXT NOT NULL,
    summary TEXT,

    -- Служебные поля
    model_used TEXT,
    user_id INTEGER,
    processed_at TEXT NOT NULL
);
"""

CREATE_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url);
CREATE INDEX IF NOT EXISTS idx_articles_user_id ON articles(user_id);
"""


def _get_connection() -> sqlite3.Connection:
    """
    Создаёт подключение к базе данных.

    Returns:
        sqlite3.Connection с row_factory = sqlite3.Row
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Инициализирует базу данных, создаёт таблицы и индексы если их нет.

    Вызывается при старте приложения (bot.py, pipeline.py).
    Операция идемпотентна — безопасно вызывать многократно.
    """
    # Создаём папку data если её нет
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.executescript(CREATE_TABLE_SQL)
        cursor.executescript(CREATE_INDEXES_SQL)
        conn.commit()
    finally:
        conn.close()


def article_exists(url: str, user_id: int | None = None) -> bool:
    """
    Проверяет, была ли уже обработана статья.

    Args:
        url: URL статьи
        user_id: ID пользователя (не используется пока, для будущего расширения)

    Returns:
        True если статья уже есть в базе
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM articles WHERE url = ?', (url,))
        return cursor.fetchone() is not None
    finally:
        conn.close()


def get_cached_summary(url: str) -> str | None:
    """
    Получает конспект из кеша если статья уже обработана.

    Args:
        url: URL статьи

    Returns:
        Текст конспекта или None если не найден
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT summary FROM articles WHERE url = ?', (url,))
        row = cursor.fetchone()
        return row['summary'] if row else None
    finally:
        conn.close()


def get_article_by_url(url: str) -> dict | None:
    """
    Получает полную информацию о статье по URL.

    Args:
        url: URL статьи

    Returns:
        dict с полями статьи или None если не найдена
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM articles WHERE url = ?', (url,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_article(
    article_data: dict,
    summary: str,
    model: str,
    user_id: int | None = None,
) -> int:
    """
    Сохраняет статью с конспектом в базу.

    Args:
        article_data: dict из scraper.get_article()
        summary: сгенерированный конспект
        model: использованная модель
        user_id: telegram user_id

    Returns:
        ID созданной записи

    Note:
        Для GitHub поле published_date будет NULL.
        Поля github_* заполняются только для source='github'.
    """
    source = article_data.get('source', 'unknown')

    # Дата публикации (только для статей, не для GitHub)
    published_date = None
    if source in ('habr', 'infostart'):
        published_date = article_data.get('date')

    # GitHub-специфичные поля
    github_stars = None
    github_language = None
    github_description = None

    if source == 'github':
        github_stars = article_data.get('stars')
        github_language = article_data.get('language')
        github_description = article_data.get('description')

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO articles (
                url, source, title, author,
                published_date,
                github_stars, github_language, github_description,
                content, summary,
                model_used, user_id, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_data.get('url'),
                source,
                article_data.get('title', 'Без названия'),
                article_data.get('author'),
                published_date,
                github_stars,
                github_language,
                github_description,
                article_data.get('content', ''),
                summary,
                model,
                user_id,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_article(
    url: str,
    summary: str,
    model: str,
) -> bool:
    """
    Обновляет конспект существующей статьи.

    Используется при перегенерации конспекта.

    Args:
        url: URL статьи
        summary: новый конспект
        model: использованная модель

    Returns:
        True если статья найдена и обновлена, False если не найдена
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE articles
            SET summary = ?, model_used = ?, processed_at = ?
            WHERE url = ?
            """,
            (summary, model, datetime.now().isoformat(), url),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
