"""Tests for digest.py build/render part (Task 9): lists, descriptions, rendering, dry_run.

No real network (conftest guard), no real LLM: describe functions are fakes.
"""

import copy
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

import database
import digest
import feed
import habr_api
import habr_store

UTC = timezone.utc
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)     # 18:00 MSK on Nov 4
DAY = date(2026, 11, 3)                             # digest day = yesterday (MSK)
IN_DAY = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
USER = 501
OTHER = 502
OWNER = int(os.environ['OWNER_ID'])
FOOTER = 'Настроить: /subscribe, выключить: /digest off'
LEAD = '<p>Короткий анонс статьи про Python и всё такое.</p>'


def item(habr_id, score=0, comments=0, published=IN_DAY, hubs_=(('python', 'collective'),),
         title=None, lead=LEAD):
    return {
        'habr_id': habr_id, 'title': title or f'Статья {habr_id}', 'url': habr_api.canonical_url(habr_id),
        'published_at': published, 'score': score, 'comments': comments, 'hubs': list(hubs_),
        'tags': ['t'], 'lead_text': lead, 'post_type': 'article',
    }


def build(user_id, items, describe=None, day=DAY, store=True):
    if store:
        habr_store.upsert_habr_articles(items)
    return digest.build_user_digest(user_id, items, day, describe=describe)


def persisted(list_id):
    return [i['habr_id'] for i in habr_store.get_ranked_list(list_id)['items']]


def lists_by_kind(result):
    out = {}
    for list_id in result['list_ids']:
        header = habr_store.get_ranked_list(list_id, limit=0)
        out[header['kind']] = list_id
    return out


def all_callbacks(result):
    return [cb for _, rows in result['parts'] for row in (rows or []) for _, cb in row]


class Counter:
    def __init__(self, result='Описание: о чём статья и зачем её читать.', error=None):
        self.calls = []
        self.result = result
        self.error = error

    def __call__(self, title, lead_text):
        self.calls.append((title, lead_text))
        if self.error is not None:
            raise self.error
        return self.result


# =============================================================================
# Day window and list building
# =============================================================================


def test_day_window_msk_boundaries(tmp_db):
    day = digest.digest_day(NOW)
    assert day == DAY
    # Run day D = Nov 4: digest day window is [Nov 2 21:00Z, Nov 3 21:00Z)
    items = [
        item(1, published=datetime(2026, 11, 2, 20, 59, 59, tzinfo=UTC)),
        item(2, published=datetime(2026, 11, 2, 21, 0, 0, tzinfo=UTC)),
        item(3, published=datetime(2026, 11, 3, 20, 59, 59, tzinfo=UTC)),
        item(4, published=datetime(2026, 11, 3, 21, 0, 0, tzinfo=UTC)),
    ]
    result = build(USER, items, day=day)
    assert sorted(persisted(lists_by_kind(result)['digest_site'])) == [2, 3]
    start, end = digest.day_bounds(day)
    assert start == datetime(2026, 11, 2, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 3, 21, 0, tzinfo=UTC)


def test_digest_day_just_after_msk_midnight():
    # 21:00Z on Nov 3 is 00:00 MSK on Nov 4 -> yesterday is Nov 3
    assert digest.digest_day(datetime(2026, 11, 3, 21, 0, tzinfo=UTC)) == DAY
    assert digest.digest_day(datetime(2026, 11, 3, 20, 59, tzinfo=UTC)) == date(2026, 11, 2)


def test_site_list_order_and_seen_exclusion(tmp_db):
    items = [
        item(10, score=5, comments=1),
        item(11, score=9, comments=0),
        item(12, score=5, comments=3),
        item(13, score=5, comments=1, published=IN_DAY + timedelta(hours=1)),
        item(14, score=50),
        item(15, score=40),
    ]
    utm_id = habr_api.parse_article_id('https://habr.com/ru/articles/14/?utm_source=tg&utm_medium=x')
    assert utm_id == 14
    habr_store.record_view(USER, utm_id, 'link')
    habr_store.record_view(USER, 15, 'feed')

    mine = build(USER, items)
    assert persisted(lists_by_kind(mine)['digest_site']) == [11, 12, 13, 10]
    # The other user has seen nothing: full list
    theirs = build(OTHER, items)
    assert persisted(lists_by_kind(theirs)['digest_site']) == [14, 15, 11, 12, 13, 10]


