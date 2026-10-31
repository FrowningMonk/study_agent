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
