"""
Bot integration of the daily digest (Task 11): /digest on|off, owner-only /digest_now,
digest list buttons through the shared `hl:` handlers, the scheduler start in main(),
the `send` adapter, menus and help.

Real telebot dispatch through `tg` (conftest). Habr is faked at the habr_api public
fetchers; the digest clock is fixed; `digest._spawn` is captured or run synchronously;
`digest._DEFAULT_DESCRIBE` is a fake and the real summarizer is guarded, so no network
and no LLM call is ever made.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from telebot import types
from telebot.apihelper import ApiTelegramException

import bot as bot_module
import digest
import feed
import habr_api
import habr_store
import summarizer
from conftest import FRIEND, STRANGER, _callback_update, _message_update

OWNER = bot_module.OWNER_ID
URL = 'https://habr.com/ru/articles/1/'


# =============================================================================
# Fakes and helpers
# =============================================================================


class FakeHabr:
    """habr_api public fetchers; records calls; the list fetch can be told to fail."""

    def __init__(self):
        self.items: list[dict] = []
        self.list_error: Exception | None = None
        self.calls: list[tuple] = []

    def fetch_articles_since(self, since, *, wait_timeout=None):
        self.calls.append(('list', since))
        if self.list_error:
            raise self.list_error
        return list(self.items)

    def fetch_article(self, habr_id, *, wait_timeout=None):
        self.calls.append(('article', habr_id))
        return next(i for i in self.items if i['habr_id'] == habr_id)

    def fetch_hub_catalog(self, *, wait_timeout=None):
        self.calls.append(('catalog',))
        return []

    def fill(self, day, site=8, hubs=0):
        """Site items 100+n (hub `other`) and python-hub items 200+n published on `day` (MSK)."""
        noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=feed.MSK)
        for n in range(site):
            self.items.append(_item(100 + n, noon - timedelta(minutes=n), 1000 - n, 'other'))
        for n in range(hubs):
            self.items.append(_item(200 + n, noon - timedelta(minutes=100 + n), 500 - n, 'python'))


def _item(habr_id, published, score, hub):
    return {
        'habr_id': habr_id, 'title': f'Статья {habr_id}', 'url': habr_api.canonical_url(habr_id),
        'published_at': published.astimezone(timezone.utc), 'score': score, 'comments': 0,
        'hubs': [(hub, 'collective')], 'tags': [], 'lead_text': '<p>Короткий анонс статьи.</p>',
        'post_type': 'article',
    }


class FakeDescribe:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, title, lead_text):
        self.calls.append(title)
        return f'О чём: {title}'


class FakeProcess:
    """Stand-in for bot.process_article (the summary LLM path)."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        return 'NEW SUMMARY', {'url': url, 'source': 'habr', 'title': 'T', 'content': 'C'}


@pytest.fixture(autouse=True)
def describe(monkeypatch):
    """No real LLM: the default describe is a fake; the real one and `_generate` raise."""
    fake = FakeDescribe()
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', fake)

    def guard(*a, **kw):
        raise AssertionError('real LLM call in a test')
    monkeypatch.setattr(summarizer, 'generate_short_description', guard)
    monkeypatch.setattr(summarizer, '_generate', guard)
    monkeypatch.delenv('DIGEST_TIME', raising=False)
    return fake


@pytest.fixture
def now(monkeypatch):
    """Fixed digest clock: the real current moment, frozen (lists stay inside 30 days)."""
    frozen = datetime.now(feed.MSK).replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(digest, '_now', lambda: frozen)
    return frozen


@pytest.fixture
def habr(tg, now, monkeypatch):
    fake = FakeHabr()
    for name in ('fetch_articles_since', 'fetch_article', 'fetch_hub_catalog'):
        monkeypatch.setattr(habr_api, name, getattr(fake, name))
    return fake


