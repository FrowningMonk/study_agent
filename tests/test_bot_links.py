"""
Link flow keyed by the Habr id (Task 3): strict routing, canonical fetch, cache by
habr_id, `link` views, the shared summary core and the restricted "Не привязывать".

Bot tests run through real telebot dispatch (`tg`); `process_article` and
`check_model_availability` are patched on `bot`. Habr HTTP is blocked by the
autouse guard in conftest.
"""

import logging
import sqlite3
from datetime import datetime, timezone

import pytest

import bot as bot_module
import habr_api
import habr_store
import pipeline
from conftest import FRIEND, _buttons, _callback_update, _message_update, _sent_to

OWNER = bot_module.OWNER_ID
CANONICAL_123 = 'https://habr.com/ru/articles/123/'


def _query(db, sql, params=()):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _views(db, user_id=None):
    sql = 'SELECT user_id, habr_id, source FROM article_views WHERE source = ?'
    params: tuple = ('link',)
    if user_id is not None:
        sql += ' AND user_id = ?'
        params += (user_id,)
    return _query(db, sql, params)


def _article_count(db):
    return _query(db, 'SELECT COUNT(*) FROM articles')[0][0]


def _save_habr(db, url, habr_id, user_id=OWNER, summary='CACHED SUMMARY'):
    return db.save_article(
        {'url': url, 'source': 'habr', 'title': 'T', 'content': 'C'}, summary, 'm', user_id, habr_id=habr_id,
    )


class FakeProcess:
    """Stand-in for bot.process_article: records URLs, returns a fixed result or fails."""

    def __init__(self, summary='NEW SUMMARY', result=True, error=None):
        self.calls: list[str] = []
        self.summary = summary
        self.result = result
        self.error = error

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        if self.error:
            raise self.error
        if not self.result:
            return None
        return self.summary, {'url': url, 'source': 'habr', 'title': 'T', 'content': 'C'}


@pytest.fixture
def fake(monkeypatch):
    process = FakeProcess()
    model_checks: list = []

    def check(model, provider):
        model_checks.append((model, provider))
        return True, ''
    monkeypatch.setattr(bot_module, 'process_article', process)
    monkeypatch.setattr(bot_module, 'check_model_availability', check)
    process.model_checks = model_checks
    return process


def _send(user_id, text, update_id=1):
    bot_module.bot.process_new_updates([_message_update(user_id, text, update_id=update_id)])


def _press(user_id, data, update_id=2):
    bot_module.bot.process_new_updates([_callback_update(user_id, data, update_id=update_id)])


def _texts(sent, chat_id):
    return [s[1][1] for s in _sent_to(sent, chat_id)]


def _markups(sent, chat_id):
    return [s[2].get('reply_markup') for s in _sent_to(sent, chat_id)]


def _cache_buttons(sent, chat_id):
    msgs = [s for s in _sent_to(sent, chat_id) if s[1][1] == bot_module.MSG_DUPLICATE_FOUND]
    assert len(msgs) == 1
    return _buttons(msgs[0][2]['reply_markup'])


# --- cache by habr_id ---------------------------------------------------------


def test_utm_link_hits_cache_by_habr_id(tg, tmp_db, fake):
    _save_habr(tmp_db, CANONICAL_123, 123)

    _send(FRIEND, 'https://habr.com/ru/articles/123/?utm_source=x#comments')

    buttons = _cache_buttons(tg, FRIEND)
    assert [b.split(':')[1] for b in buttons] == ['show', 'regen']
    assert fake.calls == []
    assert _views(tmp_db, FRIEND) == [(FRIEND, 123, 'link')]


@pytest.mark.parametrize('variant', [
    'https://habr.com/ru/companies/x/articles/123/',
    'https://habr.com/ru/post/123/',
    'https://habr.com/ru/articles/123/comments/',
    'https://m.habr.com/ru/articles/123/',
    'https://www.habr.com/en/articles/123/',
])
def test_company_blog_and_post_forms_hit_same_cache(tg, tmp_db, fake, variant):
    _save_habr(tmp_db, CANONICAL_123, 123)

    _send(FRIEND, variant)

    assert _cache_buttons(tg, FRIEND)
    assert fake.calls == []
    assert _article_count(tmp_db) == 1


