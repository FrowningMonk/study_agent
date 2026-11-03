import sqlite3

import pytest

import database


def test_users_migration_on_existing_db(tmp_path, monkeypatch):
    db_file = tmp_path / 'old.db'
    monkeypatch.setattr(database, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(database, 'DB_PATH', str(db_file))

    # Старая БД: только таблица articles с одной статьёй, без users
    conn = sqlite3.connect(db_file)
    conn.executescript(database.CREATE_TABLE_SQL)
    conn.execute(
        "INSERT INTO articles (url, source, title, content, processed_at) VALUES (?, ?, ?, ?, ?)",
        ('https://habr.com/ru/articles/1/', 'habr', 'T', 'C', '2026-01-01T00:00:00'),
    )
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(db_file)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    assert 'users' in tables
    assert count == 1
    assert database.article_exists('https://habr.com/ru/articles/1/')


def test_init_db_twice_keeps_users(tmp_db):
    tmp_db.create_access_request(1, 'a', 'A')
    tmp_db.set_user_status(1, 'approved')
    tmp_db.init_db()
    assert tmp_db.get_user(1)['status'] == 'approved'


def test_users_status_check_constraint(tmp_db):
    conn = sqlite3.connect(tmp_db.DB_PATH)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO users (user_id, status, requested_at) VALUES (1, 'admin', 'x')")
    finally:
        conn.close()


def test_set_user_status_unknown_user(tmp_db):
    assert tmp_db.set_user_status(999, 'approved') is False
    assert tmp_db.get_user(999) is None


def test_set_user_status_rejects_invalid_status(tmp_db):
    tmp_db.create_access_request(1, 'u', 'U')
    with pytest.raises(ValueError):
        tmp_db.set_user_status(1, 'admin')


def test_list_users_ordered_by_request(tmp_db):
    tmp_db.create_access_request(1, 'a', 'A')
    tmp_db.create_access_request(2, None, 'B')
    assert [u['user_id'] for u in tmp_db.list_users()] == [1, 2]


def _article(db, n: int, user_id: int = 1) -> int:
    return db.save_article(
        {'url': f'https://habr.com/ru/articles/{n}/', 'source': 'habr', 'title': f'T{n}', 'content': 'C'},
        'S', 'm', user_id,
    )


def _links_count(db) -> int:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute('SELECT COUNT(*) FROM idea_articles').fetchone()[0]
    finally:
        conn.close()


def test_delete_article_removes_links(tmp_db):
    article_id = _article(tmp_db, 1)
    idea_id = tmp_db.create_idea('I', None, 1)
    assert tmp_db.link_article_to_idea(article_id, idea_id, 1)

    assert tmp_db.delete_article(article_id)
    assert _links_count(tmp_db) == 0
    assert tmp_db.get_articles_by_idea(idea_id, 1) == []
    assert tmp_db.get_idea_by_id(idea_id, 1) is not None


def test_wipe_articles_keeps_ideas_and_users(tmp_db):
    tmp_db.create_access_request(2, 'u', 'U')
    tmp_db.set_user_status(2, 'approved')
    first = _article(tmp_db, 1, user_id=1)
    _article(tmp_db, 2, user_id=2)
    idea_id = tmp_db.create_idea('I', 'D', 1)
    tmp_db.link_article_to_idea(first, idea_id, 1)
    assert tmp_db.count_articles() == 2

    assert tmp_db.wipe_articles() == 2

    assert tmp_db.count_articles() == 0
    assert _links_count(tmp_db) == 0
    assert tmp_db.get_idea_by_id(idea_id, 1)['name'] == 'I'
    assert tmp_db.get_articles_by_idea(idea_id, 1) == []
    assert tmp_db.get_user(2)['status'] == 'approved'
    # После очистки та же ссылка разбирается заново, а не берётся из кеша
    assert not tmp_db.article_exists('https://habr.com/ru/articles/1/')


def test_wipe_articles_on_empty_db(tmp_db):
    assert tmp_db.wipe_articles() == 0


def test_init_db_removes_orphan_links(tmp_db):
    article_id = _article(tmp_db, 1)
    idea_id = tmp_db.create_idea('I', None, 1)
    # Сироты из времён, когда внешние ключи были выключены
    conn = sqlite3.connect(tmp_db.DB_PATH)
    conn.execute("INSERT INTO idea_articles (idea_id, article_id, added_at) VALUES (?, 999, 'x')", (idea_id,))
    conn.execute("INSERT INTO idea_articles (idea_id, article_id, added_at) VALUES (999, ?, 'x')", (article_id,))
    conn.execute("INSERT INTO idea_articles (idea_id, article_id, added_at) VALUES (?, ?, 'x')", (idea_id, article_id))
    conn.commit()
    conn.close()

    tmp_db.init_db()

    assert _links_count(tmp_db) == 1
    assert [a['id'] for a in tmp_db.get_articles_by_idea(idea_id, 1)] == [article_id]