@pytest.fixture
def llm(tg, monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(bot_module, 'process_article', process)
    monkeypatch.setattr(bot_module, 'check_model_availability', lambda model, provider: (True, ''))
    return process


@pytest.fixture
def jobs(monkeypatch):
    """digest._spawn captured: the /digest_now work runs only when the test calls it."""
    captured = []
    monkeypatch.setattr(digest, '_spawn', captured.append)
    return captured


@pytest.fixture
def sync_spawn(monkeypatch):
    """Both spawns run synchronously; digest spawns are counted."""
    calls = []

    def spawn(fn):
        calls.append(fn)
        fn()
    monkeypatch.setattr(digest, '_spawn', spawn)
    monkeypatch.setattr(bot_module, '_spawn', lambda fn: fn())
    return calls


def _send(user_id, text, update_id=1):
    bot_module.bot.process_new_updates([_message_update(user_id, text, update_id=update_id)])


def _press(user_id, data, update_id=2):
    bot_module.bot.process_new_updates([_callback_update(user_id, data, update_id=update_id)])


def _replies(sent, chat_id):
    """Texts sent to chat_id through send_message or reply_to."""
    out = []
    for name, args, kwargs in sent:
        if name == 'send_message' and args[0] == chat_id:
            out.append(args[1])
        elif name == 'reply_to' and args[0].chat.id == chat_id:
            out.append(args[1])
    return out


def _messages_with_markup(sent, chat_id):
    return [(a[1], kw.get('reply_markup')) for name, a, kw in sent
            if name == 'send_message' and a[0] == chat_id]


def _answers(sent):
    return [a[1] if len(a) > 1 else None for name, a, kw in sent if name == 'answer_callback_query']


def _callbacks(markup):
    return [b.callback_data for row in markup.keyboard for b in row] if markup else []


def _settings(db, user_id):
    conn = db._get_connection()
    try:
        row = conn.execute('SELECT enabled, disabled_reason FROM digest_settings WHERE user_id = ?',
                           (user_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else (row[0], row[1])


def _count(db, table):
    conn = db._get_connection()
    try:
        return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    finally:
        conn.close()


def _digest_parts(sent):
    """Owner messages that carry a digest keyboard."""
    return [(t, m) for t, m in _messages_with_markup(sent, OWNER)
            if m is not None and any(cb.startswith('hl:') for cb in _callbacks(m))]


# =============================================================================
# /digest on|off
# =============================================================================


def test_digest_off_then_on(tg, tmp_db):
    habr_store.set_digest_enabled(FRIEND, False, reason='blocked')
    habr_store.set_digest_enabled(FRIEND, True)

    _send(FRIEND, '/digest off')
    assert _settings(tmp_db, FRIEND) == (0, None)
    assert _replies(tg, FRIEND) == [bot_module.MSG_DIGEST_OFF] == ['Дайджест выключен']

    tg.clear()
    _send(FRIEND, '/digest on')
    assert _settings(tmp_db, FRIEND) == (1, None)
    assert _replies(tg, FRIEND) == ['Дайджест включён']


def test_digest_default_enabled_without_row(tg, tmp_db):
    assert _settings(tmp_db, FRIEND) is None

    _send(FRIEND, '/digest')

    (reply,) = _replies(tg, FRIEND)
    assert 'включён' in reply and '/digest on' in reply and '/digest off' in reply
    assert _settings(tmp_db, FRIEND) is None

    habr_store.set_digest_enabled(FRIEND, False)
    tg.clear()
    _send(FRIEND, '/digest')
    (reply,) = _replies(tg, FRIEND)
    assert 'выключен' in reply


def test_digest_on_after_blocked_reenables(tg, tmp_db):
    habr_store.set_digest_enabled(OWNER, False, reason='blocked')
    assert _settings(tmp_db, OWNER) == (0, 'blocked')

    _send(OWNER, '/digest on')

    assert _settings(tmp_db, OWNER) == (1, None)
    assert habr_store.get_digest_enabled(OWNER) is True
    assert _replies(tg, OWNER) == ['Дайджест включён']


def test_digest_bad_argument_usage(tg, tmp_db):
    habr_store.set_digest_enabled(FRIEND, False, reason='blocked')

    _send(FRIEND, '/digest maybe')

    (reply,) = _replies(tg, FRIEND)
    assert '/digest on' in reply and 'выключен' in reply
    assert _settings(tmp_db, FRIEND) == (0, 'blocked')


@pytest.mark.parametrize('text, expected', [
    ('/digest ON', 1), ('/digest   off  ', 0), ('/digest@study_bot on', 1), ('/digest Off', 0),
])
def test_digest_argument_parsing(tg, tmp_db, text, expected):
    habr_store.set_digest_enabled(FRIEND, not expected)

    _send(FRIEND, text)

    assert _settings(tmp_db, FRIEND)[0] == expected
    assert len(_replies(tg, FRIEND)) == 1


def test_digest_argument_parsing_rejects_extra_arguments(tg, tmp_db, jobs):
    _send(FRIEND, '/digest on off')
    assert _settings(tmp_db, FRIEND) is None
    assert '/digest on' in _replies(tg, FRIEND)[0]

    tg.clear()
    _send(OWNER, '/digest_now final extra', update_id=2)
    assert jobs == [] and _replies(tg, OWNER) == [bot_module.MSG_DIGEST_NOW_USAGE]

    tg.clear()
    _send(OWNER, '/digest_now FINAL', update_id=3)
    assert len(jobs) == 1 and _replies(tg, OWNER) == [bot_module.MSG_DIGEST_NOW_STARTED]


def test_digest_now_final_upper_case_is_final(tg, tmp_db, habr, monkeypatch):
    started = []
    monkeypatch.setattr(digest, 'start_digest_now',
                        lambda send, owner_id, final: started.append((owner_id, final)) or True)

    _send(OWNER, '/digest_now FINAL')
    _send(OWNER, '/digest_now', update_id=2)

    assert started == [(OWNER, True), (OWNER, False)]


def test_digest_commands_respect_access_gate(tg, tmp_db, jobs):
    _send(STRANGER, '/digest off')
    _send(STRANGER, '/digest_now', update_id=2)

    assert _replies(tg, STRANGER) == [bot_module.MSG_ACCESS_OFFER] * 2
    assert _settings(tmp_db, STRANGER) is None and jobs == []


# =============================================================================
# /digest_now
# =============================================================================


def test_digest_now_non_owner_gets_unknown_reply(tg, tmp_db, jobs, caplog, monkeypatch):
    called = []
    monkeypatch.setattr(digest, 'start_digest_now', lambda *a: called.append(a) or True)

    with caplog.at_level(logging.WARNING, logger='bot'):
        _send(FRIEND, '/digest_now')
        _send(FRIEND, '/digest_now final', update_id=2)

    assert _replies(tg, FRIEND) == [bot_module.MSG_UNKNOWN] * 2
    assert called == [] and jobs == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and str(FRIEND) in r.getMessage()]
    assert len(warnings) == 2


def test_digest_now_owner_runs_in_spawn(tg, tmp_db, habr, jobs, describe, now):
    habr_store.add_subscription(OWNER, 'python')
    habr.fill(digest.digest_day(now), site=8, hubs=7)

    _send(OWNER, '/digest_now')

    # Immediate ack; nothing built inline in the handler
    assert _replies(tg, OWNER) == ['Собираю дайджест…']
    assert len(jobs) == 1 and habr.calls == [] and describe.calls == []

    jobs[0]()

    parts = _digest_parts(tg)
    assert len(parts) == 2
    assert parts[0][0].startswith('Лучшее на Хабре') and parts[1][0].startswith('Твои хабы')
    assert parts[1][0].endswith(digest.TEXT_FOOTER)
    for _, markup in parts:
        assert isinstance(markup, types.InlineKeyboardMarkup)
        assert [b.text for row in markup.keyboard for b in row][-1] == feed.TEXT_MORE_BUTTON
    assert len([c for c in habr.calls if c[0] == 'list']) == 1
    assert describe.calls                       # the injected fake, not the real LLM


def test_digest_now_does_not_touch_run_rows(tg, tmp_db, habr, sync_spawn, now):
    habr.fill(digest.digest_day(now))

    _send(OWNER, '/digest_now')

    assert len(_digest_parts(tg)) == 1
    assert _count(tmp_db, 'digest_runs') == 0
    assert _count(tmp_db, 'digest_deliveries') == 0


def test_digest_now_final_marks_owner_delivery_sent(tg, tmp_db, habr, sync_spawn, now):
    habr.fill(digest.digest_day(now))

    _send(OWNER, '/digest_now final')

    deliveries = habr_store.list_deliveries(now.date().isoformat())
    assert [(d['user_id'], d['status']) for d in deliveries] == [(OWNER, 'sent')]
    assert len(_digest_parts(tg)) == 1


def test_digest_now_single_flight(tg, tmp_db, habr, jobs, now):
    habr.fill(digest.digest_day(now))

    _send(OWNER, '/digest_now')
    _send(OWNER, '/digest_now final', update_id=2)

    assert _replies(tg, OWNER) == ['Собираю дайджест…', 'Уже собираю дайджест']
    assert len(jobs) == 1

    jobs[0]()
    tg.clear()
    _send(OWNER, '/digest_now', update_id=3)            # the flag is released after the run
    assert _replies(tg, OWNER) == ['Собираю дайджест…'] and len(jobs) == 2


def test_digest_now_habr_failure_reports_reason_to_owner(tg, tmp_db, habr, jobs):
    habr.list_error = habr_api.HabrUnavailable('http', 503, detail='SECRET-RAW-DETAIL')

    _send(OWNER, '/digest_now')
    jobs[0]()

    replies = _replies(tg, OWNER)
    assert replies == ['Собираю дайджест…', 'Дайджест не собран: Хабр не ответил (HTTP 503)']
    assert not any('SECRET' in r or 'Habr unavailable' in r for r in replies)


def test_digest_now_bad_argument(tg, tmp_db, jobs, monkeypatch):
    called = []
    monkeypatch.setattr(digest, 'start_digest_now', lambda *a: called.append(a) or True)

    _send(OWNER, '/digest_now foo')

    assert _replies(tg, OWNER) == [bot_module.MSG_DIGEST_NOW_USAGE]
    assert called == [] and jobs == []


def test_digest_now_spawn_failure_reported(tg, tmp_db, monkeypatch, caplog):
    def broken(fn):
        raise RuntimeError('cannot start thread')
    monkeypatch.setattr(digest, '_spawn', broken)

    with caplog.at_level(logging.ERROR, logger='bot'):
        _send(OWNER, '/digest_now')

    assert any(r.levelno == logging.ERROR for r in caplog.records)
    (reply,) = _replies(tg, OWNER)
    assert 'cannot start thread' not in reply and reply.startswith('Дайджест не собран')
    tg.clear()
    monkeypatch.setattr(digest, '_spawn', lambda fn: None)
    _send(OWNER, '/digest_now', update_id=2)            # the flag was released
    assert _replies(tg, OWNER) == ['Собираю дайджест…']


def test_digest_commands_with_url_argument_not_handled_as_url(tg, tmp_db, llm, jobs, monkeypatch):
    def no_scrape(*a, **kw):
        raise AssertionError('scrape in a command handler')
    monkeypatch.setattr(bot_module, '_summarize_for_user', no_scrape)

    _send(OWNER, f'/digest_now {URL}')
    _send(FRIEND, f'/digest off {URL}', update_id=2)

    assert _replies(tg, OWNER) == [bot_module.MSG_DIGEST_NOW_USAGE]
    assert '/digest on' in _replies(tg, FRIEND)[0]
    assert llm.calls == [] and jobs == []
    assert _settings(tmp_db, FRIEND) is None


def test_no_real_llm_in_bot_digest_tests(tg, tmp_db, habr, sync_spawn, describe, now, monkeypatch):
    real_calls = []
    monkeypatch.setattr(summarizer, 'generate_short_description', lambda *a, **kw: real_calls.append(a))
    habr.fill(digest.digest_day(now), site=3)

    _send(OWNER, '/digest_now')

    assert describe.calls == ['Статья 100', 'Статья 101', 'Статья 102']
    assert real_calls == []


def test_digest_now_after_revoked_user_settings_inert(tg, tmp_db, habr, sync_spawn, now):
    _send(FRIEND, '/digest off')
    tmp_db.set_user_status(FRIEND, 'rejected')          # revoked by the owner
    habr.fill(digest.digest_day(now))
    tg.clear()

    _send(OWNER, '/digest_now final', update_id=2)

    assert _settings(tmp_db, FRIEND) == (0, None)
    assert bot_module.is_allowed(FRIEND) is False
    assert FRIEND not in digest._recipients(OWNER)
    assert _replies(tg, FRIEND) == []
    assert [d['user_id'] for d in habr_store.list_deliveries(now.date().isoformat())] == [OWNER]


# =============================================================================
# Digest lists through the shared hl: handlers
# =============================================================================


def _owner_digest(tg, habr, now, site=8, hubs=7):
    habr_store.add_subscription(OWNER, 'python')
    habr.fill(digest.digest_day(now), site=site, hubs=hubs)
    _send(OWNER, '/digest_now', update_id=50)
    parts = _digest_parts(tg)
    tg.clear()
    habr.calls.clear()
    return parts


def test_digest_list_buttons_summary_and_more(tg, tmp_db, habr, llm, sync_spawn, now):
    (_, site), (_, hubs_markup) = _owner_digest(tg, habr, now, site=8, hubs=7)
    site_cbs = _callbacks(site)
    assert site_cbs[0].startswith('hl:s:') and site_cbs[-1].startswith('hl:m:')

    _press(OWNER, site_cbs[0])
    assert llm.calls == [habr_api.canonical_url(100)]
    conn = tmp_db._get_connection()
    try:
        views = conn.execute('SELECT habr_id, source FROM article_views WHERE user_id = ?', (OWNER,)).fetchall()
    finally:
        conn.close()
    assert [tuple(v) for v in views] == [(100, 'digest')]

    tg.clear()
    _press(OWNER, site_cbs[-1], update_id=3)             # «Ещё 10 ▶» of the site list (positions 6..15)
    (text, markup) = _messages_with_markup(tg, OWNER)[0]
    expected_end = feed.TEXT_DIGEST_END.format(date=feed.format_date_ru(digest.digest_day(now)))
    assert text.endswith(expected_end)
    assert markup is not None and all(cb.startswith('hl:s:') for cb in _callbacks(markup))
    assert _answers(tg) == [None]

    # The hubs part is a separate list with its own id, same handlers
    hubs_cbs = _callbacks(hubs_markup)
    assert hubs_cbs[0].split(':')[2] != site_cbs[0].split(':')[2]
    tg.clear()
    _press(OWNER, hubs_cbs[0], update_id=4)
    assert llm.calls[-1] == habr_api.canonical_url(200)
    _press(OWNER, hubs_cbs[-1], update_id=5)             # positions 6..7 of 7
    (text, markup) = _messages_with_markup(tg, OWNER)[-1]
    assert text.endswith(expected_end) and 'Статья 206' in text
    assert len(_callbacks(markup)) == 2


def test_digest_settings_error_reported(tg, tmp_db, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError('db is locked')
    monkeypatch.setattr(habr_store, 'set_digest_enabled', broken)

    _send(FRIEND, '/digest off')

    (reply,) = _replies(tg, FRIEND)
    assert reply.startswith('Ошибка')
    assert _settings(tmp_db, FRIEND) is None


def test_digest_list_foreign_user_rejected(tg, tmp_db, habr, llm, sync_spawn, now):
    (_, site), _ = _owner_digest(tg, habr, now)

    for n, data in enumerate(_callbacks(site)):
        _press(FRIEND, data, update_id=10 + n)

    assert set(_answers(tg)) == {'Кнопка устарела'}
    assert habr.calls == [] and llm.calls == []
    assert _replies(tg, FRIEND) == []


def test_digest_list_forged_and_expired_callbacks(tg, tmp_db, habr, llm, sync_spawn, now):
    (_, site), _ = _owner_digest(tg, habr, now)
    list_id = int(_callbacks(site)[0].split(':')[2])
    forged = ['hl:s:abc:1', f'hl:s:{list_id}', f'hl:d:{list_id}:1', f'hl:s:{list_id}:1:1',
              'hl:s:99999999999999999999:1', f'hl:s:{list_id}:99999999999', f'hl:m:{list_id}:5',
              f'hl:m:{list_id}:100', f'hl:s:{list_id}:99', 'hl:s:999999:1']
    for n, data in enumerate(forged):
        _press(OWNER, data, update_id=10 + n)
    assert _answers(tg) == ['Кнопка устарела'] * len(forged)

    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec='microseconds')
    conn = tmp_db._get_connection()
    try:
        with conn:
            conn.execute('UPDATE ranked_lists SET created_at = ? WHERE id = ?', (old, list_id))
    finally:
        conn.close()
    tg.clear()
    _press(OWNER, f'hl:s:{list_id}:1', update_id=40)
    _press(OWNER, f'hl:m:{list_id}:0', update_id=41)

    assert _answers(tg) == ['Кнопка устарела'] * 2
    assert habr.calls == [] and llm.calls == []
    assert _replies(tg, OWNER) == []


def test_digest_list_callback_data_within_64_bytes(tg, tmp_db, habr, llm, sync_spawn, now):
    parts = _owner_digest(tg, habr, now, site=40, hubs=30)
    assert len(parts) == 2

    seen = 0
    for _, markup in parts:
        pending = [markup]
        while pending:
            current = pending.pop()
            for data in _callbacks(current):
                assert len(data.encode('utf-8')) <= 64, data
                seen += 1
                if data.startswith('hl:m:'):
                    tg.clear()
                    _press(OWNER, data, update_id=100 + seen)
                    (_, page_markup) = _messages_with_markup(tg, OWNER)[0]
                    pending.append(page_markup)
    assert seen > 70                      # every page of both lists was walked
    assert llm.calls == []


# =============================================================================
# main(), send adapter, menus
# =============================================================================


@pytest.fixture
def main_env(tg, tmp_db, monkeypatch):
    calls = {'polling': 0, 'digest_spawn': [], 'bot_spawn': []}
    monkeypatch.setattr(bot_module, 'ensure_directories', lambda: None)
    monkeypatch.setattr(bot_module, 'check_providers_status', lambda: {})
    monkeypatch.setattr(bot_module, 'check_model_availability', lambda model, provider: (True, ''))
    monkeypatch.setattr(bot_module, '_spawn', calls['bot_spawn'].append)

    def polling(*a, **kw):
        calls['polling'] += 1
    monkeypatch.setattr(bot_module.bot, 'infinity_polling', polling)
    return calls


def test_main_starts_scheduler_thread(main_env, tg, monkeypatch, caplog):
    monkeypatch.setattr(digest, '_spawn', main_env['digest_spawn'].append)
    scheduler_args = []
    monkeypatch.setattr(digest, 'run_scheduler', lambda *a: scheduler_args.append(a))

    with caplog.at_level(logging.INFO, logger='bot'):
        bot_module.main()

    assert main_env['polling'] == 1
    assert len(main_env['digest_spawn']) == 1
    main_env['digest_spawn'][0]()
    ((send, owner_id, is_allowed),) = scheduler_args
    assert send is bot_module.digest_send and owner_id == OWNER
    assert is_allowed(OWNER) and is_allowed(FRIEND) and not is_allowed(STRANGER)
    assert any('18:00' in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    # The default menu has /digest but not /digest_now
    (menu,) = [a[0] for name, a, kw in tg if name == 'set_my_commands']
    names = [c.command for c in menu]
    assert 'digest' in names and 'digest_now' not in names


def test_main_scheduler_start_failure_does_not_stop_polling(main_env, monkeypatch, caplog):
    def broken(fn):
        raise RuntimeError('no threads')
    monkeypatch.setattr(digest, '_spawn', broken)

    with caplog.at_level(logging.ERROR, logger='bot'):
        bot_module.main()

    assert main_env['polling'] == 1
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_digest_scheduler_spawn_is_daemon(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, daemon, name=None):
            started.append(daemon)

        def start(self):
            pass
    monkeypatch.setattr(digest.threading, 'Thread', FakeThread)

    digest._spawn(lambda: None)

    assert started == [True]


def test_send_adapter_converts_rows_to_markup(monkeypatch):
    calls = []
    monkeypatch.setattr(bot_module.bot, 'send_message', lambda *a, **kw: calls.append((a, kw)))

    bot_module.digest_send(5, 'text', None)
    rows = [[('Саммари 1', 'hl:s:1:1')], [('Ещё 10 ▶', 'hl:m:1:0')]]
    bot_module.digest_send(5, 'text2', rows)

    assert calls[0] == ((5, 'text'), {'reply_markup': None})
    markup = calls[1][1]['reply_markup']
    assert isinstance(markup, types.InlineKeyboardMarkup)
    assert [[(b.text, b.callback_data) for b in row] for row in markup.keyboard] == rows

    error = ApiTelegramException('sendMessage', None,
                                 {'ok': False, 'error_code': 403, 'description': 'Forbidden: bot was blocked'})

    def fail(*a, **kw):
        raise error
    monkeypatch.setattr(bot_module.bot, 'send_message', fail)
    with pytest.raises(ApiTelegramException):
        bot_module.digest_send(5, 'text', rows)


def test_menus_and_help_mention_digest():
    default = [c for c, _ in bot_module.BOT_COMMANDS]
    owner = [c for c, _ in bot_module.OWNER_COMMANDS]
    assert 'digest' in default and 'digest_now' not in default
    assert 'digest' in owner and 'digest_now' in owner
    for kept in ('subscribe', 'subscriptions', 'feed', 'users', 'wipe'):
        assert kept in owner
    assert '/digest' in bot_module.MSG_HELP and '/digest_now' not in bot_module.MSG_HELP
    assert '/digest_now' in bot_module.MSG_HELP_OWNER
    assert 'дайджест' in bot_module.MSG_START.lower() and '/digest_now' not in bot_module.MSG_START


def test_owner_menu_includes_digest_now(tg, monkeypatch):
    monkeypatch.setattr(bot_module.bot, 'owner_menu_set', False)

    bot_module._ensure_owner_menu()

    ((name, args, kwargs),) = [s for s in tg if s[0] == 'set_my_commands']
    assert 'digest_now' in [c.command for c in args[0]]
    assert isinstance(kwargs['scope'], types.BotCommandScopeChat)