def test_link_view_recorded_on_cache_miss_and_failure(tg, tmp_db, fake):
    _send(FRIEND, 'https://habr.com/ru/articles/5/')
    assert _views(tmp_db, FRIEND) == [(FRIEND, 5, 'link')]
    assert any(t.startswith('Готово!') for t in _texts(tg, FRIEND))

    fake.result = False
    tg.clear()
    _send(FRIEND, 'https://habr.com/ru/articles/6/', update_id=3)
    assert (FRIEND, 6, 'link') in _views(tmp_db, FRIEND)
    error = bot_module.MSG_ERROR.format(error='Не удалось обработать статью')
    assert [s for s in tg if s[0] == 'edit_message_text'][0][1][0] == error


def test_link_view_recorded_when_model_unavailable(tg, tmp_db, fake, monkeypatch):
    monkeypatch.setattr(bot_module, 'check_model_availability', lambda m, p: (False, 'down'))
    _send(FRIEND, 'https://habr.com/ru/articles/7/')
    assert _views(tmp_db, FRIEND) == [(FRIEND, 7, 'link')]
    assert _texts(tg, FRIEND)[0].startswith('Модель ')
    assert fake.calls == []


@pytest.mark.parametrize('url', [
    'http://169.254.169.254/?habr.com',
    'https://evilhabr.com/ru/articles/1/',
    'https://habr.com@evil.com/ru/articles/1/',
    'https://habr.com/ru/hubs/python/',
    'https://habr.com/ru/sandbox/1/',
    'https://habr.com/ru/posts/1/',
])
def test_spoofed_habr_host_unsupported(tg, tmp_db, fake, url):
    _send(FRIEND, url)

    assert [(s[0], s[1][1]) for s in tg] == [('reply_to', bot_module.MSG_UNSUPPORTED)]
    assert fake.calls == []
    assert _views(tmp_db) == []


def test_non_habr_link_unchanged(tg, tmp_db, fake):
    url = 'https://github.com/owner/repo'
    _send(FRIEND, url)
    assert fake.calls == [url]
    assert tmp_db.get_article_by_url(url)['habr_id'] is None

    tg.clear()
    _send(FRIEND, url, update_id=3)
    assert _cache_buttons(tg, FRIEND)
    assert fake.calls == [url]
    assert _views(tmp_db) == []


def test_new_habr_article_fetched_via_canonical_url(tg, tmp_db, fake):
    tmp_db.create_idea('Idea', None, FRIEND)
    user_url = 'https://habr.com/ru/companies/x/articles/77/?utm_source=tg'

    _send(FRIEND, user_url)

    assert fake.calls == ['https://habr.com/ru/articles/77/']
    row = tmp_db.get_article_by_habr_id(77)
    assert row['url'] == user_url and row['user_id'] == FRIEND
    texts = _texts(tg, FRIEND)
    assert texts[0] == bot_module.MSG_PROCESSING
    assert texts[1].startswith('Готово!\n') and f'Источник: {user_url}\n' in texts[1]
    assert texts[1].endswith('NEW SUMMARY')
    assert texts[2] == bot_module.MSG_LINK_SELECT_IDEAS
    assert f'link_skip:{row["id"]}' in _buttons(_markups(tg, FRIEND)[2])
    assert bot_module.pending_article_links[FRIEND]['created_now'] is True
    # Status message is removed, not left behind
    assert [s[0] for s in tg].count('delete_message') == 1


def test_cache_show_resolves_by_habr_id(tg, tmp_db, fake):
    _save_habr(tmp_db, CANONICAL_123, 123, summary='SUMMARY OF 123')
    _send(FRIEND, 'https://habr.com/ru/articles/123/?utm_source=x')
    show = _cache_buttons(tg, FRIEND)[0]

    tg.clear()
    _press(FRIEND, show)

    assert _texts(tg, FRIEND) == ['SUMMARY OF 123']
    assert fake.calls == []


