"""
Модуль работы с SQLite базой данных.

Функции для статей (articles):
    - init_db() - инициализация БД и таблиц
    - article_exists(url) - проверка наличия статьи в БД
    - get_cached_summary(url) - получение сохранённого конспекта
    - get_article_by_url(url) - получение статьи по URL
    - get_article_by_id(id) - получение статьи по ID
    - save_article(...) - сохранение статьи с конспектом
    - update_article(...) - обновление конспекта существующей статьи

Функции для идей (ideas):
    - create_idea(...) - создание новой идеи
    - get_user_ideas(user_id) - получение всех идей пользователя
    - get_idea_by_id(idea_id, user_id) - получение одной идеи (с проверкой ownership)
    - update_idea(...) - обновление идеи (с проверкой ownership)
    - delete_idea(...) - удаление идеи (с проверкой ownership)

Функции для связи статей и идей (idea_articles):
    - link_article_to_idea(...) - привязка статьи к идее
    - unlink_article_from_idea(...) - отвязка статьи от идеи
    - get_articles_by_idea(...) - получение статей идеи
    - get_ideas_by_article(...) - получение идей статьи

Example для статей:
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
    # Таблица articles
    'init_db',
    'article_exists',
    'get_cached_summary',
    'get_article_by_url',
    'get_article_by_id',
    'save_article',
    'update_article',
    # Таблица ideas
    'create_idea',
    'get_user_ideas',
    'get_idea_by_id',
    'update_idea',
    'delete_idea',
    # Таблица idea_articles
    'link_article_to_idea',
    'unlink_article_from_idea',
    'get_articles_by_idea',
    'get_ideas_by_article',
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

# SQL для таблицы ideas
CREATE_IDEAS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

CREATE_IDEAS_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_ideas_user_id ON ideas(user_id);
"""

# SQL для таблицы idea_articles (связь статей и идей)
CREATE_IDEA_ARTICLES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS idea_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id INTEGER NOT NULL,
    article_id INTEGER NOT NULL,
    relevance_score REAL,
    relevance_reason TEXT,
    user_confirmed INTEGER DEFAULT 1,
    user_useful INTEGER,
    added_at TEXT NOT NULL,
    FOREIGN KEY (idea_id) REFERENCES ideas(id) ON DELETE CASCADE,
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
    UNIQUE (idea_id, article_id)
);
"""

CREATE_IDEA_ARTICLES_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_idea_articles_idea_id ON idea_articles(idea_id);
CREATE INDEX IF NOT EXISTS idx_idea_articles_article_id ON idea_articles(article_id);
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
    Операция идемпотентна - безопасно вызывать многократно.
    """
    # Создаём папку data если её нет
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.executescript(CREATE_TABLE_SQL)
        cursor.executescript(CREATE_INDEXES_SQL)
        cursor.executescript(CREATE_IDEAS_TABLE_SQL)
        cursor.executescript(CREATE_IDEAS_INDEXES_SQL)
        cursor.executescript(CREATE_IDEA_ARTICLES_TABLE_SQL)
        cursor.executescript(CREATE_IDEA_ARTICLES_INDEXES_SQL)
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


