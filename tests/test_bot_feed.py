"""
Bot handlers for hub subscriptions, /feed and the persistent list buttons (Task 5).

Real telebot dispatch through `tg` (conftest). Habr is faked at the habr_api public
fetchers (the autouse guard blocks real HTTP); `process_article` and
`check_model_availability` are patched on `bot`; `bot._spawn` runs the "Саммари"
worker synchronously, or captures it so waiters can join before it runs.
"""

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
import telebot
from telebot import types

import bot as bot_module
import feed
import habr_api
import habr_store
import hubs
from conftest import FRIEND, STRANGER, _buttons, _callback_update, _message_update, _sent_to

OWNER = bot_module.OWNER_ID
OTHER = 4000                     # a second approved non-owner user
HABR_ID = 123
CANONICAL = habr_api.canonical_url(HABR_ID)
TODAY = date(2026, 11, 4)
RLO, RLM = chr(0x202E), chr(0x200F)          # bidi override / right-to-left mark


# =============================================================================
# Fakes and helpers
# =============================================================================


def _hub(alias, rating, title=None):
    return {'alias': alias, 'title': title or alias.title(), 'rating': rating, 'hub_type': 'collective'}


# 35 hubs: python is the top one, rust second; 1C and bitrix exist for case tests
CATALOG = ([_hub('python', 1000), _hub('rust', 999), _hub('1C', 998), _hub('bitrix', 997)]
           + [_hub(f'hub{i:02d}', 900 - i) for i in range(31)])


def _item(habr_id, hours_ago=1.0, score=0, hubs_=(('python', 'collective'),), tags=('t',)):
    return {
        'habr_id': habr_id, 'title': f'Article {habr_id}', 'url': habr_api.canonical_url(habr_id),
        'published_at': datetime.now(timezone.utc) - timedelta(hours=hours_ago), 'score': score,
        'comments': 1, 'hubs': list(hubs_), 'tags': list(tags), 'lead_text': '<p>lead</p>',
        'post_type': 'article',
    }


class FakeHabr:
    """Records every habr_api call made by the handlers; each fetcher can be told to fail."""

    def __init__(self):
        self.catalog = list(CATALOG)
        self.items: list[dict] = []
        self.article: dict | None = None
        self.catalog_error = self.list_error = self.article_error = None
        self.calls: list[tuple] = []

    def fetch_hub_catalog(self, *, wait_timeout=None, deadline=None):
        self.calls.append(('catalog', wait_timeout))
        if self.catalog_error:
            raise self.catalog_error
        return list(self.catalog)

    def fetch_articles_since(self, since, *, wait_timeout=None, deadline=None):
        self.calls.append(('list', wait_timeout))
        if self.list_error:
            raise self.list_error
        return list(self.items)

    def fetch_article(self, habr_id, *, wait_timeout=None, deadline=None):
        self.calls.append(('article', habr_id, wait_timeout))
        if self.article_error:
            raise self.article_error
        return self.article or _item(habr_id)

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


class FakeProcess:
    """Stand-in for bot.process_article (the LLM path)."""

    def __init__(self):
        self.calls: list[str] = []
        self.error: Exception | None = None

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        if self.error:
            raise self.error
        return 'NEW SUMMARY', {'url': url, 'source': 'habr', 'title': 'T', 'content': 'C'}


@pytest.fixture
def habr(tg, monkeypatch):
    fake = FakeHabr()
    for name in ('fetch_hub_catalog', 'fetch_articles_since', 'fetch_article'):
        monkeypatch.setattr(habr_api, name, getattr(fake, name))
    return fake


@pytest.fixture
def llm(tg, tmp_db, monkeypatch):
    process = FakeProcess()
    process.model_checks = []

    def check(model, provider):
        process.model_checks.append((model, provider))
        return True, ''
    monkeypatch.setattr(bot_module, 'process_article', process)
    monkeypatch.setattr(bot_module, 'check_model_availability', check)
    tmp_db.create_access_request(OTHER, 'other', 'Другой')
    tmp_db.set_user_status(OTHER, 'approved')
    return process


@pytest.fixture
def sync_spawn(monkeypatch):
    monkeypatch.setattr(bot_module, '_spawn', lambda fn, name: fn())


@pytest.fixture
def captured_spawn(monkeypatch):
    jobs = []
    monkeypatch.setattr(bot_module, '_spawn', lambda fn, name: jobs.append(fn))
    return jobs


def _send(user_id, text, update_id=1):
    bot_module.bot.process_new_updates([_message_update(user_id, text, update_id=update_id)])


def _press(user_id, data, update_id=2):
    bot_module.bot.process_new_updates([_callback_update(user_id, data, update_id=update_id)])


def _press_with_markup(user_id, data, keyboard, update_id=3):
    raw = {
        'update_id': update_id,
        'callback_query': {
            'id': 'cb2', 'chat_instance': 'x', 'data': data,
            'from': {'id': user_id, 'is_bot': False, 'first_name': 'U'},
            'message': {
                'message_id': 11, 'date': 1, 'chat': {'id': user_id, 'type': 'private'}, 'text': 'old',
                'reply_markup': {'inline_keyboard': [
                    [{'text': t, 'callback_data': d} for t, d in row] for row in keyboard
                ]},
            },
        },
    }
    bot_module.bot.process_new_updates([types.Update.de_json(raw)])