def test_regen_on_utm_variant_updates_habr_id_row(tg, tmp_db, fake):
    first = _save_habr(tmp_db, CANONICAL_123, 123, summary='OLD')
    _send(FRIEND, 'https://habr.com/ru/articles/123/?utm_source=x')
    regen = _cache_buttons(tg, FRIEND)[1]

    _press(FRIEND, regen)

    assert fake.calls == [CANONICAL_123]
    assert _article_count(tmp_db) == 1
    assert tmp_db.get_article_by_id(first)['summary'] == 'NEW SUMMARY'
    # Cache callbacks record no views beyond the link one
    assert _views(tmp_db) == [(FRIEND, 123, 'link')]


def test_cache_show_non_habr_uses_url_row(tg, tmp_db, fake):
    url = 'https://github.com/owner/repo'
    tmp_db.save_article({'url': url, 'source': 'github', 'title': 'T', 'content': 'C'}, 'GH SUMMARY', 'm', OWNER)
    _send(FRIEND, url)
    show = _cache_buttons(tg, FRIEND)[0]

    tg.clear()
    _press(FRIEND, show)

    assert _texts(tg, FRIEND) == ['GH SUMMARY']
    assert fake.calls == []


def test_regen_failure_keeps_row_and_shows_error(tg, tmp_db, fake):
    first = _save_habr(tmp_db, CANONICAL_123, 123, summary='OLD')
    _send(FRIEND, 'https://habr.com/ru/post/123/')
    regen = _cache_buttons(tg, FRIEND)[1]
    fake.result = False
    tg.clear()

    _press(FRIEND, regen)

    assert _texts(tg, FRIEND) == [bot_module.MSG_ERROR.format(error='Не удалось обработать статью')]
    assert tmp_db.get_article_by_id(first)['summary'] == 'OLD'
    assert _article_count(tmp_db) == 1


def test_command_with_url_not_captured_by_handle_url(tg, tmp_db, fake):
    _send(FRIEND, '/subscribe https://habr.com/ru/hubs/python/')

    shown = [s[1][1] for s in tg if s[0] in ('send_message', 'reply_to')]
    assert bot_module.MSG_UNSUPPORTED not in shown
    assert bot_module.MSG_PROCESSING not in shown
    assert bot_module.MSG_DUPLICATE_FOUND not in shown
    assert _views(tmp_db) == []
    assert fake.calls == []


def test_text_with_url_still_handled(tg, tmp_db, fake):
    _send(FRIEND, 'look https://habr.com/ru/articles/9/?utm=1')
    assert fake.calls == ['https://habr.com/ru/articles/9/']


# --- "Не привязывать" (Decision 20) --------------------------------------------


def test_link_skip_keeps_row_created_by_other_user(tg, tmp_db, fake):
    article_id = _save_habr(tmp_db, CANONICAL_123, 123, user_id=OWNER)
    tmp_db.create_idea('Idea', None, FRIEND)
    bot_module._summarize_for_user(FRIEND, FRIEND, 123, 'feed')
    assert bot_module.pending_article_links[FRIEND]['created_now'] is False

    _press(FRIEND, f'link_skip:{article_id}')

    assert tmp_db.get_article_by_id(article_id) is not None
    assert FRIEND not in bot_module.pending_article_links


def test_link_skip_deletes_own_newly_created_row(tg, tmp_db, fake):
    tmp_db.create_idea('Idea', None, FRIEND)
    _send(FRIEND, 'https://habr.com/ru/articles/55/')
    article_id = tmp_db.get_article_by_habr_id(55)['id']

    _press(FRIEND, f'link_skip:{article_id}')

    assert tmp_db.get_article_by_id(article_id) is None
    assert FRIEND not in bot_module.pending_article_links
    assert ('edit_message_text', (bot_module.MSG_LINK_SKIPPED,), {'chat_id': FRIEND, 'message_id': 11}) in tg