def get_article_by_id(article_id: int) -> dict | None:
    """
    Получает полную информацию о статье по ID.

    Args:
        article_id: ID статьи в таблице articles (поле id)

    Returns:
        dict с полями статьи или None если не найдена
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM articles WHERE id = ?', (article_id,))
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


# ========================
# Функции для работы с идеями (ideas)
# ========================


def create_idea(name: str, description: str | None, user_id: int) -> int:
    """
    Создаёт новую идею в базе данных.

    Args:
        name: название идеи
        description: описание идеи (может быть None)
        user_id: ID пользователя Telegram

    Returns:
        ID созданной идеи
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute(
            """
            INSERT INTO ideas (name, description, user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, description, user_id, now, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_user_ideas(user_id: int) -> list[dict]:
    """
    Получает все идеи пользователя.

    Args:
        user_id: ID пользователя Telegram

    Returns:
        Список словарей с полями идеи, отсортированный по дате создания (новые первые)
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, name, description, user_id, created_at, updated_at
            FROM ideas
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_idea_by_id(idea_id: int, user_id: int) -> dict | None:
    """
    Получает одну идею по её ID с проверкой ownership.

    Args:
        idea_id: ID идеи
        user_id: ID пользователя для проверки владения

    Returns:
        dict с полями идеи или None если не найдена или не принадлежит пользователю
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, name, description, user_id, created_at, updated_at
            FROM ideas
            WHERE id = ? AND user_id = ?
            """,
            (idea_id, user_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_idea(
    idea_id: int,
    user_id: int,
    name: str | None = None,
    description: str | None = None,
) -> bool:
    """
    Обновляет поля name и/или description идеи с проверкой ownership.

    Args:
        idea_id: ID идеи
        user_id: ID пользователя для проверки владения
        name: новое название (опционально)
        description: новое описание (опционально)

    Returns:
        True если обновление прошло успешно, False если идея не найдена или не принадлежит пользователю
    """
    # Собираем только те поля, которые нужно обновить
    updates = []
    params = []
    if name is not None:
        updates.append('name = ?')
        params.append(name)
    if description is not None:
        updates.append('description = ?')
        params.append(description)

    if not updates:
        return False

    updates.append('updated_at = ?')
    params.append(datetime.now().isoformat())
    params.extend([idea_id, user_id])

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            UPDATE ideas
            SET {', '.join(updates)}
            WHERE id = ? AND user_id = ?
            """,
            params,
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_idea(idea_id: int, user_id: int) -> bool:
    """
    Удаляет идею с проверкой ownership.

    Args:
        idea_id: ID идеи
        user_id: ID пользователя для проверки владения

    Returns:
        True если удаление прошло успешно, False если идея не найдена или не принадлежит пользователю
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM ideas WHERE id = ? AND user_id = ?',
            (idea_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ========================
# Функции для связи статей и идей (idea_articles)
# ========================


def link_article_to_idea(article_id: int, idea_id: int, user_id: int) -> bool:
    """
    Привязывает статью к идее с проверкой ownership.

    Args:
        article_id: ID статьи
        idea_id: ID идеи
        user_id: ID пользователя для проверки владения

    Returns:
        True если привязка создана, False если нет прав или уже существует
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()

        # Проверяем ownership идеи
        cursor.execute(
            'SELECT 1 FROM ideas WHERE id = ? AND user_id = ?',
            (idea_id, user_id),
        )
        if not cursor.fetchone():
            return False

        # Проверяем ownership статьи (user_id может быть NULL для старых статей)
        cursor.execute(
            'SELECT 1 FROM articles WHERE id = ? AND (user_id = ? OR user_id IS NULL)',
            (article_id, user_id),
        )
        if not cursor.fetchone():
            return False

        # Создаём привязку (UNIQUE constraint предотвратит дубликаты)
        try:
            cursor.execute(
                """
                INSERT INTO idea_articles (idea_id, article_id, user_confirmed, added_at)
                VALUES (?, ?, 1, ?)
                """,
                (idea_id, article_id, datetime.now().isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Привязка уже существует
            return False
    finally:
        conn.close()


def unlink_article_from_idea(article_id: int, idea_id: int, user_id: int) -> bool:
    """
    Удаляет привязку статьи от идеи с проверкой ownership.

    Args:
        article_id: ID статьи
        idea_id: ID идеи
        user_id: ID пользователя для проверки владения

    Returns:
        True если привязка удалена, False если не найдена или нет прав
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()

        # Проверяем ownership идеи
        cursor.execute(
            'SELECT 1 FROM ideas WHERE id = ? AND user_id = ?',
            (idea_id, user_id),
        )
        if not cursor.fetchone():
            return False

        cursor.execute(
            'DELETE FROM idea_articles WHERE idea_id = ? AND article_id = ?',
            (idea_id, article_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_articles_by_idea(idea_id: int, user_id: int) -> list[dict]:
    """
    Получает все статьи, привязанные к идее.

    Args:
        idea_id: ID идеи
        user_id: ID пользователя для проверки владения

    Returns:
        Список словарей с полями статей и датой привязки
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()

        # Проверяем ownership идеи
        cursor.execute(
            'SELECT 1 FROM ideas WHERE id = ? AND user_id = ?',
            (idea_id, user_id),
        )
        if not cursor.fetchone():
            return []

        cursor.execute(
            """
            SELECT a.id, a.url, a.source, a.title, a.author, a.summary,
                   ia.added_at, ia.user_useful
            FROM articles a
            JOIN idea_articles ia ON a.id = ia.article_id
            WHERE ia.idea_id = ?
            ORDER BY ia.added_at DESC
            """,
            (idea_id,),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_ideas_by_article(article_id: int, user_id: int) -> list[dict]:
    """
    Получает все идеи, к которым привязана статья.

    Args:
        article_id: ID статьи
        user_id: ID пользователя для проверки владения

    Returns:
        Список словарей с полями идей
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT i.id, i.name, i.description, ia.added_at
            FROM ideas i
            JOIN idea_articles ia ON i.id = ia.idea_id
            WHERE ia.article_id = ? AND i.user_id = ?
            ORDER BY ia.added_at DESC
            """,
            (article_id, user_id),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