def test_hubs_list_excludes_site_top5_and_dedups(tmp_db):
    habr_store.add_subscription(USER, 'python')
    habr_store.add_subscription(USER, 'devops')
    both = (('python', 'collective'), ('devops', 'collective'))
    # Site top-5 = 1..5; item 1 is also in subscribed hubs but must not repeat in the hubs part
    items = [item(1, score=99, hubs_=both)]
    items += [item(i, score=100 - i, hubs_=(('other', 'collective'),)) for i in range(2, 6)]
    # Item 20 is in two subscribed hubs: appears once
    items += [item(20, score=10, hubs_=both), item(21, score=12, hubs_=(('devops', 'collective'),)),
              item(22, score=11, hubs_=(('other', 'collective'),))]
    result = build(USER, items)
    kinds = lists_by_kind(result)
    assert persisted(kinds['digest_site'])[:5] == [1, 2, 3, 4, 5]
    assert persisted(kinds['digest_hubs']) == [21, 20]
    assert len(result['parts']) == 2
    assert result['parts'][1][0].startswith('Твои хабы')


def test_corporative_hub_never_matches_subscription(tmp_db):
    habr_store.add_subscription(USER, 'bitrix')
    items = [item(i, score=100) for i in range(1, 6)]
    items += [item(30, score=5, hubs_=(('bitrix', 'corporative'),)),
              item(31, score=4, hubs_=(('bitrix', 'collective'),))]
    result = build(USER, items)
    assert persisted(lists_by_kind(result)['digest_hubs']) == [31]


def test_no_subscriptions_only_site_part(tmp_db):
    result = build(USER, [item(i, score=i) for i in range(1, 4)])
    assert len(result['parts']) == 1
    text, rows = result['parts'][0]
    assert text.startswith('Лучшее на Хабре')
    assert text.endswith(FOOTER)
    assert set(lists_by_kind(result)) == {'digest_site'}


def test_hubs_part_hidden_when_nothing_matches(tmp_db):
    habr_store.add_subscription(USER, 'golang')
    result = build(USER, [item(i, score=i) for i in range(1, 9)])
    assert len(result['parts']) == 1
    assert 'Твои хабы' not in result['parts'][0][0]
    assert set(lists_by_kind(result)) == {'digest_site'}


def test_both_parts_empty_text(tmp_db):
    expected = [('За вчера ничего нового\n\n' + FOOTER, None)]
    assert digest.render_empty_day(DAY) == expected

    empty = build(USER, [])
    assert empty == {'parts': expected, 'list_ids': []}

    habr_store.add_subscription(USER, 'python')
    items = [item(1), item(2)]
    for i in items:
        habr_store.record_view(USER, i['habr_id'], 'digest')
    all_seen = build(USER, items)
    assert all_seen['parts'] == expected
    assert all_seen['list_ids'] == []


# =============================================================================
# Keyboards and callbacks
# =============================================================================


def test_fewer_than_five_articles(tmp_db):
    result = build(USER, [item(i, score=i) for i in range(1, 4)])
    text, rows = result['parts'][0]
    assert [line.split('.')[0] for line in text.splitlines() if line[:1].isdigit()] == ['1', '2', '3']
    labels = [t for row in rows for t, _ in row]
    assert labels == ['Саммари 1', 'Саммари 2', 'Саммари 3']
    assert feed.TEXT_MORE_BUTTON not in labels


def test_exactly_five_articles_no_more_button(tmp_db):
    result = build(USER, [item(i, score=i) for i in range(1, 6)])
    labels = [t for row in result['parts'][0][1] for t, _ in row]
    assert labels == [f'Саммари {n}' for n in range(1, 6)]


def test_more_than_five_has_more_button(tmp_db):
    result = build(USER, [item(i, score=i) for i in range(1, 8)], )
    list_id = lists_by_kind(result)['digest_site']
    rows = result['parts'][0][1]
    assert rows[-1] == [(feed.TEXT_MORE_BUTTON, f'hl:m:{list_id}:0')]
    decoded = feed.decode_list_callback(rows[-1][0][1], USER)
    assert decoded is not None and decoded.action == 'm' and decoded.value == 0
    assert decoded.list_id == list_id and decoded.kind == 'digest_site'


def test_footer_only_on_last_message(tmp_db):
    habr_store.add_subscription(USER, 'python')
    items = [item(i, score=100 - i, hubs_=(('other', 'collective'),)) for i in range(1, 6)]
    items += [item(40, score=1), item(41, score=2)]
    result = build(USER, items)
    texts = [t for t, _ in result['parts']]
    assert len(texts) == 2
    assert FOOTER not in texts[0]
    assert texts[1].endswith(FOOTER)
    assert texts[1].count(FOOTER) == 1