@pytest.mark.parametrize('case', ['stale_id', 'foreign_idea', 'bare', 'garbage_id'])
def test_link_skip_stale_id_or_foreign_idea_link_not_deleted(tg, tmp_db, fake, case):
    tmp_db.create_idea('Idea', None, FRIEND)
    _send(FRIEND, 'https://habr.com/ru/articles/56/')
    article_id = tmp_db.get_article_by_habr_id(56)['id']
    assert bot_module.pending_article_links[FRIEND]['created_now'] is True
    data = {
        'stale_id': f'link_skip:{article_id + 1}',
        'foreign_idea': f'link_skip:{article_id}',
        'bare': 'link_skip',
        'garbage_id': 'link_skip:' + '9' * 30,
    }[case]
    if case == 'foreign_idea':
        owner_idea = tmp_db.create_idea('Owner idea', None, OWNER)
        # The owner may link it: they have a view for the habr_id (Decision 20)
        habr_store.record_view(OWNER, 56, 'link')
        assert tmp_db.link_article_to_idea(article_id, owner_idea, OWNER) is True

    _press(FRIEND, data)

    assert tmp_db.get_article_by_id(article_id) is not None
    assert FRIEND not in bot_module.pending_article_links
    assert any(s[0] == 'edit_message_text' and s[1][0] == bot_module.MSG_LINK_SKIPPED for s in tg)


def test_link_skip_after_restart_deletes_nothing(tg, tmp_db):
    article_id = _save_habr(tmp_db, CANONICAL_123, 123, user_id=FRIEND)
    _press(FRIEND, f'link_skip:{article_id}')
    assert tmp_db.get_article_by_id(article_id) is not None


def test_toggle_rerender_keeps_article_id_on_skip(tg, tmp_db, fake):
    idea = tmp_db.create_idea('Idea', None, FRIEND)
    _send(FRIEND, 'https://habr.com/ru/articles/57/')
    article_id = tmp_db.get_article_by_habr_id(57)['id']
    tg.clear()

    _press(FRIEND, f'toggle_link:{idea}')

    edits = [s for s in tg if s[0] == 'edit_message_reply_markup']
    assert f'link_skip:{article_id}' in _buttons(edits[0][2]['reply_markup'])


# --- _summarize_for_user / _deliver_outcome ------------------------------------


def test_summarize_for_user_non_link_source_sends_cached_summary(tg, tmp_db, fake):
    article_id = _save_habr(tmp_db, CANONICAL_123, 123)
    tmp_db.create_idea('Idea', None, FRIEND)

    outcome = bot_module._summarize_for_user(FRIEND, FRIEND, 123, 'feed')

    texts = _texts(tg, FRIEND)
    assert bot_module.MSG_DUPLICATE_FOUND not in texts
    assert texts[-1] == bot_module.MSG_LINK_SELECT_IDEAS
    assert texts[0].endswith('CACHED SUMMARY')
    assert fake.calls == [] and fake.model_checks == []
    assert outcome == bot_module.SummaryOutcome(ok=True, messages=texts[:-1], error_text=None, article_id=article_id)
    assert _views(tmp_db) == []  # the core records no views


def test_summarize_for_user_empty_cached_summary_regenerates(tg, tmp_db, fake):
    article_id = _save_habr(tmp_db, CANONICAL_123, 123, summary='')

    outcome = bot_module._summarize_for_user(FRIEND, FRIEND, 123, 'digest')

    assert fake.calls == [CANONICAL_123]
    assert outcome.ok and outcome.article_id == article_id
    assert tmp_db.get_article_by_id(article_id)['summary'] == 'NEW SUMMARY'


@pytest.mark.parametrize('long_summary', [False, True])
def test_summarize_for_user_outcome_on_generation(tg, tmp_db, fake, long_summary):
    if long_summary:
        fake.summary = '\n\n'.join(['a' * 3000, 'b' * 3000, 'c' * 100])

    outcome = bot_module._summarize_for_user(FRIEND, FRIEND, 300, 'feed')

    row = tmp_db.get_article_by_habr_id(300)
    texts = _texts(tg, FRIEND)
    assert texts[0] == bot_module.MSG_PROCESSING
    assert outcome.ok is True and outcome.error_text is None
    assert outcome.article_id == row['id']
    assert outcome.messages == texts[1:]
    if long_summary:
        model, provider = bot_module.get_user_model(FRIEND)
        header, *chunks = outcome.messages
        assert header == f'Готово! Модель: {model} ({provider})'
        assert len(chunks) >= 2
        assert '\n\n'.join(chunks) == fake.summary
    else:
        assert len(outcome.messages) == 1 and outcome.messages[0].endswith('NEW SUMMARY')


