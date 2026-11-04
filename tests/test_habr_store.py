import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

import database
import habr_store

NEW_TABLES = (
    'habr_hubs', 'hub_subscriptions', 'habr_articles', 'habr_article_hubs',
    'habr_article_tags', 'article_views', 'ranked_lists', 'ranked_list_items',
    'digest_settings', 'digest_runs', 'digest_deliveries',
)


def _raw(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _query(db_path, sql, params=()):
    conn = _raw(db_path)
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _dump_new_tables(db_path):
    return {t: _query(db_path, f'SELECT * FROM {t} ORDER BY 1, 2') for t in NEW_TABLES}


def _old_shape_db(tmp_path, monkeypatch, rows):
    """Production-shaped DB from before the feature: only `articles`, no habr_id column."""
    db_file = tmp_path / 'old.db'
    monkeypatch.setattr(database, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(database, 'DB_PATH', str(db_file))
    conn = sqlite3.connect(db_file)
    conn.executescript(database.CREATE_TABLE_SQL)
    for url, source, user_id in rows:
        conn.execute(
            "INSERT INTO articles (url, source, title, content, user_id, processed_at) "
            "VALUES (?, ?, 'T', 'C', ?, '2026-01-01T00:00:00')",
            (url, source, user_id),
        )
    conn.commit()
    conn.close()
    return db_file


def _habr_ids_by_url(db_file):
    return dict(_query(db_file, 'SELECT url, habr_id FROM articles'))


def _save(db, url, user_id=1, habr_id=None):
    return db.save_article(
        {'url': url, 'source': 'habr', 'title': 'T', 'content': 'C'}, 'S', 'm', user_id, habr_id=habr_id,
    )


def _item(habr_id, hubs=(('python', 'collective'),), tags=('t1',), score=5):
    return {
        'habr_id': habr_id,
        'title': f'Title {habr_id}',
        'url': f'https://habr.com/ru/articles/{habr_id}/',
        'published_at': datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc),
        'score': score,
        'comments': 2,
        'hubs': list(hubs),
        'tags': list(tags),
        'lead_text': '<p>lead</p>',
        'post_type': 'article',
    }


# --- schema and migration ---------------------------------------------------


def test_init_db_creates_all_habr_tables(tmp_db):
    tables = {r[0] for r in _query(tmp_db.DB_PATH, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(NEW_TABLES) <= tables
    columns = {r[1] for r in _query(tmp_db.DB_PATH, 'PRAGMA table_info(articles)')}
    assert 'habr_id' in columns
    indexes = {r[0] for r in _query(tmp_db.DB_PATH, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert 'idx_articles_habr_id' in indexes
    # Plain index: duplicates by habr_id are allowed
    unique = {r[1]: r[2] for r in _query(tmp_db.DB_PATH, 'PRAGMA index_list(articles)')}
    assert unique['idx_articles_habr_id'] == 0
    # No FK from new tables to articles/users; only ranked_list_items -> ranked_lists
    for table in NEW_TABLES:
        targets = {r[2] for r in _query(tmp_db.DB_PATH, f'PRAGMA foreign_key_list({table})')}
        assert targets <= {'ranked_lists'}, table
    fk = _query(tmp_db.DB_PATH, 'PRAGMA foreign_key_list(ranked_list_items)')
    assert fk and fk[0][2] == 'ranked_lists' and fk[0][6] == 'CASCADE'


def test_migration_on_preexisting_db_sets_habr_id(tmp_path, monkeypatch):
    rows = [
        ('https://habr.com/ru/articles/101/', 'habr', 1),
        ('https://habr.com/ru/post/102/', 'habr', 1),
        ('https://habr.com/ru/news/103/', 'habr', 1),
        ('https://habr.com/ru/companies/yandex/articles/104/', 'habr', 1),
        ('https://habr.com/ru/articles/105/?utm_source=tg&utm_medium=x#comments', 'habr', 1),
        ('https://www.habr.com/en/company/x/blog/106/', 'habr', 1),
        ('https://m.habr.com/ru/articles/107/comments/', 'habr', 1),
        ('https://github.com/owner/repo', 'github', 1),
        ('https://infostart.ru/1c/articles/2000000/', 'infostart', 1),
    ]
    db_file = _old_shape_db(tmp_path, monkeypatch, rows)

    database.init_db()

    got = _habr_ids_by_url(db_file)
    assert got['https://habr.com/ru/articles/101/'] == 101
    assert got['https://habr.com/ru/post/102/'] == 102
    assert got['https://habr.com/ru/news/103/'] == 103
    assert got['https://habr.com/ru/companies/yandex/articles/104/'] == 104
    assert got['https://habr.com/ru/articles/105/?utm_source=tg&utm_medium=x#comments'] == 105
    assert got['https://www.habr.com/en/company/x/blog/106/'] == 106
    assert got['https://m.habr.com/ru/articles/107/comments/'] == 107
    assert got['https://github.com/owner/repo'] is None
    assert got['https://infostart.ru/1c/articles/2000000/'] is None


def test_migration_rejects_lookalike_hosts(tmp_path, monkeypatch):
    bad = [
        'https://evilhabr.com/articles/5/',
        'http://x/?habr.com',
        'https://habr.com@evil.com/articles/5/',
        'https://habr.com:8080/articles/5/',
        'https://habr.com/ru/sandbox/5/',
        'https://habr.com/ru/posts/5/',
        'ftp://habr.com/ru/articles/5/',
        'https://habr.com.evil.com/ru/articles/5/',
        'https://habr.com/ru/articles/٥٦/',  # Arabic-Indic digits
        'https://habr.com/ru/articles/' + '9' * 30 + '/',  # does not fit INTEGER
        'https://habr.com/ru/articles/5/../../evil',
    ]
    db_file = _old_shape_db(tmp_path, monkeypatch, [(u, 'habr', 1) for u in bad])

    database.init_db()

    assert all(v is None for v in _habr_ids_by_url(db_file).values())
    assert _query(db_file, 'SELECT COUNT(*) FROM article_views') == [(0,)]


def test_migration_creates_legacy_views_once(tmp_path, monkeypatch):
    rows = [
        ('https://habr.com/ru/articles/1/', 'habr', 10),
        ('https://habr.com/ru/articles/1/?utm_source=x', 'habr', 10),  # duplicate, same user
        ('https://habr.com/ru/articles/1/?utm_source=y', 'habr', 20),  # duplicate, other user
        ('https://habr.com/ru/articles/2/', 'habr', None),
        ('https://github.com/a/b', 'github', 10),
    ]
    db_file = _old_shape_db(tmp_path, monkeypatch, rows)

    database.init_db()

    views = _query(db_file, 'SELECT user_id, habr_id, source FROM article_views ORDER BY user_id')
    assert views == [(10, 1, 'legacy'), (20, 1, 'legacy')]
    assert _habr_ids_by_url(db_file)['https://habr.com/ru/articles/2/'] == 2

    counts = (_query(db_file, 'SELECT COUNT(*) FROM articles'), _query(db_file, 'SELECT COUNT(*) FROM article_views'))
    database.init_db()
    assert (_query(db_file, 'SELECT COUNT(*) FROM articles'),
            _query(db_file, 'SELECT COUNT(*) FROM article_views')) == counts


def test_migration_skips_pair_with_existing_view(tmp_db):
    conn = _raw(tmp_db.DB_PATH)
    conn.execute(
        "INSERT INTO articles (url, source, title, content, user_id, processed_at) "
        "VALUES ('https://habr.com/ru/articles/7/', 'habr', 'T', 'C', 1, 'x')"
    )
    conn.commit()
    conn.close()
    habr_store.record_view(1, 7, 'link')

    tmp_db.init_db()

    assert _query(tmp_db.DB_PATH, 'SELECT user_id, habr_id, source FROM article_views') == [(1, 7, 'link')]


def test_init_db_twice_is_noop(tmp_db):
    _save(tmp_db, 'https://habr.com/ru/articles/1/', user_id=1, habr_id=1)
    habr_store.record_view(2, 1, 'feed')
    habr_store.upsert_habr_article(_item(1))
    habr_store.replace_hub_catalog([{'alias': 'python', 'title': 'Python', 'rating': 10.0}])
    habr_store.add_subscription(1, 'python')
    habr_store.create_ranked_list(1, 'feed', '2026-11-03', [1])
    habr_store.set_digest_enabled(1, False, 'blocked')
    habr_store.create_run('2026-11-03')
    habr_store.snapshot_recipients('2026-11-03', [1, 2])
    tmp_db.init_db()  # creates the legacy view for user 1
    before = _dump_new_tables(tmp_db.DB_PATH)
    articles_before = _query(tmp_db.DB_PATH, 'SELECT * FROM articles')

    tmp_db.init_db()

    assert _dump_new_tables(tmp_db.DB_PATH) == before
    assert _query(tmp_db.DB_PATH, 'SELECT * FROM articles') == articles_before


def test_migration_on_fresh_empty_db_is_noop(tmp_db):
    assert all(rows == [] for rows in _dump_new_tables(tmp_db.DB_PATH).values())


# --- articles.habr_id -------------------------------------------------------


def test_get_article_by_habr_id_returns_earliest_row(tmp_db):
    first = _save(tmp_db, 'https://habr.com/ru/articles/42/', habr_id=42)
    _save(tmp_db, 'https://habr.com/ru/articles/42/?utm_source=x', user_id=2, habr_id=42)

    row = tmp_db.get_article_by_habr_id(42)

    assert row['id'] == first
    assert row['habr_id'] == 42
    assert isinstance(row, dict)
    assert tmp_db.get_article_by_habr_id(43) is None
    # Duplicates are kept, never merged
    assert _query(tmp_db.DB_PATH, 'SELECT COUNT(*) FROM articles WHERE habr_id = 42') == [(2,)]


def test_save_article_stores_habr_id(tmp_db):
    with_id = _save(tmp_db, 'https://habr.com/ru/articles/5/', habr_id=5)
    without = tmp_db.save_article(
        {'url': 'https://github.com/a/b', 'source': 'github', 'title': 'T', 'content': 'C'}, 'S', 'm', 1,
    )

    assert tmp_db.get_article_by_id(with_id)['habr_id'] == 5
    assert tmp_db.get_article_by_id(without)['habr_id'] is None


# --- views ------------------------------------------------------------------


def test_record_view_and_seen_set(tmp_db):
    habr_store.record_view(1, 10, 'link')
    habr_store.record_view(1, 11, 'digest')
    habr_store.record_view(1, 11, 'feed')
    habr_store.record_view(1, 12, 'legacy')
    habr_store.record_view(2, 99, 'link')

    assert habr_store.get_viewed_ids(1) == {10, 11, 12}
    assert habr_store.get_viewed_ids(2) == {99}
    assert habr_store.get_viewed_ids(3) == set()
    with pytest.raises(ValueError):
        habr_store.record_view(1, 13, 'bogus')
    assert 13 not in habr_store.get_viewed_ids(1)


def test_article_views_source_check_constraint(tmp_db):
    conn = sqlite3.connect(tmp_db.DB_PATH)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO article_views (user_id, habr_id, source, created_at) VALUES (1, 1, 'web', 'x')"
            )
    finally:
        conn.close()


# --- habr article metadata --------------------------------------------------


def test_upsert_habr_article_replaces_hubs_tags_keeps_description(tmp_db):
    habr_store.upsert_habr_article(_item(1, hubs=[('python', 'collective'), ('yandex', 'corporative')], tags=['a', 'b']))
    assert habr_store.set_short_description(1, 'Short text')
    habr_store.mark_unavailable(1)

    habr_store.upsert_habr_article(_item(1, hubs=[('go', 'collective')], tags=['c'], score=42))

    got = habr_store.get_habr_article(1)
    assert got['score'] == 42
    assert got['comments_count'] == 2
    assert got['hubs'] == [{'alias': 'go', 'hub_type': 'collective'}]
    assert got['tags'] == ['c']
    assert got['short_description'] == 'Short text'
    assert got['unavailable_at'] is not None
    assert got['published_at'] == '2026-11-03T09:00:00+00:00'
    assert got['fetched_at'] is not None
    assert _query(tmp_db.DB_PATH, 'SELECT COUNT(*) FROM habr_article_hubs') == [(1,)]
    assert _query(tmp_db.DB_PATH, 'SELECT COUNT(*) FROM habr_article_tags') == [(1,)]


def test_get_habr_articles_by_ids(tmp_db):
    habr_store.upsert_habr_articles([_item(1), _item(2, hubs=[], tags=[])])

    got = habr_store.get_habr_articles([1, 2, 3])

    assert set(got) == {1, 2}
    assert got[2]['hubs'] == [] and got[2]['tags'] == []
    assert got[1]['hubs'] == [{'alias': 'python', 'hub_type': 'collective'}]
    assert habr_store.get_habr_articles([]) == {}
    assert habr_store.get_habr_article(3) is None


def test_mark_unavailable_without_metadata_row(tmp_db):
    habr_store.mark_unavailable(77)

    got = habr_store.get_habr_article(77)
    assert got['unavailable_at'] is not None
    assert got['fetched_at'] is None
    assert habr_store.set_short_description(78, 'x') is False


# --- hub catalog ------------------------------------------------------------


BAD_ALIASES = [
    'with space', 'pyth١on', '٥٦', 'a:b', 'a/b', 'x' * 51, '', 'питон', 'a\nb', None, 5,
]


def test_hub_catalog_import_rejects_bad_aliases(tmp_db):
    good = ['python', '1C', 'DIY', 'machine_learning', 'c-sharp', 'x' * 50]
    hubs = [{'alias': a, 'title': f'T {a}', 'rating': float(i)} for i, a in enumerate(good + BAD_ALIASES)]

    stored = habr_store.replace_hub_catalog(hubs)

    assert stored == len(good)
    assert {r[0] for r in _query(tmp_db.DB_PATH, 'SELECT alias FROM habr_hubs')} == set(good)
    assert habr_store.find_hub('PYTHON')['alias'] == 'python'
    assert habr_store.find_hub('1c')['alias'] == '1C'
    assert habr_store.find_hub('diy')['alias'] == 'DIY'
    assert habr_store.find_hub('missing') is None
    assert habr_store.find_hub('a:b') is None
    assert habr_store.HUB_ALIAS_RE.pattern == '^[A-Za-z0-9_-]{1,50}$'
    assert all(habr_store.HUB_ALIAS_RE.fullmatch(a) for a in good)
    assert not any(isinstance(a, str) and habr_store.HUB_ALIAS_RE.fullmatch(a) for a in BAD_ALIASES)


def test_hub_catalog_replace_top_and_fetched_at(tmp_db):
    assert habr_store.catalog_fetched_at() is None
    habr_store.replace_hub_catalog([
        {'alias': 'old', 'title': 'Old', 'rating': 100.0},
    ])
    habr_store.replace_hub_catalog([
        {'alias': 'a', 'title': 'A', 'rating': 1.0},
        {'alias': 'b', 'title': 'B', 'rating': 3.0},
        {'alias': 'c', 'title': 'C', 'rating': 2.0},
    ])

    assert [h['alias'] for h in habr_store.top_hubs(2)] == ['b', 'c']
    assert habr_store.find_hub('old') is None
    fetched = datetime.fromisoformat(habr_store.catalog_fetched_at())
    assert fetched.tzinfo is not None
    assert abs(datetime.now(timezone.utc) - fetched) < timedelta(minutes=1)


# --- subscriptions ----------------------------------------------------------


def test_subscriptions_idempotent_per_user(tmp_db):
    owner_id = 123456789  # owner is not stored in users: no FK may block this
    assert habr_store.add_subscription(owner_id, 'python') is True
    assert habr_store.add_subscription(owner_id, 'python') is False
    habr_store.add_subscription(owner_id, 'go')
    habr_store.add_subscription(2, 'rust')

    assert habr_store.list_subscriptions(owner_id) == ['go', 'python']
    assert habr_store.remove_subscription(owner_id, 'go') is True
    assert habr_store.remove_subscription(owner_id, 'go') is False
    assert habr_store.list_subscriptions(owner_id) == ['python']
    assert habr_store.list_subscriptions(2) == ['rust']
    assert habr_store.list_subscriptions(3) == []


# --- ranked lists -----------------------------------------------------------


def test_ranked_list_roundtrip_and_cascade(tmp_db):
    ids = list(range(100, 125))
    list_id = habr_store.create_ranked_list(1, 'digest_site', '2026-11-03', ids)

    full = habr_store.get_ranked_list(list_id)
    assert full['user_id'] == 1 and full['kind'] == 'digest_site' and full['list_date'] == '2026-11-03'
    assert full['total'] == 25
    assert [i['habr_id'] for i in full['items']] == ids
    assert [i['position'] for i in full['items']] == list(range(1, 26))

    page = habr_store.get_ranked_list(list_id, offset=20, limit=10)
    assert [i['habr_id'] for i in page['items']] == ids[20:]
    assert page['total'] == 25
    assert habr_store.get_ranked_list(list_id + 1) is None

    with pytest.raises(ValueError):
        habr_store.create_ranked_list(1, 'bogus', '2026-11-03', [1])
    conn = sqlite3.connect(tmp_db.DB_PATH)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO ranked_lists (user_id, kind, list_date, created_at) VALUES (1, 'x', 'd', 'c')")
    finally:
        conn.close()

    conn = tmp_db._get_connection()  # foreign keys on
    try:
        with conn:
            conn.execute('DELETE FROM ranked_lists WHERE id = ?', (list_id,))
    finally:
        conn.close()
    assert _query(tmp_db.DB_PATH, 'SELECT COUNT(*) FROM ranked_list_items') == [(0,)]


def test_create_ranked_list_empty(tmp_db):
    list_id = habr_store.create_ranked_list(1, 'feed', '2026-11-03', [])
    assert habr_store.get_ranked_list(list_id)['items'] == []


def _age_list(db_path, list_id, days):
    created = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute('UPDATE ranked_lists SET created_at = ? WHERE id = ?', (created, list_id))
    conn.commit()
    conn.close()


def test_cleanup_deletes_lists_older_than_30_days(tmp_db):
    old = habr_store.create_ranked_list(1, 'feed', '2026-10-01', [1, 2])
    kept = habr_store.create_ranked_list(1, 'feed', '2026-10-03', [3])
    fresh = habr_store.create_ranked_list(1, 'feed', '2026-11-03', [4])
    _age_list(tmp_db.DB_PATH, old, 31)
    _age_list(tmp_db.DB_PATH, kept, 29)

    assert habr_store.cleanup_ranked_lists() == 1

    assert habr_store.get_ranked_list(old) is None
    assert habr_store.get_ranked_list(kept) is not None
    assert habr_store.get_ranked_list(fresh) is not None
    assert {r[0] for r in _query(tmp_db.DB_PATH, 'SELECT habr_id FROM ranked_list_items')} == {3, 4}


# --- digest settings, runs, deliveries --------------------------------------


def test_digest_settings_missing_row_means_enabled(tmp_db):
    assert habr_store.get_digest_enabled(1) is True

    habr_store.set_digest_enabled(1, False, reason='blocked')
    assert habr_store.get_digest_enabled(1) is False
    assert _query(tmp_db.DB_PATH, 'SELECT enabled, disabled_reason FROM digest_settings') == [(0, 'blocked')]

    habr_store.set_digest_enabled(1, True)
    assert habr_store.get_digest_enabled(1) is True
    assert _query(tmp_db.DB_PATH, 'SELECT enabled, disabled_reason FROM digest_settings') == [(1, None)]
    assert habr_store.get_digest_enabled(2) is True


def test_get_run_and_create_run(tmp_db):
    assert habr_store.get_run('2026-11-03') is None

    assert habr_store.create_run('2026-11-03') is True
    run = habr_store.get_run('2026-11-03')
    assert run['digest_date'] == '2026-11-03' and run['status'] == 'pending'
    assert run['finished_at'] is None

    assert habr_store.create_run('2026-11-03') is False
    assert _query(tmp_db.DB_PATH, 'SELECT COUNT(*) FROM digest_runs') == [(1,)]


def test_list_unfinished_runs_before_date(tmp_db):
    for date in ('2026-11-01', '2026-11-02', '2026-10-31', '2026-11-03', '2026-11-04', '2026-10-30'):
        habr_store.create_run(date)
    habr_store.update_run('2026-11-01', status='done')
    habr_store.update_run('2026-10-30', status='skipped')

    got = habr_store.list_unfinished_runs('2026-11-03')

    assert [r['digest_date'] for r in got] == ['2026-10-31', '2026-11-02']


def test_update_run_whitelist(tmp_db):
    habr_store.create_run('2026-11-03')
    assert habr_store.update_run(
        '2026-11-03', status='done', last_error='Хабр не ответил (HTTP 503)',
        next_attempt_at='2026-11-03T15:15:00+00:00', warned_at='2026-11-03T18:00:00+00:00',
        finished_at='2026-11-03T18:10:00+00:00',
    ) is True
    run = habr_store.get_run('2026-11-03')
    assert run['status'] == 'done'
    assert run['last_error'] == 'Хабр не ответил (HTTP 503)'
    assert run['next_attempt_at'] == '2026-11-03T15:15:00+00:00'
    assert run['warned_at'] == '2026-11-03T18:00:00+00:00'
    assert run['finished_at'] == '2026-11-03T18:10:00+00:00'
    assert habr_store.update_run('2026-11-04', status='done') is False

    before = _query(tmp_db.DB_PATH, 'SELECT * FROM digest_runs')
    for bad in ({'digest_date': '2000-01-01'},
                {'status = status, last_error': 'x'},
                {"status='done' --": 'x'},
                {'status': 'done', 'bogus': 1}):
        with pytest.raises(ValueError):
            habr_store.update_run('2026-11-03', **bad)
    with pytest.raises(ValueError):
        habr_store.update_run('2026-11-03', status='running')
    with pytest.raises(ValueError):
        habr_store.update_run('2026-11-03')
    assert _query(tmp_db.DB_PATH, 'SELECT * FROM digest_runs') == before


def test_delivery_snapshot_insert_or_ignore_keeps_sent(tmp_db):
    owner = 100
    habr_store.create_run('2026-11-03')
    habr_store.snapshot_recipients('2026-11-03', [owner])
    habr_store.set_delivery_status('2026-11-03', owner, 'sent')

    inserted = habr_store.snapshot_recipients('2026-11-03', [owner, 2])

    assert inserted == 1
    rows = {d['user_id']: d for d in habr_store.list_deliveries('2026-11-03')}
    assert rows[owner]['status'] == 'sent' and rows[owner]['sent_at'] is not None
    assert rows[2]['status'] == 'pending' and rows[2]['sent_at'] is None


def test_set_delivery_status_validates(tmp_db):
    habr_store.snapshot_recipients('2026-11-03', [1])
    with pytest.raises(ValueError):
        habr_store.set_delivery_status('2026-11-03', 1, 'lost')
    assert habr_store.set_delivery_status('2026-11-03', 1, 'failed') is True
    assert habr_store.list_deliveries('2026-11-03')[0]['sent_at'] is None
    assert habr_store.set_delivery_status('2026-11-03', 2, 'sent') is False


def test_delivery_claim_is_atomic(tmp_db):
    habr_store.snapshot_recipients('2026-11-03', [1, 2])
    assert habr_store.claim_delivery('2026-11-03', 1) is True
    assert habr_store.claim_delivery('2026-11-03', 1) is False
    assert habr_store.claim_delivery('2026-11-03', 3) is False

    workers = 8
    barrier = threading.Barrier(workers)
    results = []
    lock = threading.Lock()

    def claim():
        barrier.wait()
        won = habr_store.claim_delivery('2026-11-03', 2)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=claim) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == workers
    assert results.count(True) == 1
    assert habr_store.list_deliveries('2026-11-03', status='sending')[1]['user_id'] == 2


def test_list_deliveries_filters_by_status(tmp_db):
    habr_store.snapshot_recipients('2026-11-03', [1, 2, 3])
    habr_store.snapshot_recipients('2026-11-02', [1])
    habr_store.set_delivery_status('2026-11-03', 2, 'sent')
    habr_store.set_delivery_status('2026-11-03', 3, 'failed')

    assert [d['user_id'] for d in habr_store.list_deliveries('2026-11-03')] == [1, 2, 3]
    assert [d['user_id'] for d in habr_store.list_deliveries('2026-11-03', status='pending')] == [1]
    assert [d['user_id'] for d in habr_store.list_deliveries('2026-11-03', status='sent')] == [2]
    assert all(d['digest_date'] == '2026-11-03' for d in habr_store.list_deliveries('2026-11-03'))
    assert habr_store.list_deliveries('2026-11-04') == []


# --- interaction with existing articles functions ---------------------------


def test_wipe_and_delete_article_still_work_with_new_tables(tmp_db):
    first = _save(tmp_db, 'https://habr.com/ru/articles/1/', habr_id=1)
    _save(tmp_db, 'https://habr.com/ru/articles/2/', habr_id=2)
    _save(tmp_db, 'https://habr.com/ru/articles/3/', habr_id=3)
    habr_store.record_view(1, 1, 'link')
    habr_store.upsert_habr_article(_item(1))
    habr_store.replace_hub_catalog([{'alias': 'python', 'title': 'P', 'rating': 1.0}])
    habr_store.add_subscription(1, 'python')
    habr_store.create_ranked_list(1, 'feed', '2026-11-03', [1, 2])
    habr_store.set_digest_enabled(1, False)
    habr_store.create_run('2026-11-03')
    habr_store.snapshot_recipients('2026-11-03', [1])
    snapshot = _dump_new_tables(tmp_db.DB_PATH)

    assert tmp_db.delete_article(first) is True
    assert _dump_new_tables(tmp_db.DB_PATH) == snapshot

    assert tmp_db.wipe_articles() == 2
    assert tmp_db.count_articles() == 0
    assert _dump_new_tables(tmp_db.DB_PATH) == snapshot


def test_link_article_to_idea_allowed_with_view_row(tmp_db):
    article_id = _save(tmp_db, 'https://habr.com/ru/articles/50/', user_id=1, habr_id=50)
    idea_b = tmp_db.create_idea('B idea', None, 2)
    habr_store.record_view(2, 50, 'digest')

    assert tmp_db.link_article_to_idea(article_id, idea_b, 2) is True
    assert [a['id'] for a in tmp_db.get_articles_by_idea(idea_b, 2)] == [article_id]
    # Already linked -> False (contract unchanged)
    assert tmp_db.link_article_to_idea(article_id, idea_b, 2) is False


def test_link_article_to_idea_refused_without_view_row(tmp_db):
    article_id = _save(tmp_db, 'https://habr.com/ru/articles/50/', user_id=1, habr_id=50)
    idea_b = tmp_db.create_idea('B idea', None, 2)
    idea_a = tmp_db.create_idea('A idea', None, 1)

    assert tmp_db.link_article_to_idea(article_id, idea_b, 2) is False

    # A view of a different habr_id does not grant access
    habr_store.record_view(2, 51, 'link')
    assert tmp_db.link_article_to_idea(article_id, idea_b, 2) is False

    # Someone else's view does not grant access either
    habr_store.record_view(3, 50, 'link')
    assert tmp_db.link_article_to_idea(article_id, idea_b, 2) is False

    # Idea owned by another user -> False even with a view row
    habr_store.record_view(2, 50, 'link')
    assert tmp_db.link_article_to_idea(article_id, idea_a, 2) is False

    # Rows without habr_id fall back to the owner rule only
    gh = tmp_db.save_article({'url': 'https://github.com/a/b', 'source': 'github', 'title': 'T', 'content': 'C'},
                             'S', 'm', 1)
    assert tmp_db.link_article_to_idea(gh, idea_b, 2) is False

    # Owner-less row still allowed
    orphan = _save(tmp_db, 'https://habr.com/ru/articles/60/', user_id=None, habr_id=60)
    assert tmp_db.link_article_to_idea(orphan, idea_b, 2) is True


def test_functions_use_patched_db_path(tmp_db, tmp_path):
    assert database.DB_PATH == str(tmp_path / 'test.db')
    habr_store.record_view(1, 5, 'link')
    habr_store.add_subscription(1, 'python')

    assert _query(str(tmp_path / 'test.db'), 'SELECT habr_id FROM article_views') == [(5,)]
    assert _query(str(tmp_path / 'test.db'), 'SELECT hub_alias FROM hub_subscriptions') == [('python',)]
    assert habr_store.get_viewed_ids(1) == {5}