def test_summary_buttons_one_to_five(tmp_db):
    habr_store.add_subscription(USER, 'python')
    items = [item(i, score=100 - i, hubs_=(('other', 'collective'),)) for i in range(1, 9)]
    items += [item(40 + i, score=i) for i in range(1, 8)]
    result = build(USER, items)
    kinds = lists_by_kind(result)
    for (text, rows), kind in zip(result['parts'], ('digest_site', 'digest_hubs')):
        list_id = kinds[kind]
        summaries = [(t, cb) for row in rows for t, cb in row if t.startswith('Саммари')]
        assert summaries == [(f'Саммари {n}', f'hl:s:{list_id}:{n}') for n in range(1, 6)]
        for _, cb in summaries:
            decoded = feed.resolve_summary_click(cb, USER)
            assert decoded is not None
            assert decoded.habr_id == persisted(list_id)[decoded.value - 1]
    for cb in all_callbacks(result):
        assert len(cb.encode('utf-8')) <= 64


def test_lists_persisted_full_not_only_top5(tmp_db):
    habr_store.add_subscription(USER, 'python')
    items = [item(i, score=100 - i, hubs_=(('other', 'collective'),)) for i in range(1, 13)]
    items += [item(40 + i, score=i) for i in range(1, 10)]
    result = build(USER, items)
    kinds = lists_by_kind(result)
    site = habr_store.get_ranked_list(kinds['digest_site'])
    hubs_list = habr_store.get_ranked_list(kinds['digest_hubs'])
    assert site['total'] == 21
    assert hubs_list['total'] == 9
    for header in (site, hubs_list):
        assert header['user_id'] == USER
        assert header['list_date'] == '2026-11-03'
    assert result['list_ids'] == [kinds['digest_site'], kinds['digest_hubs']]


def test_keyboards_are_data_rows(tmp_db):
    result = build(USER, [item(i, score=i) for i in range(1, 9)])
    for text, rows in result['parts']:
        assert isinstance(text, str)
        assert isinstance(rows, list)
        for row in rows:
            assert isinstance(row, list)
            for button in row:
                assert type(button) is tuple and len(button) == 2
                assert all(type(x) is str for x in button)


# =============================================================================
# Descriptions
# =============================================================================


def test_describe_called_once_for_shared_article(tmp_db):
    describe = Counter()
    items = [item(1, score=10, title='Общая &amp; статья', lead='<p>raw &amp; lead</p>')]
    first = build(USER, items, describe=describe)
    second = build(OTHER, items, describe=describe)
    # Raw values passed through unchanged: title as normalized, lead as raw HTML
    assert describe.calls == [('Общая &amp; статья', '<p>raw &amp; lead</p>')]
    assert habr_store.get_habr_article(1)['short_description'] == describe.result
    assert describe.result in first['parts'][0][0]
    assert describe.result in second['parts'][0][0]


def test_describe_none_uses_fallback_and_stores_nothing(tmp_db):
    describe = Counter(result=None)
    items = [item(1, score=10)]
    result = build(USER, items, describe=describe)
    assert 'Короткий анонс статьи про Python' in result['parts'][0][0]
    assert habr_store.get_habr_article(1)['short_description'] is None
    build(OTHER, items, describe=describe)
    assert len(describe.calls) == 2


def test_describe_exception_uses_fallback(tmp_db):
    describe = Counter(error=RuntimeError('llm down'))
    result = build(USER, [item(1, score=10), item(2, score=5, lead='')], describe=describe)
    text = result['parts'][0][0]
    assert 'Короткий анонс статьи про Python' in text
    assert habr_store.get_habr_article(1)['short_description'] is None
    # Empty lead + failing describe: title block without a description line
    block = text.split('\n\n')[2]
    assert block.splitlines() == [feed.render_line(2, {'title': 'Статья 2', 'score': 5, 'comments': 0})]


def test_describe_blank_or_non_string_result_is_rejected(tmp_db):
    for bad in ('   ', 42):
        describe = Counter(result=bad)
        result = build(USER, [item(7, score=1)], describe=describe)
        assert 'Короткий анонс' in result['parts'][0][0]
        assert habr_store.get_habr_article(7)['short_description'] is None