@pytest.mark.parametrize('mode', ['none', 'raises', 'model_unavailable'])
def test_summarize_for_user_outcome_on_failure(tg, tmp_db, fake, monkeypatch, mode):
    if mode == 'none':
        fake.result = False
    elif mode == 'raises':
        fake.error = RuntimeError('boom https://secret')
    else:
        monkeypatch.setattr(bot_module, 'check_model_availability', lambda m, p: (False, 'down'))

    outcome = bot_module._summarize_for_user(FRIEND, FRIEND, 301, 'feed')

    assert outcome.ok is False and outcome.article_id is None and outcome.messages == []
    shown = [s[1][0] for s in tg if s[0] == 'edit_message_text'] + _texts(tg, FRIEND)
    assert outcome.error_text in shown
    if mode == 'none':
        assert outcome.error_text == bot_module.MSG_ERROR.format(error='Не удалось обработать статью')
    elif mode == 'raises':
        assert outcome.error_text == bot_module.MSG_ERROR.format(error='RuntimeError')
    else:
        model, provider = bot_module.get_user_model(FRIEND)
        assert outcome.error_text == bot_module.MSG_MODEL_UNAVAILABLE.format(
            model=model, provider=provider, error='down')
    assert tmp_db.get_article_by_habr_id(301) is None


def test_deliver_outcome_sends_same_messages_without_regeneration(tg, tmp_db, fake):
    outcome = bot_module._summarize_for_user(OWNER, OWNER, 400, 'feed')
    calls_before, checks_before = list(fake.calls), list(fake.model_checks)
    tmp_db.create_idea('Idea', None, FRIEND)
    tg.clear()

    bot_module._deliver_outcome(FRIEND, FRIEND, outcome)

    texts = _texts(tg, FRIEND)
    assert texts == outcome.messages + [bot_module.MSG_LINK_SELECT_IDEAS]
    assert fake.calls == calls_before and fake.model_checks == checks_before
    assert bot_module.pending_article_links[FRIEND] == {
        'article_id': outcome.article_id, 'selected_ideas': set(), 'created_now': False,
    }

    _press(FRIEND, f'link_skip:{outcome.article_id}')
    assert tmp_db.get_article_by_id(outcome.article_id) is not None


def test_deliver_outcome_link_cache_hit_sends_nothing(tg, tmp_db):
    tmp_db.create_idea('Idea', None, FRIEND)
    article_id = _save_habr(tmp_db, CANONICAL_123, 123)

    bot_module._deliver_outcome(FRIEND, FRIEND, bot_module.SummaryOutcome(ok=True, article_id=article_id))

    assert tg == []
    assert FRIEND not in bot_module.pending_article_links


def test_deliver_outcome_error_sends_error_text_only(tg, tmp_db):
    tmp_db.create_idea('Idea', None, FRIEND)
    outcome = bot_module.SummaryOutcome(ok=False, error_text='Ошибка: X', article_id=None)

    bot_module._deliver_outcome(FRIEND, FRIEND, outcome)

    assert tg == [('send_message', (FRIEND, 'Ошибка: X'), {})]


# --- pipeline -------------------------------------------------------------------


@pytest.fixture
def scraped(monkeypatch):
    calls: list[str] = []

    def get_article(url):
        calls.append(url)
        return {'url': url, 'source': 'habr', 'title': 'T', 'content': 'C'}
    monkeypatch.setattr(pipeline, 'get_article', get_article)
    monkeypatch.setattr(pipeline, 'generate_summary', lambda data, model, provider: 'S')
    return calls