def _texts(sent, chat_id):
    return [s[1][1] for s in _sent_to(sent, chat_id)]


def _answers(sent):
    return [(s[1][1] if len(s[1]) > 1 else s[2].get('text')) for s in sent if s[0] == 'answer_callback_query']


def _query(db, sql, params=()):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _execute(db, sql, params=()):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        with conn:
            conn.execute(sql, params)
    finally:
        conn.close()


def _views(db, user_id):
    return _query(db, 'SELECT habr_id, source FROM article_views WHERE user_id = ? ORDER BY habr_id',
                  (user_id,))


def _list_for(user_id, habr_ids=(HABR_ID,), kind='feed'):
    habr_store.upsert_habr_articles([_item(i) for i in habr_ids])
    return feed.create_list(user_id, kind, TODAY, list(habr_ids))


def _rows_of(message_call):
    markup = message_call[2].get('reply_markup')
    return [[b.callback_data for b in row] for row in markup.keyboard] if markup else []


def _with_catalog():
    habr_store.replace_hub_catalog(CATALOG)


# =============================================================================
# /subscribe
# =============================================================================


def test_subscribe_with_args_adds_found_and_reports_missing(tg, habr):
    _send(FRIEND, '/subscribe python rust xyz123')

    assert hubs.list_subscriptions(FRIEND) == ['python', 'rust']
    text = '\n'.join(_texts(tg, FRIEND))
    assert 'python' in text and 'rust' in text
    assert 'Не найдены: xyz123' in text
    assert habr.count('catalog') == 1
    assert habr.calls[0] == ('catalog', habr_api.HANDLER_WAIT_TIMEOUT)


def test_subscribe_mixed_case_stores_canonical_case(tg, habr):
    _send(FRIEND, '/subscribe PYTHON 1c Bitrix')

    assert sorted(hubs.list_subscriptions(FRIEND)) == ['1C', 'bitrix', 'python']


def test_subscribe_hub_url_reaches_command(tg, habr, llm):
    _send(FRIEND, '/subscribe https://habr.com/ru/hubs/python/')

    assert hubs.list_subscriptions(FRIEND) == ['python']
    assert llm.calls == [] and llm.model_checks == []
    assert not any('Не найдены' in t for t in _texts(tg, FRIEND))


def test_subscribe_repeat_is_not_error(tg, habr):
    _send(FRIEND, '/subscribe python')
    _send(FRIEND, '/subscribe python python', update_id=5)

    assert hubs.list_subscriptions(FRIEND) == ['python']
    texts = _texts(tg, FRIEND)
    assert len(texts) == 2
    assert not any('Ошибка' in t or 'Не найдены' in t for t in texts)


def test_subscribe_without_args_shows_top30_keyboard(tg, habr):
    _send(FRIEND, '/subscribe')

    msgs = _sent_to(tg, FRIEND)
    assert len(msgs) == 1
    data = _buttons(msgs[0][2]['reply_markup'])
    expected = [h['alias'] for h in sorted(CATALOG, key=lambda h: -h['rating'])[:30]]
    assert data == [f'hs:a:{a}' for a in expected]
    assert all(len(d.encode('utf-8')) <= 64 for d in data)

    _press(FRIEND, data[1])
    assert hubs.list_subscriptions(FRIEND) == ['rust']
    assert 'Подписка на rust оформлена' in _answers(tg)


def test_subscribe_long_reply_split_under_limit(tg, habr):
    aliases = [f'{"a" * 45}{i:04d}' for i in range(150)]
    habr.catalog = [_hub(a, 1) for a in aliases]

    _send(FRIEND, '/subscribe ' + ' '.join(aliases))

    assert len(hubs.list_subscriptions(FRIEND)) == 150
    texts = _texts(tg, FRIEND)
    assert len(texts) > 1
    assert all(len(t) <= bot_module.TELEGRAM_MAX_MESSAGE_LENGTH for t in texts)
    joined = ' '.join(texts)
    assert all(a in joined for a in aliases)


def test_subscribe_only_unknown_tokens(tg, habr):
    _send(FRIEND, '/subscribe xyz123 abc')

    assert hubs.list_subscriptions(FRIEND) == []
    assert _texts(tg, FRIEND) == ['Не найдены: xyz123, abc']


def test_subscribe_button_already_subscribed(tg, habr):
    _with_catalog()
    habr_store.add_subscription(FRIEND, 'python')

    _press(FRIEND, 'hs:a:python')

    assert _answers(tg) == ['Ты уже подписан на python']
    assert hubs.list_subscriptions(FRIEND) == ['python']