def test_cached_description_reused_without_describe(tmp_db):
    habr_store.upsert_habr_articles([item(1, score=10)])
    habr_store.set_short_description(1, 'Сохранённое описание.')
    describe = Counter()
    result = build(USER, [item(1, score=10)], describe=describe)
    assert describe.calls == []
    assert 'Сохранённое описание.' in result['parts'][0][0]
    # describe=None also shows the stored description (cache is shared by all users)
    assert 'Сохранённое описание.' in build(OTHER, [item(1, score=10)])['parts'][0][0]


def test_describe_only_for_top5(tmp_db):
    describe = Counter()
    build(USER, [item(i, score=i) for i in range(1, 9)], describe=describe)
    assert sorted(t for t, _ in describe.calls) == sorted(f'Статья {i}' for i in range(4, 9))


def test_no_describe_function_no_llm(tmp_db, monkeypatch):
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', Counter(error=AssertionError('must not be called')))
    result = build(USER, [item(1, score=10)], describe=None)
    assert 'Короткий анонс статьи про Python' in result['parts'][0][0]
    assert habr_store.get_habr_article(1)['short_description'] is None
    code = ('import sys, digest; '
            'bad = [m for m in ("summarizer", "bot", "telebot") if m in sys.modules]; '
            'assert not bad, bad')
    proc = subprocess.run([sys.executable, '-c', code], cwd=os.path.dirname(os.path.dirname(__file__)),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_default_resolved_at_call_time(tmp_db, monkeypatch, fake_list_router, capsys):
    fake = Counter()
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', fake)
    digest.build_user_digest(USER, [item(1, score=10)], DAY)
    assert len(fake.calls) == 1
    habr_store.upsert_habr_articles([item(2, score=10)])
    digest.build_user_digest(USER, [item(2, score=10)], DAY, describe=None)
    assert len(fake.calls) == 1

    fake_list_router(day_count=3)
    digest.dry_run(now=NOW)
    assert len(fake.calls) == 4                     # 3 site items of the dry-run day
    digest.dry_run(describe=None, now=NOW)
    assert len(fake.calls) == 4


def test_fallback_cleanup_and_word_boundary(tmp_db):
    lead = ('<p>Начало‮ текста\x07 <b>жирно</b> https://evil.example/x?a=1 пиши @admin и жми /start '
            + 'слово ' * 80 + '</p>')
    result = build(USER, [item(1, score=1, lead=lead)])
    desc = result['parts'][0][0].split('\n\n')[1].splitlines()[1]
    assert desc == feed.fallback_description(lead, limit=250)
    for bad in ('‮', '\x07', '<b>', 'http', '@admin', '/start', 'evil'):
        assert bad not in desc
    assert desc.startswith('Начало текста жирно')
    assert len(desc) <= 250
    assert desc.endswith('слово…')


def test_titles_sanitized_in_rendered_text(tmp_db):
    title = 'Tom &amp; Jerry ‮реверс​\x00 ' + 'длинно ' * 60
    result = build(USER, [item(1, score=1, title=title)])
    line = result['parts'][0][0].split('\n\n')[1].splitlines()[0]
    shown = line[len('1. '):line.index(' — рейтинг')]
    assert shown == feed.sanitize_title(title)
    assert shown.startswith('Tom & Jerry реверс')
    assert len(shown) <= 200
    for bad in ('‮', '​', '\x00', '&amp;'):
        assert bad not in line


def test_render_is_plain_text(tmp_db):
    habr_store.add_subscription(USER, 'python')
    items = [item(i, score=100 - i, hubs_=(('other', 'collective'),)) for i in range(1, 6)]
    items += [item(9, score=1, title='<b>Жирный</b> & *звёзды*')]
    result = build(USER, items)
    for part in result['parts']:
        assert len(part) == 2                # (text, rows): nothing like parse_mode
    # Own template texts carry no HTML/Markdown-significant characters
    templates = [digest.TEXT_HEADER_SITE, digest.TEXT_HEADER_HUBS, digest.TEXT_FOOTER, digest.TEXT_EMPTY_DAY]
    assert not any(ch in t for t in templates for ch in '<>&*_`[]')
    # A Habr title is shown literally: neither stripped as markup nor escaped
    assert '<b>Жирный</b> & *звёзды*' in result['parts'][1][0]
    assert '&amp;' not in result['parts'][1][0]


def test_no_db_writes_inside_llm_call(tmp_db):
    habr_store.add_subscription(USER, 'python')
    writes = []

    def describe(title, lead_text):
        # Another connection must be able to take the write lock right now
        conn = sqlite3.connect(database.DB_PATH, timeout=0.2)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO hub_subscriptions (user_id, hub_alias, created_at) VALUES (?, ?, ?)',
                         (9000 + len(writes), 'x', 'now'))
            conn.commit()
        finally:
            conn.close()
        writes.append(title)
        return 'Описание.'

    items = [item(i, score=100 - i, hubs_=(('other', 'collective'),)) for i in range(1, 7)]
    items += [item(20, score=1)]
    build(USER, items, describe=describe)
    assert len(writes) == 6