@pytest.mark.parametrize('url, expected', [
    ('https://habr.com/ru/articles/1/', 'habr'),
    ('https://habr.com/ru/companies/x/articles/1/?utm=1', 'habr'),
    ('https://github.com/a/b', 'github'),
    ('https://infostart.ru/public/886103/', 'infostart'),
    ('http://169.254.169.254/?habr.com', 'unknown'),
    ('https://evilhabr.com/ru/articles/1/', 'unknown'),
    ('https://habr.com/ru/hubs/python/', 'unknown'),
    ('https://github.com/a/b?habr.com', 'unknown'),
    ('https://example.com/', 'unknown'),
])
def test_pipeline_routing_strict_habr(url, expected):
    assert pipeline.get_source_name(url) == expected
    assert pipeline.is_supported_url(url) is (expected != 'unknown')
    assert bot_module.is_supported_url(url) is (expected != 'unknown')


def test_pipeline_process_article_fetches_canonical(scraped):
    result = pipeline.process_article('https://habr.com/ru/companies/x/articles/88/?utm_source=x#comments')
    assert scraped == ['https://habr.com/ru/articles/88/']
    assert result == ('S', {'url': 'https://habr.com/ru/articles/88/', 'source': 'habr', 'title': 'T', 'content': 'C'})


def test_pipeline_process_article_non_habr_unchanged(scraped):
    pipeline.process_article('https://github.com/a/b')
    assert scraped == ['https://github.com/a/b']


@pytest.mark.parametrize('url', ['http://evil/?habr.com', 'https://habr.com/ru/sandbox/5/'])
def test_pipeline_process_article_rejects_spoofed_habr(scraped, url):
    assert pipeline.process_article(url) is None
    assert scraped == []


def test_pipeline_save_by_habr_id_updates_earliest_row(tmp_db):
    first = _save_habr(tmp_db, CANONICAL_123, 123, summary='A')
    second = _save_habr(tmp_db, 'https://habr.com/ru/post/123/', 123, summary='B')
    data = {'url': CANONICAL_123, 'source': 'habr', 'title': 'T', 'content': 'C'}

    got = pipeline.save_article_to_db(data, 'NEW', 'm2', FRIEND, 'https://habr.com/ru/articles/123/?utm_source=y')

    assert got == first
    assert _article_count(tmp_db) == 2
    assert tmp_db.get_article_by_id(first)['summary'] == 'NEW'
    assert tmp_db.get_article_by_id(first)['model_used'] == 'm2'
    assert tmp_db.get_article_by_id(second)['summary'] == 'B'


def test_pipeline_insert_stores_user_url_without_mutating_input(tmp_db):
    user_url = 'https://habr.com/ru/articles/124/?utm_source=y'
    data = {'url': 'https://habr.com/ru/articles/124/', 'source': 'habr', 'title': 'T', 'content': 'C'}

    article_id = pipeline.save_article_to_db(data, 'S', 'm', FRIEND, user_url)

    row = tmp_db.get_article_by_id(article_id)
    assert row['url'] == user_url and row['habr_id'] == 124 and row['user_id'] == FRIEND
    assert data['url'] == 'https://habr.com/ru/articles/124/'


def test_pipeline_save_non_habr_keeps_url_behavior(tmp_db):
    data = {'url': 'https://github.com/a/b', 'source': 'github', 'title': 'T', 'content': 'C'}
    first = pipeline.save_article_to_db(data, 'S1', 'm', FRIEND, 'https://github.com/a/b')
    again = pipeline.save_article_to_db(data, 'S2', 'm', FRIEND, 'https://github.com/a/b')
    assert first == again
    assert tmp_db.get_article_by_id(first)['summary'] == 'S2'
    assert tmp_db.get_article_by_id(first)['habr_id'] is None


def test_is_article_linked_to_other_users_idea(tmp_db):
    article_id = _save_habr(tmp_db, CANONICAL_123, 123, user_id=FRIEND)
    own = tmp_db.create_idea('Own', None, FRIEND)
    tmp_db.link_article_to_idea(article_id, own, FRIEND)
    assert tmp_db.is_article_linked_to_other_users_idea(article_id, FRIEND) is False
    assert tmp_db.is_article_linked_to_other_users_idea(article_id, OWNER) is True


# --- hubs/tags for linked articles (Task 14) -----------------------------------