def test_subscribe_db_error_reported(tg, habr, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(habr_store, 'add_subscription', broken)

    _send(FRIEND, '/subscribe python')

    assert _texts(tg, FRIEND) == [bot_module.MSG_ERROR.format(error='OperationalError')]


def test_subscribe_unexpected_catalog_error_friendly(tg, habr, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError('secret detail')
    monkeypatch.setattr(hubs, 'ensure_catalog', broken)

    _send(FRIEND, '/subscribe python')

    assert _texts(tg, FRIEND) == [bot_module.MSG_CATALOG_UNAVAILABLE]


def test_subscribe_stale_catalog_used_when_habr_busy(tg, habr, monkeypatch):
    _with_catalog()
    monkeypatch.setattr(hubs, 'CATALOG_MAX_AGE', timedelta(0))
    habr.catalog_error = habr_api.HabrBusy('busy')

    _send(FRIEND, '/subscribe rust')

    assert hubs.list_subscriptions(FRIEND) == ['rust']
    assert habr.count('catalog') == 1


def test_subscribe_catalog_unavailable_message(tg, habr):
    habr.catalog_error = habr_api.HabrUnavailable('http', 503)

    _send(FRIEND, '/subscribe python')

    assert _texts(tg, FRIEND) == [bot_module.MSG_CATALOG_UNAVAILABLE]
    assert bot_module.MSG_CATALOG_UNAVAILABLE.startswith('Не удалось получить каталог хабов')
    assert hubs.list_subscriptions(FRIEND) == []


def test_subscribe_stale_catalog_used_silently(tg, habr, tmp_db, monkeypatch):
    _with_catalog()
    monkeypatch.setattr(hubs, 'CATALOG_MAX_AGE', timedelta(0))
    habr.catalog_error = habr_api.HabrUnavailable('http', 503)

    _send(FRIEND, '/subscribe rust')

    assert hubs.list_subscriptions(FRIEND) == ['rust']
    assert bot_module.MSG_CATALOG_UNAVAILABLE not in _texts(tg, FRIEND)


def test_subscribe_throttle_timeout_message(tg, habr):
    habr.catalog_error = habr_api.HabrBusy('busy')
    _send(FRIEND, '/subscribe python')
    assert _texts(tg, FRIEND) == ['Хабр сейчас отвечает медленно, попробуй позже']

    habr_store.add_subscription(FRIEND, 'python')
    habr.list_error = habr_api.HabrBusy('busy')
    _send(FRIEND, '/feed', update_id=5)
    assert _texts(tg, FRIEND)[-1] == 'Хабр сейчас отвечает медленно, попробуй позже'


# =============================================================================
# Hub buttons and /subscriptions
# =============================================================================


@pytest.mark.parametrize('data', ['hs:a:nosuchhub', 'hs:a:bad alias', 'hs:a:py/thon', 'hs:a:' + 'x' * 51,
                                  'hs:a:', 'hs:x:python', 'hs:a:python\n'])
def test_hub_callback_unknown_alias_rejected(tg, habr, tmp_db, data, caplog):
    _with_catalog()

    with caplog.at_level(logging.WARNING, logger='hubs'):
        _press(FRIEND, data)

    assert _answers(tg) == ['Кнопка устарела']
    assert hubs.list_subscriptions(FRIEND) == []
    assert habr.calls == []
    assert any(f'user_id={FRIEND}' in r.message for r in caplog.records)


def test_subscriptions_list_and_unsubscribe(tg, habr, tmp_db):
    _send(FRIEND, '/subscriptions')
    assert _texts(tg, FRIEND) == ['Подписок пока нет — /subscribe']

    for alias in ('python', 'rust'):
        habr_store.add_subscription(FRIEND, alias)
    tg.clear()
    _send(FRIEND, '/subscriptions', update_id=5)
    msgs = _sent_to(tg, FRIEND)
    assert len(msgs) == 1
    markup = msgs[0][2]['reply_markup']
    buttons = [(b.text, b.callback_data) for row in markup.keyboard for b in row]
    assert buttons == [('✖ python', 'hs:d:python'), ('✖ rust', 'hs:d:rust')]

    tg.clear()
    _press_with_markup(FRIEND, 'hs:d:python', [buttons])
    assert hubs.list_subscriptions(FRIEND) == ['rust']
    edits = [s for s in tg if s[0] == 'edit_message_reply_markup']
    assert len(edits) == 1
    assert _buttons(edits[0][2]['reply_markup']) == ['hs:d:rust']

    tg.clear()
    _press_with_markup(FRIEND, 'hs:d:rust', [[('✖ rust', 'hs:d:rust')]], update_id=6)
    assert hubs.list_subscriptions(FRIEND) == []
    edits = [s for s in tg if s[0] == 'edit_message_text']
    assert edits and edits[0][1][0] == 'Подписок пока нет — /subscribe'
    assert habr.calls == []


def test_stale_unsubscribe_button_is_removed(tg, habr):
    habr_store.add_subscription(FRIEND, 'rust')

    _press_with_markup(FRIEND, 'hs:d:python', [[('✖ python', 'hs:d:python'), ('✖ rust', 'hs:d:rust')]])

    assert _answers(tg) == ['Кнопка устарела']
    edits = [c for c in tg if c[0] == 'edit_message_reply_markup']
    assert len(edits) == 1 and _buttons(edits[0][2]['reply_markup']) == ['hs:d:rust']
    assert hubs.list_subscriptions(FRIEND) == ['rust']


def test_emptied_later_page_not_redrawn_with_first_page(tg, habr):
    for alias in ('python', 'rust'):
        habr_store.add_subscription(FRIEND, alias)

    # The last button of a 2nd /subscriptions message; other subscriptions remain
    _press_with_markup(FRIEND, 'hs:d:rust', [[('✖ rust', 'hs:d:rust')]])

    edits = [c for c in tg if c[0] == 'edit_message_text']
    assert len(edits) == 1
    assert edits[0][1][0] == hubs.TEXT_SUBSCRIPTIONS_REFRESHED and edits[0][2].get('reply_markup') is None
    assert hubs.list_subscriptions(FRIEND) == ['python']


def test_unsubscribe_from_inaccessible_message(tg, habr):
    habr_store.add_subscription(FRIEND, 'python')

    _press(FRIEND, 'hs:d:python')          # date 0 -> InaccessibleMessage without a keyboard

    edits = [c for c in tg if c[0] == 'edit_message_text']
    assert [e[1][0] for e in edits] == ['Подписок пока нет — /subscribe']


def test_subscriptions_many_hubs_split_messages(tg, habr):
    aliases = [f'hub_{i:03d}_{"x" * 40}' for i in range(160)]
    for alias in aliases:
        habr_store.add_subscription(FRIEND, alias)

    _send(FRIEND, '/subscriptions')

    msgs = _sent_to(tg, FRIEND)
    assert len(msgs) == 4
    seen = []
    for msg in msgs:
        data = _buttons(msg[2]['reply_markup'])
        assert len(data) <= 50
        assert len(msg[1][1]) < bot_module.TELEGRAM_MAX_MESSAGE_LENGTH
        assert all(len(d.encode('utf-8')) <= 64 for d in data)
        seen += data
    assert seen == [f'hs:d:{a}' for a in sorted(aliases)]


def test_unsubscribe_validates_against_own_subscriptions(tg, habr, tmp_db):
    _with_catalog()
    habr_store.add_subscription(OWNER, 'rust')
    habr_store.add_subscription(FRIEND, 'gonehub')        # left the catalog since

    _press(FRIEND, 'hs:d:python')                         # in the catalog, not subscribed
    _press(FRIEND, 'hs:d:rust', update_id=3)              # someone else's subscription
    assert _answers(tg) == ['Кнопка устарела', 'Кнопка устарела']
    assert hubs.list_subscriptions(OWNER) == ['rust']

    tg.clear()
    _press(FRIEND, 'hs:d:gonehub', update_id=4)
    assert hubs.list_subscriptions(FRIEND) == []
    assert _answers(tg) == ['Отписка от gonehub']


# =============================================================================
# /feed
# =============================================================================


def test_feed_without_subscriptions(tg, habr):
    _send(FRIEND, '/feed')

    assert _texts(tg, FRIEND) == ['Подписок пока нет — /subscribe']
    assert habr.calls == []


def test_feed_empty_after_filter(tg, habr):
    habr_store.add_subscription(FRIEND, 'python')
    habr.items = [_item(1, hubs_=[('rust', 'collective')]), _item(2, hubs_=[('python', 'corporative')])]

    _send(FRIEND, '/feed')

    assert _texts(tg, FRIEND) == ['За последние сутки в твоих хабах ничего нового']


def test_feed_with_data_first_page(tg, habr, llm, tmp_db):
    habr_store.add_subscription(FRIEND, 'python')
    habr.items = [_item(i, score=100 - i) for i in range(1, 13)]

    _send(FRIEND, '/feed')

    msgs = _sent_to(tg, FRIEND)
    assert len(msgs) == 1
    text = msgs[0][1][1]
    lines = text.split('\n')
    assert len(lines) == 10
    assert lines[0].startswith('1. Article 1 — рейтинг 99')
    assert 'parse_mode' not in msgs[0][2]
    rows = _rows_of(msgs[0])
    assert [len(r) for r in rows] == [5, 5, 1]
    lists = _query(tmp_db, 'SELECT id, user_id, kind FROM ranked_lists')
    assert [(u, k) for _, u, k in lists] == [(FRIEND, 'feed')]
    list_id = lists[0][0]
    assert rows[0][0] == f'hl:s:{list_id}:1' and rows[2] == [f'hl:m:{list_id}:10']
    assert llm.calls == [] and llm.model_checks == []
    assert _views(tmp_db, FRIEND) == []
    assert habr.calls == [('list', habr_api.HANDLER_WAIT_TIMEOUT)]     # handler throttle mode


def test_feed_sends_sanitized_title(tg, habr):
    """The text actually sent by /feed: control/bidi characters gone, title capped at 200."""
    habr_store.add_subscription(FRIEND, 'python')
    evil = _item(1, score=5)
    evil['title'] = RLO + 'evil' + RLM + '\x07 ' + 'x' * 300 + '\nsecond line'
    habr.items = [evil]

    _send(FRIEND, '/feed')

    (msg,) = _sent_to(tg, FRIEND)
    text = msg[1][1]
    for bad in (RLO, RLM, '\x07'):
        assert bad not in text
    lines = text.split('\n')
    assert not any(line.startswith('second line') for line in lines)    # one line per article
    title = lines[0].split('. ', 1)[1].split(' — рейтинг')[0]
    assert title.startswith('evil x') and len(title) <= feed.TITLE_MAX_LEN
    assert 'parse_mode' not in msg[2]


def test_feed_more_10_to_the_end(tg, habr):
    habr_store.add_subscription(FRIEND, 'python')
    habr.items = [_item(i, score=100 - i) for i in range(1, 26)]
    _send(FRIEND, '/feed')
    more = _rows_of(_sent_to(tg, FRIEND)[0])[-1][0]

    tg.clear()
    _press(FRIEND, more)
    page2 = _sent_to(tg, FRIEND)
    assert len(page2) == 1
    assert page2[0][1][1].split('\n')[0].startswith('11. ')
    assert 'Это все новые статьи за сутки' not in page2[0][1][1]
    more = _rows_of(page2[0])[-1][0]
    assert more.endswith(':20')

    tg.clear()
    _press(FRIEND, more, update_id=3)
    last = _sent_to(tg, FRIEND)[0]
    lines = last[1][1].split('\n')
    assert lines[0].startswith('21. ') and lines[-1] == 'Это все новые статьи за сутки'
    assert not any(d.startswith('hl:m:') for row in _rows_of(last) for d in row)
    assert habr.count('list') == 1


def test_feed_cached_fetch_and_summarized_article_excluded(tg, habr, llm, sync_spawn):
    habr_store.add_subscription(FRIEND, 'python')
    habr.items = [_item(1, score=10), _item(2, score=5)]
    _send(FRIEND, '/feed')
    first = _rows_of(_sent_to(tg, FRIEND)[0])[0][0]

    _press(FRIEND, first)
    tg.clear()
    _send(FRIEND, '/feed', update_id=5)

    assert _sent_to(tg, FRIEND)[0][1][1].startswith('1. Article 2')
    assert habr.count('list') == 1


def test_feed_unexpected_error_friendly(tg, habr):
    habr_store.add_subscription(FRIEND, 'python')
    habr.list_error = RuntimeError('secret detail')

    _send(FRIEND, '/feed')

    assert _texts(tg, FRIEND) == [bot_module.MSG_FEED_UNAVAILABLE]


def test_feed_habr_failure_friendly_message(tg, habr):
    habr_store.add_subscription(FRIEND, 'python')
    habr.list_error = habr_api.HabrUnavailable('http', 503, detail='boom-detail')

    _send(FRIEND, '/feed')

    texts = _texts(tg, FRIEND)
    assert texts == [bot_module.MSG_FEED_UNAVAILABLE]
    assert not any(s in texts[0] for s in ('503', 'boom', 'HTTP', 'Habr unavailable'))


# =============================================================================
# List buttons: validation
# =============================================================================


def test_forged_callback_rejected_without_io(tg, habr, llm, tmp_db, caplog, sync_spawn):
    own = _list_for(FRIEND, (1, 2, 3))
    foreign = _list_for(OWNER, (1,))
    expired = _list_for(FRIEND, (1,))
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec='microseconds')
    _execute(tmp_db, 'UPDATE ranked_lists SET created_at = ? WHERE id = ?', (old, expired))

    forged = [
        'hl:s:abc:1', 'hl:s:99999999999999999999:1', f'hl:s:{own}:99999999999', f'hl:s:0{own}:1',
        f'hl:s:{own}:01', f'hl:s:{own}:١', f'hl:x:{own}:1', f'hl:s:{own}:1:2', 'hl:',
        f'hl:s:{foreign}:1', f'hl:s:{expired}:1', f'hl:s:{own}:4', f'hl:s:{own}:0',
        f'hl:m:{own}:0', f'hl:m:{own}:15', f'hl:m:{own}:10', 'hl:s:999999:1',
    ]
    with caplog.at_level(logging.WARNING, logger='feed'):
        for n, data in enumerate(forged):
            _press(FRIEND, data, update_id=10 + n)

    assert _answers(tg) == ['Кнопка устарела'] * len(forged)
    assert habr.calls == [] and llm.calls == [] and llm.model_checks == []
    assert _sent_to(tg, FRIEND) == []
    assert _views(tmp_db, FRIEND) == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and f'user_id={FRIEND}' in r.message]
    assert len(warnings) == len(forged)

    # Positive control: the same harness accepts the valid button of the same list
    tg.clear()
    _press(FRIEND, f'hl:s:{own}:1', update_id=99)
    assert _answers(tg) == [None] and llm.calls == [habr_api.canonical_url(1)]