# =============================================================================
# Paging
# =============================================================================


def test_paging_page_and_end_text(tmp_db):
    result = build(USER, [item(i, score=100 - i) for i in range(1, 18)])   # 17 items
    list_id = lists_by_kind(result)['digest_site']

    text, rows = digest.render_page(list_id, 0)
    lines = text.splitlines()
    assert [line.split('.')[0] for line in lines] == [str(n) for n in range(6, 16)]
    assert rows[:2] == feed.number_rows(list_id, range(6, 16))
    assert [len(r) for r in rows[:2]] == [5, 5]
    assert rows[2] == [(feed.TEXT_MORE_BUTTON, f'hl:m:{list_id}:10')]
    assert 'Это все' not in text

    text, rows = digest.render_page(list_id, 10)
    assert [line.split('.')[0] for line in text.splitlines()[:2]] == ['16', '17']
    assert text.endswith('Это все статьи за 3 ноября')
    assert all(t != feed.TEXT_MORE_BUTTON for row in rows for t, _ in row)

    assert digest.render_page(999999, 0) is None


@pytest.mark.parametrize('day, expected', [(date(2026, 3, 1), '1 марта'), (date(2026, 11, 2), '2 ноября')])
def test_paging_end_text_month_genitive(tmp_db, day, expected):
    published = datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)
    result = build(USER, [item(i, score=i, published=published) for i in range(1, 8)], day=day)
    text, _ = digest.render_page(result['list_ids'][0], 0)
    assert text.endswith(f'Это все статьи за {expected}')


# =============================================================================
# dry_run
# =============================================================================


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.headers = {}
        self._body = json.dumps(body).encode('utf-8') if not isinstance(body, bytes) else body

    def iter_content(self, chunk_size=1):
        yield self._body

    def close(self):
        pass


@pytest.fixture
def fake_list_router(monkeypatch, fixture_text):
    """Installs a fake requests.get serving one list page built from the trimmed real fixture."""
    page = json.loads(fixture_text('habr_list_page.json'))
    template = page['publicationRefs'][page['publicationIds'][0]]
    state = {'log': []}

    def install(day_count=12, hubs_=('python',), status=200):
        refs = []
        for n in range(day_count):
            ref = copy.deepcopy(template)
            ref['id'] = str(800000 + n)
            ref['titleHtml'] = f'Статья дня {n}'
            ref['timePublished'] = (datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
                                    - timedelta(minutes=10 * n)).isoformat()
            ref['statistics']['score'] = 100 - n
            ref['hubs'] = [{'alias': 'python' if n % 2 else 'other', 'type': 'collective'}]
            if hubs_ != ('python',):
                ref['hubs'] = [{'alias': a, 'type': 'collective'} for a in hubs_]
            refs.append(ref)
        old = copy.deepcopy(template)
        old['id'] = '700000'
        old['timePublished'] = '2026-11-02T10:00:00+00:00'
        refs.append(old)
        body = {'pagesCount': 1, 'publicationIds': [r['id'] for r in refs],
                'publicationRefs': {r['id']: r for r in refs}}

        def fake_get(url, params=None, **kw):
            state['log'].append((url, dict(params or {})))
            return _Resp(status, body if status == 200 else {'error': 'x'})

        monkeypatch.setattr(habr_api.requests, 'get', fake_get)
        return state['log']
    return install


def _count(table, path=None):
    conn = sqlite3.connect(path or database.DB_PATH)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    finally:
        conn.close()


def test_dry_run_uses_temp_db(tmp_db, fake_list_router, capsys):
    fake_list_router()
    original = database.DB_PATH
    original_dir = database.DATA_DIR
    habr_store.add_subscription(OWNER, 'other')        # proves the temp DB is a copy

    result = digest.dry_run(describe=None, now=NOW)

    assert database.DB_PATH == original and database.DATA_DIR == original_dir
    for table in ('habr_articles', 'ranked_lists', 'ranked_list_items', 'digest_runs', 'digest_deliveries'):
        assert _count(table) == 0, table
    assert habr_store.list_subscriptions(OWNER) == ['other']
    assert len(result['parts']) == 2                   # hubs part from the copied subscription
    assert 'Твои хабы' in capsys.readouterr().out


