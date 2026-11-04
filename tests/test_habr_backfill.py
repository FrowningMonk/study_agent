"""
Hubs/tags backfill (Task 14, Decision 18): `habr_store.list_backfill_candidates`,
`habr_backfill.run_once` / `run_safely` and the backfill thread start in `bot.main()`.

Habr is faked at `habr_api.fetch_article`; the pause between articles goes through
the patched `habr_backfill._sleep`, so the tests never wait and never hit the network.
"""

import logging
import sqlite3
from datetime import datetime, timezone

import pytest

import bot as bot_module
import digest
import habr_api
import habr_backfill
import habr_store


# =============================================================================
# Fakes and helpers
# =============================================================================


def _item(habr_id, hubs=(('python', 'collective'),), tags=('t1', 't2')):
    return {
        'habr_id': habr_id, 'title': f'Article {habr_id}', 'url': habr_api.canonical_url(habr_id),
        'published_at': datetime(2026, 1, 1, tzinfo=timezone.utc), 'score': 3, 'comments': 1,
        'hubs': list(hubs), 'tags': list(tags), 'lead_text': '<p>lead</p>', 'post_type': 'article',
    }


class FakeHabr:
    """Stand-in for habr_api.fetch_article: per-id errors, records (habr_id, wait_timeout)."""

    def __init__(self):
        self.calls: list[tuple[int, object]] = []
        self.errors: dict[int, Exception] = {}

    def __call__(self, habr_id, *, wait_timeout=None, deadline=None):
        self.calls.append((habr_id, wait_timeout))
        if habr_id in self.errors:
            raise self.errors[habr_id]
        return _item(habr_id)

    @property
    def ids(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def habr(monkeypatch):
    fake = FakeHabr()
    monkeypatch.setattr(habr_api, 'fetch_article', fake)
    return fake


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(habr_backfill, '_sleep', calls.append)
    return calls


def _save(db, habr_id, url=None, user_id=1000):
    url = url or f'https://habr.com/ru/articles/{habr_id}/'
    return db.save_article({'url': url, 'source': 'habr', 'title': 'T', 'content': 'C'}, 'S', 'm', user_id,
                           habr_id=habr_id)


def _query(db, sql, params=()):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# =============================================================================
# Candidate listing (habr_store)
# =============================================================================


def test_list_backfill_candidates(tmp_db):
    _save(tmp_db, 30)
    _save(tmp_db, 10)
    _save(tmp_db, 10, url='https://habr.com/ru/articles/10/?utm_source=x')
    _save(tmp_db, 20)
    _save(tmp_db, 40)
    _save(tmp_db, None, url='https://github.com/a/b')
    habr_store.upsert_habr_article(_item(20))
    habr_store.mark_unavailable(40)
    # A metadata row without fetched_at/unavailable_at is still a candidate
    conn = sqlite3.connect(tmp_db.DB_PATH)
    with conn:
        conn.execute('INSERT INTO habr_articles (habr_id, title) VALUES (30, ?)', ('bare',))
    conn.close()

    assert habr_store.list_backfill_candidates() == [10, 30]


@pytest.mark.parametrize('state, expected', [
    ('no_row', True),
    ('bare_row', True),
    ('fetched', False),
    ('unavailable', False),
])
def test_needs_metadata_fetch(tmp_db, state, expected):
    if state == 'bare_row':
        conn = sqlite3.connect(tmp_db.DB_PATH)
        with conn:
            conn.execute('INSERT INTO habr_articles (habr_id, title) VALUES (7, ?)', ('bare',))
        conn.close()
    elif state == 'fetched':
        habr_store.upsert_habr_article(_item(7))
    elif state == 'unavailable':
        habr_store.mark_unavailable(7)

    assert habr_store.needs_metadata_fetch(7) is expected


# =============================================================================
# run_once
# =============================================================================


def test_backfill_fills_hubs_and_tags(tmp_db, habr, sleeps):
    _save(tmp_db, 1)
    _save(tmp_db, 2)

    stats = habr_backfill.run_once()

    assert stats['requests'] == 2 and stats['filled'] == 2 and stats['unavailable'] == 0
    assert stats['stopped_on_error'] is False
    for habr_id in (1, 2):
        meta = habr_store.get_habr_article(habr_id)
        assert meta['fetched_at'] is not None and meta['unavailable_at'] is None
        assert meta['hubs'] == [{'alias': 'python', 'hub_type': 'collective'}]
        assert meta['tags'] == ['t1', 't2']


def test_second_run_makes_zero_requests(tmp_db, habr, sleeps):
    _save(tmp_db, 1)
    _save(tmp_db, 2)
    habr_backfill.run_once()
    habr.calls.clear()

    stats = habr_backfill.run_once()

    assert stats['requests'] == 0 and stats['filled'] == 0
    assert habr.calls == []


def test_not_found_marks_unavailable_and_not_requested_again(tmp_db, habr, sleeps):
    _save(tmp_db, 1)
    _save(tmp_db, 2)
    habr.errors[1] = habr_api.HabrNotFound('NOT_FOUND')

    stats = habr_backfill.run_once()

    assert stats['requests'] == 2 and stats['unavailable'] == 1 and stats['filled'] == 1
    assert habr_store.get_habr_article(1)['unavailable_at'] is not None
    assert habr_store.get_habr_article(1)['fetched_at'] is None
    habr.calls.clear()
    assert habr_backfill.run_once()['requests'] == 0
    assert habr.calls == []


@pytest.mark.parametrize('error', [
    habr_api.HabrUnavailable('http', 503),
    habr_api.HabrBusy('busy'),
], ids=['unavailable', 'busy'])
def test_transient_error_stops_and_resumes_next_run(tmp_db, habr, sleeps, error):
    for habr_id in (1, 2, 3):
        _save(tmp_db, habr_id)
    habr.errors[2] = error

    stats = habr_backfill.run_once()

    assert stats['stopped_on_error'] is True
    assert stats['requests'] == 2 and stats['filled'] == 1
    assert habr.ids == [1, 2]
    assert habr_store.get_habr_article(2) is None
    assert habr_store.get_habr_article(3) is None

    del habr.errors[2]
    habr.calls.clear()
    stats = habr_backfill.run_once()

    assert habr.ids == [2, 3]
    assert stats['filled'] == 2 and stats['stopped_on_error'] is False
    assert habr_store.get_habr_article(3)['fetched_at'] is not None


def test_db_error_on_store_stops_and_resumes_next_run(tmp_db, habr, sleeps, monkeypatch, caplog):
    for habr_id in (1, 2, 3):
        _save(tmp_db, habr_id)
    real_upsert = habr_store.upsert_habr_article

    def locked_on_2(item):
        if item['habr_id'] == 2:
            raise sqlite3.OperationalError('database is locked')
        real_upsert(item)
    monkeypatch.setattr(habr_store, 'upsert_habr_article', locked_on_2)

    with caplog.at_level(logging.WARNING, logger='habr_backfill'):
        stats = habr_backfill.run_once()

    assert stats['stopped_on_error'] is True and stats['filled'] == 1
    assert habr.ids == [1, 2]
    assert habr_store.get_habr_article(2) is None and habr_store.get_habr_article(3) is None
    assert any('OperationalError' in r.getMessage() for r in caplog.records)

    monkeypatch.setattr(habr_store, 'upsert_habr_article', real_upsert)
    habr.calls.clear()
    stats = habr_backfill.run_once()

    assert habr.ids == [2, 3] and stats['filled'] == 2 and stats['stopped_on_error'] is False


def test_db_error_on_mark_unavailable_stops_run(tmp_db, habr, sleeps, monkeypatch):
    _save(tmp_db, 1)
    _save(tmp_db, 2)
    habr.errors[1] = habr_api.HabrNotFound('NOT_FOUND')

    def locked(habr_id):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(habr_store, 'mark_unavailable', locked)

    stats = habr_backfill.run_once()

    assert stats['stopped_on_error'] is True and stats['unavailable'] == 0
    assert habr.ids == [1]


def test_db_error_on_listing_returns_stats(monkeypatch, habr, sleeps):
    def locked():
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(habr_store, 'list_backfill_candidates', locked)

    stats = habr_backfill.run_once()

    assert stats['stopped_on_error'] is True and stats['requests'] == 0
    assert habr.calls == []


def test_failure_log_has_category_not_message_text(tmp_db, habr, sleeps, caplog):
    _save(tmp_db, 1)
    habr.errors[1] = habr_api.HabrUnavailable('invalid_shape', 200, 'SECRET-BODY-TEXT')

    with caplog.at_level(logging.WARNING, logger='habr_backfill'):
        habr_backfill.run_once()

    text = ' '.join(r.getMessage() for r in caplog.records)
    assert 'HabrUnavailable invalid_shape HTTP 200' in text
    assert 'SECRET-BODY-TEXT' not in text


def test_unexpected_exception_logged_thread_ends(monkeypatch, caplog):
    def boom():
        raise RuntimeError('db is gone')
    monkeypatch.setattr(habr_backfill, 'run_once', boom)

    with caplog.at_level(logging.ERROR, logger='habr_backfill'):
        habr_backfill.run_safely()

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and errors[0].exc_info is not None


def test_pause_between_articles(tmp_db, habr, monkeypatch):
    for habr_id in (1, 2, 3):
        _save(tmp_db, habr_id)
    events: list = []
    monkeypatch.setattr(habr_backfill, '_sleep', lambda s: events.append(('sleep', s)))
    real_fetch = habr

    def fetch(habr_id, *, wait_timeout=None, deadline=None):
        events.append(('fetch', habr_id))
        return real_fetch(habr_id, wait_timeout=wait_timeout)
    monkeypatch.setattr(habr_api, 'fetch_article', fetch)

    habr_backfill.run_once()

    assert habr_backfill.PAUSE_BETWEEN_ARTICLES == 2
    pause = ('sleep', habr_backfill.PAUSE_BETWEEN_ARTICLES)
    # A pause between consecutive requests, none before the first or after the last
    assert events == [('fetch', 1), pause, ('fetch', 2), pause, ('fetch', 3)]


def test_duplicate_rows_not_merged(tmp_db, habr, sleeps):
    first = _save(tmp_db, 5)
    second = _save(tmp_db, 5, url='https://habr.com/ru/companies/x/articles/5/')
    idea = tmp_db.create_idea('Idea', None, 1000)
    assert tmp_db.link_article_to_idea(second, idea, 1000)
    before = _query(tmp_db, 'SELECT * FROM articles ORDER BY id')
    links_before = _query(tmp_db, 'SELECT * FROM idea_articles')

    habr_backfill.run_once()

    assert habr.ids == [5]
    assert _query(tmp_db, 'SELECT * FROM articles ORDER BY id') == before
    assert [r[0] for r in before] == [first, second]
    assert _query(tmp_db, 'SELECT * FROM idea_articles') == links_before


def test_already_filled_articles_skipped(tmp_db, habr, sleeps):
    _save(tmp_db, 1)
    _save(tmp_db, 2)
    # Metadata from a list fetch (digest / /feed)
    habr_store.upsert_habr_articles([_item(1)])

    stats = habr_backfill.run_once()

    assert habr.ids == [2]
    assert stats['requests'] == 1


def test_articles_without_habr_id_ignored(tmp_db, habr, sleeps):
    _save(tmp_db, None, url='https://github.com/a/b')

    stats = habr_backfill.run_once()

    assert habr.calls == [] and stats['requests'] == 0
    assert _query(tmp_db, 'SELECT COUNT(*) FROM habr_articles') == [(0,)]


def test_backfill_uses_background_mode(tmp_db, habr, sleeps):
    _save(tmp_db, 1)

    habr_backfill.run_once()

    assert habr.calls == [(1, None)]


# =============================================================================
# Thread start in bot.main()
# =============================================================================


@pytest.fixture
def main_env(tg, tmp_db, monkeypatch):
    calls = {'polling': 0, 'bot_spawn': [], 'digest_spawn': []}
    monkeypatch.setattr(bot_module, 'ensure_directories', lambda: None)
    monkeypatch.setattr(bot_module, 'check_providers_status', lambda: {})
    monkeypatch.setattr(bot_module, 'check_model_availability', lambda model, provider: (True, ''))
    monkeypatch.setattr(digest, '_spawn', calls['digest_spawn'].append)

    def polling(*a, **kw):
        calls['polling'] += 1
    monkeypatch.setattr(bot_module.bot, 'infinity_polling', polling)
    return calls


def test_main_starts_backfill_thread(main_env, monkeypatch):
    monkeypatch.setattr(bot_module, '_spawn', lambda fn, name: main_env['bot_spawn'].append((fn, name)))

    bot_module.main()

    assert (habr_backfill.run_safely, 'habr-backfill') in main_env['bot_spawn']
    assert main_env['polling'] == 1


def test_main_backfill_start_failure_does_not_block_polling(main_env, monkeypatch, caplog):
    def broken_for_backfill(fn, name):
        if name == 'habr-backfill':
            raise RuntimeError('no threads')
        main_env['bot_spawn'].append(name)
    monkeypatch.setattr(bot_module, '_spawn', broken_for_backfill)

    with caplog.at_level(logging.ERROR, logger='bot'):
        bot_module.main()

    assert main_env['polling'] == 1
    assert main_env['bot_spawn'] == ['digest-scheduler']      # the scheduler still starts
    assert any(r.levelno >= logging.ERROR and 'no threads' in r.getMessage() + str(r.exc_info)
               for r in caplog.records)