def _answer_count(sent):
    return len([s for s in sent if s[0] == 'answer_callback_query'])


@pytest.mark.parametrize('path, expected', [
    ('more', None), ('forged', 'Кнопка устарела'), ('started', None), ('already', 'Саммари уже готовится'),
    ('hub_add', 'Подписка на python оформлена'), ('hub_stale', 'Кнопка устарела'),
])
def test_callback_answered_exactly_once(tg, habr, llm, captured_spawn, path, expected):
    _with_catalog()
    list_id = _list_for(FRIEND, tuple(range(1, 16)))
    if path == 'already':
        feed.REGISTRY.click(1, FRIEND)
    data = {'more': f'hl:m:{list_id}:10', 'forged': 'hl:s:x:1', 'started': f'hl:s:{list_id}:1',
            'already': f'hl:s:{list_id}:1', 'hub_add': 'hs:a:python', 'hub_stale': 'hs:d:python'}[path]

    _press(FRIEND, data)

    assert _answer_count(tg) == 1
    assert _answers(tg) == [expected]


def test_stale_answer_error_does_not_crash(tg, habr, llm, monkeypatch):
    list_id = _list_for(FRIEND, tuple(range(1, 16)))

    def stale(*a, **k):
        tg.append(('answer_callback_query', a, k))
        raise telebot.apihelper.ApiTelegramException(
            'answerCallbackQuery', '', {'error_code': 400, 'description': 'query is too old'})
    monkeypatch.setattr(bot_module.bot, 'answer_callback_query', stale)

    _press(FRIEND, f'hl:m:{list_id}:10')

    assert _answer_count(tg) == 1
    assert len(_sent_to(tg, FRIEND)) == 1              # the page is still sent