def test_dry_run_without_db_uses_fresh_temp_db(tmp_path, monkeypatch, fake_list_router, capsys):
    missing_dir = tmp_path / 'nodata'
    monkeypatch.setattr(database, 'DATA_DIR', str(missing_dir))
    monkeypatch.setattr(database, 'DB_PATH', str(missing_dir / 'study_agent.db'))
    fake_list_router()

    result = digest.dry_run(describe=None, now=NOW)

    assert not missing_dir.exists()
    assert database.DB_PATH == str(missing_dir / 'study_agent.db')
    assert result['parts']


def test_dry_run_restores_db_path_on_error(tmp_db, monkeypatch, fake_list_router):
    fake_list_router()
    original = database.DB_PATH

    def boom(*a, **kw):
        raise RuntimeError('build failed')

    monkeypatch.setattr(digest, 'build_user_digest', boom)
    with pytest.raises(RuntimeError):
        digest.dry_run(describe=None, now=NOW)
    assert database.DB_PATH == original


def test_dry_run_seeds_owner_subscriptions(tmp_db, monkeypatch, fake_list_router, capsys):
    fake_list_router()
    added = []
    real_add = habr_store.add_subscription
    monkeypatch.setattr(habr_store, 'add_subscription', lambda u, a: (added.append((u, a)), real_add(u, a))[1])

    result = digest.dry_run(describe=None, now=NOW)

    assert added == [(OWNER, 'python'), (OWNER, 'programming'), (OWNER, 'machine_learning')]
    assert len(result['parts']) == 2
    assert 'Твои хабы' in capsys.readouterr().out
    assert habr_store.list_subscriptions(OWNER) == []      # the real DB is untouched


def test_dry_run_keeps_existing_owner_subscriptions(tmp_db, monkeypatch, fake_list_router, capsys):
    fake_list_router(hubs_=('devops',))
    habr_store.add_subscription(OWNER, 'devops')
    added = []
    monkeypatch.setattr(habr_store, 'add_subscription', lambda u, a: added.append((u, a)))

    result = digest.dry_run(describe=None, now=NOW)

    assert added == []
    assert len(result['parts']) == 2
    assert habr_store.list_subscriptions(OWNER) == ['devops']


def test_dry_run_prints_parts_without_llm_or_delivery_rows(tmp_db, monkeypatch, fake_list_router, capsys):
    log = fake_list_router(day_count=12)
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', Counter(error=AssertionError('LLM called')))

    def forbidden(*a, **kw):
        raise AssertionError('delivery state touched')

    for name in ('create_run', 'update_run', 'snapshot_recipients', 'claim_delivery', 'set_delivery_status'):
        monkeypatch.setattr(habr_store, name, forbidden)

    result = digest.dry_run(describe=None, now=NOW)

    out = capsys.readouterr().out
    assert 'Лучшее на Хабре' in out and 'Твои хабы' in out
    assert '6. Статья дня' in out                  # first paging page of the site list
    assert FOOTER in out
    assert [p for _, p in log] and all(u.startswith(habr_api.API_BASE + 'articles/') for u, _ in log)
    assert _count('digest_runs') == 0 and _count('digest_deliveries') == 0
    assert len(result['parts']) == 2


def test_dry_run_empty_day(tmp_db, fake_list_router, capsys):
    fake_list_router(day_count=0)
    result = digest.dry_run(describe=None, now=NOW)
    out = capsys.readouterr().out
    assert 'За вчера ничего нового' in out and FOOTER in out
    assert result['list_ids'] == []


def test_dry_run_habr_unavailable_prints_category(tmp_db, fake_list_router, capsys):
    fake_list_router(status=503)
    assert digest.dry_run(describe=None, now=NOW) is None
    captured = capsys.readouterr()
    assert 'Хабр недоступен' in captured.out and 'HTTP 503' in captured.out
    assert 'Traceback' not in captured.out + captured.err


def test_fetch_day_articles_filters_window_and_upserts(tmp_db, fake_list_router):
    log = fake_list_router(day_count=3)
    items = digest.fetch_day_articles(DAY)
    assert sorted(i['habr_id'] for i in items) == [800000, 800001, 800002]
    assert set(habr_store.get_habr_articles([800000, 800001, 800002, 700000])) == {800000, 800001, 800002}
    assert log[0][1]['page'] == 1