class FakeKek:
    """Stand-in for habr_api.fetch_article: records calls and how many bot calls preceded each."""

    def __init__(self, sent, error=None):
        self.sent = sent
        self.error = error
        self.calls: list[tuple[int, object, int]] = []

    def __call__(self, habr_id, *, wait_timeout=None):
        self.calls.append((habr_id, wait_timeout, len(self.sent)))
        if self.error is not None:
            raise self.error
        return {
            'habr_id': habr_id, 'title': 'T', 'url': habr_api.canonical_url(habr_id),
            'published_at': datetime(2026, 1, 1, tzinfo=timezone.utc), 'score': 1, 'comments': 0,
            'hubs': [('python', 'collective'), ('bitrix', 'corporative')], 'tags': ['tag1'],
            'lead_text': '', 'post_type': 'article',
        }


@pytest.fixture
def kek(tg, monkeypatch):
    fake_kek = FakeKek(tg)
    monkeypatch.setattr(habr_api, 'fetch_article', fake_kek)
    return fake_kek


def test_link_flow_stores_hubs_tags_for_new_article(tg, tmp_db, fake, kek):
    _send(FRIEND, 'https://habr.com/ru/companies/x/articles/77/?utm_source=tg')

    assert [(c[0], c[1]) for c in kek.calls] == [(77, habr_api.HANDLER_WAIT_TIMEOUT)]
    meta = habr_store.get_habr_article(77)
    assert meta['fetched_at'] is not None
    assert meta['hubs'] == [{'alias': 'bitrix', 'hub_type': 'corporative'},
                            {'alias': 'python', 'hub_type': 'collective'}]
    assert meta['tags'] == ['tag1']


def test_link_flow_summary_sent_before_kek_fetch(tg, tmp_db, fake, kek):
    tmp_db.create_idea('Idea', None, FRIEND)
    kek.error = RuntimeError('kek exploded')

    _send(FRIEND, 'https://habr.com/ru/articles/77/')

    texts = _texts(tg, FRIEND)
    assert texts[-2].startswith('Готово!') and texts[-1] == bot_module.MSG_LINK_SELECT_IDEAS
    # The kek call starts only after every bot message of the flow was sent
    ((_, _, sent_before),) = kek.calls
    assert sent_before == len(tg)
    assert habr_store.get_habr_article(77) is None


@pytest.mark.parametrize('error', [
    habr_api.HabrUnavailable('http', 503),
    habr_api.HabrBusy('busy'),
    habr_api.HabrNotFound('NOT_FOUND'),
    ValueError('boom'),
], ids=['unavailable', 'busy', 'not_found', 'generic'])
def test_link_flow_kek_errors_logged_not_shown(tg, tmp_db, fake, kek, caplog, error):
    tmp_db.create_idea('Idea', None, FRIEND)
    kek.error = error

    with caplog.at_level(logging.WARNING, logger='bot'):
        _send(FRIEND, 'https://habr.com/ru/articles/77/')

    texts = _texts(tg, FRIEND)
    assert texts == [bot_module.MSG_PROCESSING, texts[1], bot_module.MSG_LINK_SELECT_IDEAS]
    assert texts[1].startswith('Готово!')
    assert [s[0] for s in tg if s[0] in ('reply_to', 'edit_message_text')] == []
    assert any(r.name == 'bot' and r.levelno >= logging.WARNING and '77' in r.getMessage()
               for r in caplog.records)
    if isinstance(error, habr_api.HabrNotFound):
        assert habr_store.get_habr_article(77)['unavailable_at'] is not None


@pytest.mark.parametrize('state', ['fetched', 'unavailable'])
def test_link_flow_skips_fetch_when_already_fetched(tg, tmp_db, fake, kek, state):
    if state == 'fetched':
        habr_store.upsert_habr_article(kek(77))
        kek.calls.clear()
    else:
        habr_store.mark_unavailable(77)

    _send(FRIEND, 'https://habr.com/ru/articles/77/')

    assert any(t.startswith('Готово!') for t in _texts(tg, FRIEND))
    assert kek.calls == []