def test_not_modified_edit_error_does_not_crash(tg, habr, monkeypatch):
    def not_modified(*a, **k):
        tg.append(('edit_message_reply_markup', a, k))
        raise telebot.apihelper.ApiTelegramException(
            'editMessageReplyMarkup', '', {'error_code': 400, 'description': 'message is not modified'})
    monkeypatch.setattr(bot_module.bot, 'edit_message_reply_markup', not_modified)
    habr_store.add_subscription(FRIEND, 'python')
    habr_store.add_subscription(FRIEND, 'rust')

    _press_with_markup(FRIEND, 'hs:d:python', [[('✖ python', 'hs:d:python'), ('✖ rust', 'hs:d:rust')]])

    assert _answers(tg) == ['Отписка от python']
    assert len([s for s in tg if s[0] == 'edit_message_reply_markup']) == 1
    assert hubs.list_subscriptions(FRIEND) == ['rust']


# =============================================================================
# "Саммари" button
# =============================================================================


def _save_cached(db, habr_id=HABR_ID, summary='CACHED SUMMARY'):
    return db.save_article({'url': habr_api.canonical_url(habr_id), 'source': 'habr', 'title': 'T',
                            'content': 'C'}, summary, 'm', OWNER, habr_id=habr_id)


def test_summary_button_cached(tg, habr, llm, tmp_db, sync_spawn):
    _save_cached(tmp_db)
    tmp_db.create_idea('Идея', None, FRIEND)
    list_id = _list_for(FRIEND)

    _press(FRIEND, f'hl:s:{list_id}:1')

    texts = _texts(tg, FRIEND)
    assert any(t.startswith('📦 Сохранённый конспект:') and 'CACHED SUMMARY' in t for t in texts)
    assert bot_module.MSG_LINK_SELECT_IDEAS in texts
    assert llm.calls == [] and llm.model_checks == []
    assert _views(tmp_db, FRIEND) == [(HABR_ID, 'feed')]
    assert _answers(tg) == [None]
    assert len(feed.REGISTRY) == 0


def _idea_links(db):
    return _query(db, 'SELECT article_id, idea_id FROM idea_articles ORDER BY idea_id')


def _edits(sent, chat_id):
    return [s[1][0] for s in sent if s[0] == 'edit_message_text' and s[2].get('chat_id') == chat_id]


def test_summary_on_other_users_cached_article_then_link_to_own_idea(tg, habr, llm, tmp_db, sync_spawn):
    """Decision 20 end to end: FRIEND opens OWNER's cached row via «Саммари» and links it."""
    article_id = _save_cached(tmp_db)                    # OWNER's row for HABR_ID
    idea = tmp_db.create_idea('Идея', None, FRIEND)
    list_id = _list_for(FRIEND)

    _press(FRIEND, f'hl:s:{list_id}:1')
    assert bot_module.MSG_LINK_SELECT_IDEAS in _texts(tg, FRIEND)
    assert bot_module.pending_article_links[FRIEND]['article_id'] == article_id
    assert bot_module.pending_article_links[FRIEND]['created_now'] is False

    _press(FRIEND, f'toggle_link:{idea}', update_id=3)
    _press(FRIEND, 'link_done', update_id=4)

    assert _idea_links(tmp_db) == [(article_id, idea)]
    assert _edits(tg, FRIEND) == [bot_module.MSG_LINK_DONE.format(ideas='Идея')]
    assert FRIEND not in bot_module.pending_article_links
    # The row is still OWNER's: linking never transfers ownership
    assert _query(tmp_db, 'SELECT user_id FROM articles WHERE id = ?', (article_id,)) == [(OWNER,)]


def test_link_done_without_view_row_creates_no_link(tg, habr, llm, tmp_db):
    """Same session for FRIEND, but no «Саммари» click: no article_views row, no link."""
    article_id = _save_cached(tmp_db)
    idea = tmp_db.create_idea('Идея', None, FRIEND)
    bot_module.pending_article_links[FRIEND] = {
        'article_id': article_id, 'selected_ideas': set(), 'created_now': False,
    }

    _press(FRIEND, f'toggle_link:{idea}', update_id=3)
    _press(FRIEND, 'link_done', update_id=4)

    assert _views(tmp_db, FRIEND) == []
    assert _idea_links(tmp_db) == []
    assert _edits(tg, FRIEND) == [bot_module.MSG_LINK_SKIPPED]
    assert FRIEND not in bot_module.pending_article_links


def test_summary_button_new_generates_once_and_saves(tg, habr, llm, tmp_db, sync_spawn):
    list_id = _list_for(FRIEND)

    _press(FRIEND, f'hl:s:{list_id}:1')

    assert llm.calls == [CANONICAL]
    assert llm.model_checks == [bot_module.get_user_model(FRIEND)]
    rows = _query(tmp_db, 'SELECT habr_id, summary FROM articles')
    assert rows == [(HABR_ID, 'NEW SUMMARY')]
    assert any('NEW SUMMARY' in t for t in _texts(tg, FRIEND))
    assert len(feed.REGISTRY) == 0
    assert ('article', HABR_ID, None) in habr.calls       # background throttle mode


def test_summary_button_unavailable_404(tg, habr, llm, tmp_db, sync_spawn):
    habr.article_error = habr_api.HabrNotFound('NOT_FOUND')
    list_id = _list_for(FRIEND)

    _press(FRIEND, f'hl:s:{list_id}:1')

    assert _texts(tg, FRIEND) == ['Статья недоступна на Хабре — возможно, её удалили или скрыли']
    assert habr_store.get_habr_article(HABR_ID)['unavailable_at'] is not None
    assert llm.calls == [] and llm.model_checks == []
    assert len(feed.REGISTRY) == 0


def test_summary_button_kek_unavailable_falls_back_to_scrape(tg, habr, llm, sync_spawn):
    habr.article_error = habr_api.HabrUnavailable('timeout')
    list_id = _list_for(FRIEND)

    _press(FRIEND, f'hl:s:{list_id}:1')

    assert llm.calls == [CANONICAL]
    assert any('NEW SUMMARY' in t for t in _texts(tg, FRIEND))


def test_summary_button_stores_hubs_tags_from_precheck(tg, habr, llm, sync_spawn):
    list_id = _list_for(FRIEND)
    habr.article = _item(HABR_ID, hubs_=[('rust', 'collective'), ('megacorp', 'corporative')],
                         tags=['async', 'tokio'])

    _press(FRIEND, f'hl:s:{list_id}:1')

    meta = habr_store.get_habr_article(HABR_ID)
    assert meta['hubs'] == [{'alias': 'megacorp', 'hub_type': 'corporative'},
                            {'alias': 'rust', 'hub_type': 'collective'}]
    assert meta['tags'] == ['async', 'tokio']
    assert habr.count('article') == 1


@pytest.mark.parametrize('kind, source', [('digest_site', 'digest'), ('digest_hubs', 'digest'),
                                          ('feed', 'feed')])
def test_view_source_mapping_all_list_kinds(tg, habr, llm, tmp_db, sync_spawn, monkeypatch, kind, source):
    kinds = []
    real = feed.view_source
    monkeypatch.setattr(feed, 'view_source', lambda k: kinds.append(k) or real(k))
    list_id = _list_for(FRIEND, kind=kind)

    _press(FRIEND, f'hl:s:{list_id}:1')

    assert _views(tmp_db, FRIEND) == [(HABR_ID, source)]
    assert kinds and set(kinds) == {kind}


def test_summary_button_same_user_twice_says_already_preparing(tg, habr, llm, tmp_db, sync_spawn):
    list_id = _list_for(FRIEND)
    feed.REGISTRY.click(HABR_ID, FRIEND)

    _press(FRIEND, f'hl:s:{list_id}:1')

    assert _answers(tg) == ['Саммари уже готовится']
    assert llm.calls == [] and habr.calls == []
    assert _views(tmp_db, FRIEND) == [(HABR_ID, 'feed')]


def test_summary_button_other_user_becomes_waiter(tg, habr, llm, captured_spawn):
    friend_list = _list_for(FRIEND)
    owner_list = _list_for(OWNER)

    _press(FRIEND, f'hl:s:{friend_list}:1')
    _press(OWNER, f'hl:s:{owner_list}:1', update_id=3)
    assert _answers(tg) == [None, 'Саммари уже готовится, пришлю сюда']
    assert len(captured_spawn) == 1 and llm.calls == []

    captured_spawn[0]()

    assert llm.calls == [CANONICAL]
    friend_texts = [t for t in _texts(tg, FRIEND) if 'NEW SUMMARY' in t]
    owner_texts = [t for t in _texts(tg, OWNER) if 'NEW SUMMARY' in t]
    assert friend_texts and friend_texts == owner_texts
    assert len(feed.REGISTRY) == 0


def test_waiter_send_failure_does_not_stop_others(tg, habr, llm, captured_spawn, monkeypatch):
    lists = {user: _list_for(user) for user in (FRIEND, OWNER, OTHER)}
    for n, user in enumerate((FRIEND, OWNER, OTHER)):
        _press(user, f'hl:s:{lists[user]}:1', update_id=10 + n)
    recorded = bot_module.bot.send_message

    def send(chat_id, *a, **k):
        if chat_id == OWNER:
            raise telebot.apihelper.ApiTelegramException(
                'sendMessage', '', {'error_code': 403, 'description': 'bot was blocked by the user'})
        return recorded(chat_id, *a, **k)
    monkeypatch.setattr(bot_module.bot, 'send_message', send)

    captured_spawn[0]()

    assert any('NEW SUMMARY' in t for t in _texts(tg, OTHER))
    assert any('NEW SUMMARY' in t for t in _texts(tg, FRIEND))
    assert len(feed.REGISTRY) == 0


def test_summary_generation_failure_notifies_all_and_clears_registry(tg, habr, llm, captured_spawn):
    llm.error = RuntimeError('scrape failed')
    friend_list, owner_list = _list_for(FRIEND), _list_for(OWNER)
    _press(FRIEND, f'hl:s:{friend_list}:1')
    _press(OWNER, f'hl:s:{owner_list}:1', update_id=3)

    captured_spawn[0]()

    expected = bot_module.MSG_ERROR.format(error='RuntimeError')
    # The clicker sees the error in place of its "Обрабатываю..." status message (link-flow behaviour)
    edits = [(c[1][0], c[2]['chat_id']) for c in tg if c[0] == 'edit_message_text']
    assert edits == [(expected, FRIEND)]
    assert _texts(tg, OWNER)[-1] == expected
    assert len(feed.REGISTRY) == 0


def test_summary_worker_exception_reaches_all_and_clears_registry(tg, habr, llm, captured_spawn,
                                                                  monkeypatch):
    def broken(*a, **k):
        raise RuntimeError('unexpected')
    monkeypatch.setattr(bot_module, '_summarize_for_user', broken)
    friend_list, owner_list = _list_for(FRIEND), _list_for(OWNER)
    _press(FRIEND, f'hl:s:{friend_list}:1')
    _press(OWNER, f'hl:s:{owner_list}:1', update_id=3)

    captured_spawn[0]()

    expected = bot_module.MSG_ERROR.format(error='RuntimeError')
    assert _texts(tg, FRIEND)[-1] == expected and _texts(tg, OWNER)[-1] == expected
    assert len(feed.REGISTRY) == 0


def test_spawn_failure_clears_registry(tg, habr, llm, monkeypatch):
    def no_thread(fn, name):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(bot_module, '_spawn', no_thread)
    list_id = _list_for(FRIEND)

    _press(FRIEND, f'hl:s:{list_id}:1')

    assert len(feed.REGISTRY) == 0
    assert _texts(tg, FRIEND) == [bot_module.MSG_ERROR.format(error='RuntimeError')]


def test_spawn_runs_daemon_thread(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, daemon, name):
            started.append((target, daemon, name))

        def start(self):
            started.append('start')
    monkeypatch.setattr(bot_module.threading, 'Thread', FakeThread)
    job = lambda: None  # noqa: E731

    bot_module._spawn(job, 'summary-1')

    assert started == [(job, True, 'summary-1'), 'start']


def test_summary_thread_named_by_habr_id(tg, habr, llm, monkeypatch):
    names = []
    monkeypatch.setattr(bot_module, '_spawn', lambda fn, name: names.append(name))
    list_id = _list_for(FRIEND)

    _press(FRIEND, f'hl:s:{list_id}:1')

    assert names == [f'summary-{HABR_ID}']


def test_reset_for_tests_clears_registry_used_by_bot(tg, habr, llm, captured_spawn):
    list_id = _list_for(FRIEND)
    _press(FRIEND, f'hl:s:{list_id}:1')
    assert len(feed.REGISTRY) == 1                # HABR_ID in flight (job captured, not run)
    assert not [v for v in vars(bot_module).values() if isinstance(v, feed.InFlightRegistry)]

    feed._reset_for_tests()

    assert len(feed.REGISTRY) == 0


# =============================================================================
# Glue, menus, access
# =============================================================================


def test_to_markup_converts_rows():
    rows = [[('1', 'hl:s:1:1'), ('2', 'hl:s:1:2')], [('Ещё 10 ▶', 'hl:m:1:10')]]

    markup = bot_module._to_markup(rows)

    assert isinstance(markup, types.InlineKeyboardMarkup)
    assert [[(b.text, b.callback_data) for b in row] for row in markup.keyboard] == rows
    with pytest.raises(ValueError):
        bot_module._to_markup([[('x', 'hs:a:' + 'я' * 30)]])
    assert bot_module._to_markup([]).keyboard == []


def test_commands_menu_and_help_list_new_commands():
    commands = ('subscribe', 'subscriptions', 'feed')
    for names in ([c for c, _ in bot_module.BOT_COMMANDS], [c for c, _ in bot_module.OWNER_COMMANDS]):
        assert all(c in names for c in commands)
    for text in (bot_module.MSG_HELP, bot_module.MSG_HELP_OWNER, bot_module.MSG_START):
        assert all(f'/{c}' in text for c in commands)
    for is_owner in (False, True):
        keyboard = bot_module.create_main_keyboard(is_owner=is_owner).keyboard
        labels = [b['text'] for row in keyboard for b in row]
        assert all(f'/{c}' in labels for c in commands)
        assert all(len(row) <= 3 for row in keyboard)


@pytest.mark.parametrize('text', ['/subscribe python', '/subscriptions', '/feed'])
def test_new_handlers_respect_access_gate(tg, habr, text):
    _send(STRANGER, text)

    assert _texts(tg, STRANGER) == [bot_module.MSG_ACCESS_OFFER]
    assert habr.calls == []


@pytest.mark.parametrize('data', ['hl:s:1:1', 'hl:m:1:10', 'hs:a:python', 'hs:d:python'])
def test_new_handlers_respect_access_gate_callbacks(tg, habr, tmp_db, data):
    _with_catalog()
    _list_for(STRANGER)
    habr_store.add_subscription(STRANGER, 'python')

    _press(STRANGER, data)

    assert _answers(tg) == [bot_module.MSG_ACCESS_DENIED_CALLBACK]
    assert habr_store.list_subscriptions(STRANGER) == ['python']
    assert habr.calls == []
