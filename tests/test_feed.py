import logging
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import feed
import habr_api
import habr_store
import hubs
from conftest import SignallingLock

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 3)          # digest day D (MSK)


@pytest.fixture(autouse=True)
def _reset_modules():
    feed._reset_for_tests()
    hubs._reset_for_tests()
    yield
    feed._reset_for_tests()
    hubs._reset_for_tests()


def item(habr_id, published=NOW, score=0, comments=0, hubs_=(('python', 'collective'),), title=None,
         tags=('t',), lead='<p>lead</p>'):
    return {
        'habr_id': habr_id, 'title': title or f'Article {habr_id}', 'url': habr_api.canonical_url(habr_id),
        'published_at': published, 'score': score, 'comments': comments, 'hubs': list(hubs_),
        'tags': list(tags), 'lead_text': lead, 'post_type': 'article',
    }


def ids(items):
    return [i['habr_id'] for i in items]


def utc(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


# =============================================================================
# Ranking
# =============================================================================


def test_feed_order_score_then_published():
    items = [
        item(1, NOW - timedelta(hours=5), score=10),
        item(2, NOW - timedelta(hours=1), score=10),
        item(3, NOW - timedelta(hours=2), score=30),
        item(4, NOW - timedelta(hours=3), score=-2),
    ]
    assert ids(feed.select_feed(items, {'python'}, set(), NOW)) == [3, 2, 1, 4]


def test_feed_only_subscribed_collective_hubs():
    items = [
        item(1, hubs_=[('bitrix', 'corporative')]),
        item(2, hubs_=[('bitrix', 'collective')]),
        item(3, hubs_=[('rust', 'collective')]),
        item(4, hubs_=[('megacorp', 'corporative'), ('Bitrix', 'collective')]),
    ]
    assert sorted(ids(feed.select_feed(items, ['BITRIX'], set(), NOW))) == [2, 4]
    assert feed.select_feed(items, [], set(), NOW) == []


def test_feed_excludes_seen(tmp_db):
    for habr_id, source in ((1, 'link'), (2, 'digest'), (3, 'feed'), (4, 'legacy')):
        habr_store.record_view(7, habr_id, source)
    items = [item(i) for i in range(1, 6)]
    seen = habr_store.get_viewed_ids(7)
    assert ids(feed.select_feed(items, {'python'}, seen, NOW)) == [5]


def test_feed_window_edges_exactly_24h(monkeypatch):
    monkeypatch.setattr(feed, '_now', lambda: NOW)
    items = [item(1, NOW - timedelta(hours=24)), item(2, NOW - timedelta(hours=24, seconds=1))]
    assert ids(feed.select_feed(items, {'python'}, set(), feed._now())) == [1]


def test_feed_skips_items_without_aware_published():
    bad = item(1)
    bad['published_at'] = None
    naive = item(2, published=datetime(2026, 11, 4, 11, 0))
    assert ids(feed.select_feed([bad, naive, item(3)], {'python'}, set(), NOW)) == [3]


def test_digest_day_boundaries_msk():
    start, end = feed.msk_day_bounds(DAY)
    assert start == utc('2026-11-02T21:00:00Z') and end == utc('2026-11-03T21:00:00Z')
    items = [
        item(1, utc('2026-11-02T20:59:59Z')),
        item(2, utc('2026-11-02T21:00:00Z')),
        item(3, utc('2026-11-03T20:59:59Z')),
        item(4, utc('2026-11-03T21:00:00Z')),
    ]
    assert sorted(ids(feed.select_digest_site(items, set(), DAY))) == [2, 3]
    assert sorted(ids(feed.select_digest_hubs(items, {'python'}, set(), DAY, []))) == [2, 3]


def test_digest_site_order_score_comments_published():
    t = utc('2026-11-03T10:00:00Z')
    items = [
        item(1, t, score=5, comments=1),
        item(2, t + timedelta(hours=1), score=5, comments=1),   # fresher wins the last tie
        item(3, t, score=5, comments=9),                         # more comments
        item(4, t, score=50, comments=0),                        # top score
        item(5, t, score=-1, comments=100),
    ]
    assert ids(feed.select_digest_site(items, set(), DAY)) == [4, 3, 2, 1, 5]


def test_digest_site_ignores_hubs_and_excludes_seen():
    t = utc('2026-11-03T10:00:00Z')
    items = [item(1, t, hubs_=[('megacorp', 'corporative')]), item(2, t, hubs_=[]), item(3, t)]
    assert sorted(ids(feed.select_digest_site(items, {3}, DAY))) == [1, 2]


def test_digest_hubs_excludes_site_top5_and_dedups():
    t = utc('2026-11-03T10:00:00Z')
    items = [item(i, t, score=100 - i, hubs_=[('other', 'collective')]) for i in range(1, 5)]
    items += [
        item(5, t, score=95, hubs_=[('python', 'collective')]),        # in the site top-5
        item(6, t, score=10, hubs_=[('python', 'collective'), ('rust', 'collective')]),
        item(7, t, score=9, hubs_=[('rust', 'collective')]),
        item(8, t, score=8, hubs_=[('python', 'corporative')]),
        item(6, t, score=10, hubs_=[('python', 'collective')]),        # duplicate id in the input
    ]
    site = feed.select_digest_site(items, set(), DAY)
    assert ids(site)[:5] == [1, 2, 3, 4, 5]
    hub_list = feed.select_digest_hubs(items, {'python', 'rust'}, set(), DAY, site[:5])
    assert ids(hub_list) == [6, 7]
    # habr_ids are accepted as site_top too
    assert ids(feed.select_digest_hubs(items, {'python', 'rust'}, set(), DAY, [5])) == [6, 7]
    assert ids(feed.select_digest_hubs(items, {'python'}, set(), DAY, [])) == [5, 6]


def test_digest_lists_are_per_user_seen_exclusion():
    t = utc('2026-11-03T10:00:00Z')
    fetched = [item(i, t, score=10 - i) for i in range(1, 8)]
    a_site = feed.select_digest_site(fetched, {1, 2}, DAY)
    b_site = feed.select_digest_site(fetched, {3}, DAY)
    assert ids(a_site) == [3, 4, 5, 6, 7]
    assert ids(b_site) == [1, 2, 4, 5, 6, 7]
    a_hubs = feed.select_digest_hubs(fetched, {'python'}, {1, 2}, DAY, a_site[:5])
    b_hubs = feed.select_digest_hubs(fetched, {'python'}, {3}, DAY, b_site[:5])
    assert ids(a_hubs) == [] and ids(b_hubs) == [7]
    assert len(fetched) == 7              # inputs are not mutated


# =============================================================================
# Rendering
# =============================================================================


def _entries(start, count):
    return [(p, {'title': f'T{p}', 'score': p, 'comments_count': p * 2}) for p in range(start, start + count)]


def test_render_page_numbering_and_buttons():
    text, rows = feed.render_page(42, 'feed', NOW.date(), _entries(1, 10), total=25, offset=0)
    lines = text.split('\n')
    assert len(lines) == 10
    assert lines[0] == '1. T1 — рейтинг 1, комментарии 2'
    assert lines[9].startswith('10. T10 ')
    assert [len(r) for r in rows] == [5, 5, 1]
    assert rows[0][0] == ('1', 'hl:s:42:1')
    assert rows[1][4] == ('10', 'hl:s:42:10')
    assert rows[2] == [('Ещё 10 ▶', 'hl:m:42:10')]

    text, rows = feed.render_page(42, 'feed', NOW.date(), _entries(11, 10), total=25, offset=10)
    assert text.split('\n')[0].startswith('11. T11 ')
    assert rows[0][0] == ('11', 'hl:s:42:11')
    assert rows[-1] == [('Ещё 10 ▶', 'hl:m:42:20')]


def test_render_first_page_le_10_has_no_more_button():
    text, rows = feed.render_page(1, 'feed', NOW.date(), _entries(1, 7), total=7, offset=0)
    assert [len(r) for r in rows] == [5, 2]
    assert all('Ещё 10' not in t for row in rows for t, _ in row)
    assert text.endswith('Это все новые статьи за сутки')


def test_render_last_page_texts():
    for day, words in ((date(2026, 11, 2), '2 ноября'), (date(2026, 3, 8), '8 марта'),
                       (date(2026, 8, 31), '31 августа')):
        text, rows = feed.render_page(5, 'digest_site', day, _entries(6, 3), total=8, offset=0)
        assert text.endswith(f'Это все статьи за {words}')
        assert all(not d.startswith('hl:m') for row in rows for _, d in row)
    text, _ = feed.render_page(5, 'digest_hubs', '2026-11-02', _entries(6, 1), total=6, offset=0)
    assert text.endswith('Это все статьи за 2 ноября')
    text, _ = feed.render_page(5, 'feed', '2026-11-02', _entries(11, 2), total=12, offset=10)
    assert text.endswith('Это все новые статьи за сутки')


def test_render_missing_fields_fallback():
    text, _ = feed.render_page(1, 'feed', NOW.date(), [(1, None), (2, {'title': None, 'score': None})],
                               total=2, offset=0)
    lines = text.split('\n')
    assert lines[0] == '1. Без названия — рейтинг —, комментарии —'
    assert lines[1].startswith('2. Без названия ')


def test_render_feed_empty_states(tmp_db, monkeypatch):
    def no_fetch(*a, **k):
        pytest.fail('Habr must not be called without subscriptions')

    monkeypatch.setattr(habr_api, 'fetch_articles_since', no_fetch)
    assert feed.build_feed(1, NOW) == ('Подписок пока нет — /subscribe', [])

    habr_store.add_subscription(1, 'python')
    monkeypatch.setattr(habr_api, 'fetch_articles_since',
                        lambda since, *, wait_timeout=None, deadline=None: [item(1, hubs_=[('rust', 'collective')])])
    assert feed.build_feed(1, NOW) == ('За последние сутки в твоих хабах ничего нового', [])


def test_build_feed_persists_list_and_renders_first_page(tmp_db, monkeypatch):
    habr_store.add_subscription(1, 'python')
    fetched = [item(i, NOW - timedelta(minutes=i), score=100 - i) for i in range(1, 13)]
    monkeypatch.setattr(habr_api, 'fetch_articles_since', lambda since, *, wait_timeout=None, deadline=None: fetched)
    text, rows = feed.build_feed(1, NOW)
    assert len(text.split('\n')) == 10
    assert rows[-1][0][0] == 'Ещё 10 ▶'
    list_id = int(rows[0][0][1].split(':')[2])
    stored = habr_store.get_ranked_list(list_id)
    assert stored['kind'] == 'feed' and stored['user_id'] == 1 and stored['total'] == 12
    assert stored['list_date'] == '2026-11-04'
    assert habr_store.get_viewed_ids(1) == set()


def test_render_title_sanitized():
    raw = 'A &amp; B &quot;q&quot;\u202eevil\u2066\u200f\x00\x1b\x85 tail'
    assert feed.sanitize_title(raw) == 'A & B "q"evil tail'
    long = 'word ' * 100
    assert len(feed.sanitize_title(long)) <= 200
    # sanitize before truncating: 300 bidi chars do not eat the visible length
    assert feed.sanitize_title('\u202e' * 300 + 'ok') == 'ok'
    assert feed.sanitize_title('line1\nline2') == 'line1 line2'
    assert feed.sanitize_title(None) == 'Без названия'
    text, _ = feed.render_page(1, 'feed', NOW.date(), [(1, {'title': raw})], total=1, offset=0)
    assert '\u202e' not in text and '&amp;' not in text


def test_render_keyboard_is_data_rows():
    _, rows = feed.render_page(1, 'feed', NOW.date(), _entries(1, 10), total=30, offset=0)
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, list)
        for button in row:
            assert isinstance(button, tuple) and len(button) == 2
            assert all(isinstance(x, str) for x in button)
    code = ('import sys, feed, hubs; '
            'bad = [m for m in ("telebot", "bot") if m in sys.modules]; '
            'assert not bad, bad')
    root = __import__('pathlib').Path(feed.__file__).parent
    subprocess.run([sys.executable, '-c', code], cwd=root, check=True, timeout=60)


def test_format_date_ru_all_months():
    expected = ['1 января', '2 февраля', '3 марта', '4 апреля', '5 мая', '6 июня', '7 июля',
                '8 августа', '9 сентября', '10 октября', '11 ноября', '31 декабря']
    days = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 31]
    assert [feed.format_date_ru(date(2026, m, d)) for m, d in zip(range(1, 13), days)] == expected
    assert feed.MSK.utcoffset(None) == timedelta(hours=3)
    assert feed.msk_date(utc('2026-11-03T21:00:00Z')) == date(2026, 11, 4)


def test_view_source_mapping():
    assert feed.view_source('digest_site') == 'digest'
    assert feed.view_source('digest_hubs') == 'digest'
    assert feed.view_source('feed') == 'feed'
    with pytest.raises(ValueError):
        feed.view_source('link')


# =============================================================================
# Callback codec
# =============================================================================


def _make_list(user_id=1, kind='feed', n=25, list_date='2026-11-04'):
    return habr_store.create_ranked_list(user_id, kind, list_date, [1000 + i for i in range(1, n + 1)])


def test_callback_codec_roundtrip_ascii(tmp_db):
    list_id = _make_list(n=25)
    s = feed.decode_list_callback(feed.encode_summary(list_id, 13), 1)
    assert (s.action, s.list_id, s.value, s.habr_id) == ('s', list_id, 13, 1013)
    m = feed.decode_list_callback(feed.encode_more(list_id, 20), 1)
    assert (m.action, m.list_id, m.value, m.kind, m.total) == ('m', list_id, 20, 'feed', 25)
    assert feed.encode_summary(list_id, 13).isascii()


def test_encode_rejects_out_of_range():
    for args in ((0, 1), (10 ** 9, 1), (1, 0), (1, 10 ** 4), (-1, 1)):
        with pytest.raises(ValueError):
            feed.encode_summary(*args)
    with pytest.raises(ValueError):
        feed.encode_more(1, -10)


def test_callback_forged_formats_rejected(tmp_db, monkeypatch):
    _make_list()
    lookups = []
    monkeypatch.setattr(habr_store, 'get_ranked_list', lambda *a, **k: lookups.append(a))
    forged = ['hl:s:1:', 'hl:x:1:1', 'hl:s:١:1', 'hl:s:1234567890:1', 'hl:s:1:12345', 'hl:s:1:1\n',
              'hl:s:-1:1', 'HL:s:1:1', 'hl:s:1:1:extra', 'hl:s:1:1 ', ' hl:s:1:1', 'hl:s:0:1',
              'hl:s:1:١', 'hl:m:1:10x', 'hl:s:01:1', 'hl:s:1:01', 'hl:m:1:010', 'hl:m:1:00', '', None, 12]
    for data in forged:
        assert feed.decode_list_callback(data, 1) is None, data
    assert lookups == []


def test_callback_foreign_list_rejected(tmp_db, caplog):
    list_id = _make_list(user_id=111)
    with caplog.at_level(logging.WARNING, logger='feed'):
        assert feed.decode_list_callback(f'hl:s:{list_id}:1', 222) is None
    assert any('user_id=222' in r.message and r.levelno == logging.WARNING for r in caplog.records)
    assert feed.decode_list_callback(f'hl:s:{list_id}:1', 111) is not None


def test_callback_missing_list_rejected(tmp_db):
    assert feed.decode_list_callback('hl:s:999:1', 1) is None


def test_callback_expired_list_rejected(tmp_db, monkeypatch):
    list_id = _make_list()
    created = datetime.fromisoformat(habr_store.get_ranked_list(list_id)['created_at'])
    data = f'hl:s:{list_id}:1'
    assert feed.decode_list_callback(data, 1, now=created + timedelta(days=30)) is not None
    assert feed.decode_list_callback(data, 1, now=created + timedelta(days=30, seconds=1)) is None
    monkeypatch.setattr(feed, '_now', lambda: created + timedelta(days=31))
    assert feed.decode_list_callback(data, 1) is None


def test_callback_out_of_range_rejected(tmp_db):
    list_id = _make_list(n=25)
    for data in (f'hl:s:{list_id}:0', f'hl:s:{list_id}:26', f'hl:s:{list_id}:9999',
                 f'hl:m:{list_id}:15', f'hl:m:{list_id}:30', f'hl:m:{list_id}:9990'):
        assert feed.decode_list_callback(data, 1) is None, data
    assert feed.decode_list_callback(f'hl:s:{list_id}:25', 1) is not None


def test_callback_valid_summary_returns_habr_id(tmp_db):
    list_id = _make_list(n=12)
    click = feed.decode_list_callback(f'hl:s:{list_id}:12', 1)
    assert click.action == 's' and click.habr_id == 1012 and click.kind == 'feed'
    more = feed.decode_list_callback(f'hl:m:{list_id}:10', 1)
    assert more.action == 'm' and more.habr_id is None
    assert feed.decode_list_callback('hl:s:x:1', 1) is None


def test_callback_data_max_64_bytes():
    samples = [feed.encode_summary(999_999_999, 9999), feed.encode_more(999_999_999, 9990)]
    samples += [d for row in feed.render_page(999_999_999, 'feed', NOW.date(), _entries(9990, 10),
                                              total=10 ** 5, offset=9980)[1] for _, d in row]
    for data in samples:
        assert len(data.encode('utf-8')) <= 64, data


def _positions(text):
    return [int(line.split('.', 1)[0]) for line in text.split('\n') if line and line[0].isdigit()]


def _with_meta(n):
    habr_store.upsert_habr_articles([item(1000 + i, score=i, comments=i) for i in range(1, n + 1)])


def test_more_page_digest_offsets(tmp_db):
    _with_meta(22)
    list_id = _make_list(kind='digest_site', n=22, list_date='2026-11-03')
    for offset, expected, has_more in ((0, range(6, 16), True), (10, range(16, 23), False)):
        click = feed.decode_list_callback(f'hl:m:{list_id}:{offset}', 1)
        assert click is not None
        text, rows = feed.render_list_page(click.list_id, click.value)
        assert _positions(text) == list(expected)
        assert any(d.startswith('hl:m') for row in rows for _, d in row) is has_more
    assert text.endswith('Это все статьи за 3 ноября')
    assert rows[-1][-1] == ('22', f'hl:s:{list_id}:22')
    for offset in (5, 20, 30):
        assert feed.decode_list_callback(f'hl:m:{list_id}:{offset}', 1) is None, offset
    hubs_list = _make_list(kind='digest_hubs', n=16, list_date='2026-11-03')
    text, rows = feed.render_list_page(hubs_list, 10)
    assert _positions(text) == [16]


def test_more_page_feed_offsets(tmp_db):
    _with_meta(25)
    list_id = _make_list(kind='feed', n=25)
    for offset, expected, has_more in ((10, range(11, 21), True), (20, range(21, 26), False)):
        click = feed.decode_list_callback(f'hl:m:{list_id}:{offset}', 1)
        assert click is not None
        text, rows = feed.render_list_page(click.list_id, click.value)
        assert _positions(text) == list(expected)
        assert any(d.startswith('hl:m') for row in rows for _, d in row) is has_more
    assert text.endswith('Это все новые статьи за сутки')
    for offset in (0, 5, 30):
        assert feed.decode_list_callback(f'hl:m:{list_id}:{offset}', 1) is None, offset
    assert feed.render_list_page(999, 10) is None


def test_callback_never_calls_habr_or_llm(tmp_db, monkeypatch):
    import summarizer

    def boom(*a, **k):
        pytest.fail('network/LLM must not be called by the codec')

    for name in ('fetch_articles_since', 'fetch_article', 'fetch_hub_catalog', '_request'):
        monkeypatch.setattr(habr_api, name, boom)
    # the single LLM entry point used by every generator, patched explicitly
    monkeypatch.setattr(summarizer, '_generate', boom)
    for name in dir(summarizer):
        if name.startswith(('generate', '_generate', 'summarize')) and callable(getattr(summarizer, name)):
            monkeypatch.setattr(summarizer, name, boom)
    list_id = _make_list(n=25)
    assert feed.decode_list_callback('hl:s:١:1', 1) is None
    assert feed.decode_list_callback(f'hl:s:{list_id}:3', 2) is None
    assert feed.decode_list_callback(f'hl:s:{list_id}:3', 1).habr_id == 1003
    assert feed.decode_list_callback(f'hl:m:{list_id}:10', 1) is not None
    assert feed.render_list_page(list_id, 10) is not None


# =============================================================================
# Fetch cache
# =============================================================================


class FakeList:
    def __init__(self, items=None, error=None):
        self.items, self.error, self.calls = items or [item(1)], error, []

    def __call__(self, since, *, wait_timeout=None, deadline=None):
        self.calls.append((since, wait_timeout))
        if self.error is not None:
            raise self.error
        return list(self.items)


def test_feed_fetch_cache_reuses_within_5_min(tmp_db, monkeypatch):
    fake = FakeList()
    monkeypatch.setattr(habr_api, 'fetch_articles_since', fake)
    clock = [NOW]
    monkeypatch.setattr(feed, '_now', lambda: clock[0])
    feed.fetch_recent()
    clock[0] = NOW + timedelta(minutes=4, seconds=59)
    feed.fetch_recent()
    assert len(fake.calls) == 1
    clock[0] = NOW + timedelta(minutes=5, seconds=1)
    feed.fetch_recent()
    assert len(fake.calls) == 2
    assert fake.calls[1][0] == clock[0] - timedelta(hours=24)


def test_feed_fetch_cache_ttl_patchable(tmp_db, monkeypatch):
    fake = FakeList()
    monkeypatch.setattr(habr_api, 'fetch_articles_since', fake)
    monkeypatch.setattr(feed, 'FETCH_CACHE_TTL', timedelta(seconds=1))
    feed.fetch_recent(NOW)
    feed.fetch_recent(NOW + timedelta(seconds=2))
    assert len(fake.calls) == 2


def test_feed_fetch_cache_concurrent_single_fetch(tmp_db, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def blocking(since, *, wait_timeout=None, deadline=None):
        calls.append(since)
        started.set()
        assert release.wait(5)
        return [item(1)]

    monkeypatch.setattr(habr_api, 'fetch_articles_since', blocking)
    lock = SignallingLock()
    monkeypatch.setattr(feed, '_cache_lock', lock)
    results = []
    threads = [threading.Thread(target=lambda: results.append(ids(feed.fetch_recent(NOW)))) for _ in range(2)]
    threads[0].start()
    assert started.wait(5)
    threads[1].start()
    # Thread 2 passed the first cache check while the cache was still empty and is now
    # waiting on the single-flight lock: only the re-check under the lock saves a fetch
    assert lock.contended.wait(5)
    threads[1].join(0.2)
    assert threads[1].is_alive() and len(calls) == 1
    release.set()
    for t in threads:
        t.join(5)
        assert not t.is_alive()
    assert len(calls) == 1 and results == [[1], [1]]


def test_feed_fetch_upserts_metadata(tmp_db, monkeypatch):
    fetched = [item(1, score=7, comments=3, hubs_=[('python', 'collective'), ('bitrix', 'corporative')],
                    tags=('ai', 'ml'))]
    monkeypatch.setattr(habr_api, 'fetch_articles_since', FakeList(fetched))
    feed.fetch_recent(NOW)
    stored = habr_store.get_habr_article(1)
    assert stored['title'] == 'Article 1' and stored['score'] == 7 and stored['comments_count'] == 3
    assert stored['hubs'] == [{'alias': 'bitrix', 'hub_type': 'corporative'},
                              {'alias': 'python', 'hub_type': 'collective'}]
    assert stored['tags'] == ['ai', 'ml']


def test_feed_fetch_passes_handler_wait_timeout_and_propagates_busy(tmp_db, monkeypatch):
    monkeypatch.setattr(habr_api, 'HANDLER_WAIT_TIMEOUT', 7.0)
    fake = FakeList()
    monkeypatch.setattr(habr_api, 'fetch_articles_since', fake)
    feed.fetch_recent(NOW)
    assert fake.calls[0][1] == 7.0
    feed._reset_for_tests()
    for error in (habr_api.HabrBusy('busy'), habr_api.HabrUnavailable('network')):
        monkeypatch.setattr(habr_api, 'fetch_articles_since', FakeList(error=error))
        with pytest.raises(type(error)):
            feed.fetch_recent(NOW)
    habr_store.add_subscription(1, 'python')
    monkeypatch.setattr(habr_api, 'fetch_articles_since', FakeList(error=habr_api.HabrBusy('busy')))
    with pytest.raises(habr_api.HabrBusy):
        feed.build_feed(1, NOW)


def test_reset_for_tests_clears_cache_and_registry(tmp_db, monkeypatch):
    fake = FakeList()
    monkeypatch.setattr(habr_api, 'fetch_articles_since', fake)
    feed.fetch_recent(NOW)
    feed.REGISTRY.click(1, 10)
    registry = feed.REGISTRY
    feed._reset_for_tests()
    assert feed.REGISTRY is registry and len(registry) == 0
    feed.fetch_recent(NOW)
    assert len(fake.calls) == 2


# =============================================================================
# InFlightRegistry
# =============================================================================


def _run_in_thread(registry, habr_id, generate):
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault('outcome', registry.run(habr_id, generate)))
    thread.start()
    return thread, out


def test_registry_same_user_second_click_one_generation():
    registry = feed.InFlightRegistry()
    started, release = threading.Event(), threading.Event()
    calls = []

    def generate():
        calls.append(1)
        started.set()
        assert release.wait(5)
        return 'summary'

    first = registry.click(5, 100)
    assert first == feed.ClickResult(feed.CLICK_STARTED, None)
    thread, out = _run_in_thread(registry, 5, generate)
    assert started.wait(5)
    second = registry.click(5, 100)
    assert second.kind == feed.CLICK_ALREADY_RUNNING and second.text == 'Саммари уже готовится'
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert calls == [1]
    assert out['outcome'] == feed.GenerationOutcome('summary', None, set())
    assert len(registry) == 0


def test_registry_other_user_becomes_waiter():
    registry = feed.InFlightRegistry()
    started, release = threading.Event(), threading.Event()

    def generate():
        started.set()
        assert release.wait(5)
        return 'summary'

    registry.click(5, 100)
    thread, out = _run_in_thread(registry, 5, generate)
    assert started.wait(5)
    waiter = registry.click(5, 200)
    assert waiter.kind == feed.CLICK_WAITER and waiter.text == 'Саммари уже готовится, пришлю сюда'
    again = registry.click(5, 200)                 # waiter clicks again: not registered twice
    assert again.kind == feed.CLICK_ALREADY_RUNNING
    release.set()
    thread.join(5)
    assert out['outcome'].waiters == {200}
    assert out['outcome'].result == 'summary'
    assert len(registry) == 0
    assert registry.finish(5) == set()


def test_registry_cleared_after_exception():
    registry = feed.InFlightRegistry()
    registry.click(5, 100)
    registry.click(5, 200)
    error = RuntimeError('llm down')

    def failing():
        raise error

    outcome = registry.run(5, failing)
    assert outcome.error is error and outcome.result is None and outcome.waiters == {200}
    assert len(registry) == 0
    assert registry.click(5, 100).kind == feed.CLICK_STARTED


def test_registry_cleared_after_base_exception():
    registry = feed.InFlightRegistry()
    registry.click(5, 100)

    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        registry.run(5, interrupted)
    assert len(registry) == 0


def test_registry_distinct_habr_ids_independent():
    registry = feed.InFlightRegistry()
    assert registry.click(1, 100).kind == feed.CLICK_STARTED
    assert registry.click(2, 100).kind == feed.CLICK_STARTED
    assert registry.click(2, 200).kind == feed.CLICK_WAITER
    assert registry.finish(1) == set()
    assert len(registry) == 1                     # only habr_id 2 is still in flight
    assert registry.finish(2) == {200}


def test_module_registry_is_single_instance():
    registry = feed.REGISTRY
    assert isinstance(registry, feed.InFlightRegistry)
    registry.click(9, 1)
    feed._reset_for_tests()
    assert feed.REGISTRY is registry
    assert len(registry) == 0


# =============================================================================
# Text-cleaning helpers
# =============================================================================


def test_url_mention_command_patterns():
    assert feed.URL_RE.search('see https://x.y/a now').group() == 'https://x.y/a'
    assert feed.URL_RE.search('www.example.com/page').group() == 'www.example.com/page'
    assert feed.MENTION_RE.search('hi @name!').group() == '@name'
    assert feed.COMMAND_RE.search('run /start please').group() == '/start'
    assert feed.COMMAND_RE.search('/start@my_bot').group() == '/start@my_bot'
    for plain in ('plain words only', 'mail me at user@example.com', 'a/b path', 'http ftp'):
        assert not feed.URL_RE.search(plain), plain
        assert not feed.MENTION_RE.search(plain), plain
        assert not feed.COMMAND_RE.search(plain), plain


@pytest.mark.parametrize('text, found', [
    ('Перейдите на evil.ru/login и введите пароль', 'evil.ru/login'),
    ('Смотрите t.me/scam', 't.me/scam'),
    ('см. bit.ly/abc', 'bit.ly/abc'),
    ('Сайт example.com, заходите', 'example.com'),
    ('Пишите на sub.domain.org.', 'sub.domain.org'),
    ('ссылка: EVIL.RU/Login', 'EVIL.RU/Login'),
    ('домен сайт.рф/путь', 'сайт.рф/путь'),
    ('punycode xn--80ak6aa92e.xn--p1ai тоже', 'xn--80ak6aa92e.xn--p1ai'),
    # Any TLD-looking suffix counts when a path or a port follows
    ('Код на evil.io/x', 'evil.io/x'),
    ('Скачать: evil.net/payload', 'evil.net/payload'),
    ('порт evil.dev:8080/x', 'evil.dev:8080/x'),
    ('Войти: evil.icu/login', 'evil.icu/login'),
    ('см. evil.vip/x', 'evil.vip/x'),
    ('см. evil.space/x', 'evil.space/x'),
    ('см. evil.page/x', 'evil.page/x'),
])
def test_domain_pattern_finds_bare_domains(text, found):
    assert feed.DOMAIN_RE.search(text).group() == found


@pytest.mark.parametrize('text', [
    'Node.js и Vue.js против Next.js',
    'Правим config.yaml и package.json',
    'Миграция на ASP.NET Core',
    'Запусти main.py и setup.py', 'README.md', 'deploy.sh', 'src/main.rs', 'libfoo.so',
    'Python 3.12 и v1.2.3', 'Spring.io без ссылки', 'кластер на k8s.io',
    'mail me at user@example.com', 'т.е. и т.д.', 'e.g. i.e.', 'U.S.A.',
    'файл config.com.yaml',
    'plain words only',
])
def test_domain_pattern_ignores_tech_names(text):
    assert not feed.DOMAIN_RE.search(text), text


@pytest.mark.parametrize('text', [
    'tg://resolve?domain=x', 'открой ton://x', 'ftp://host/file', 'https://x.y/a', 'www.example.com',
])
def test_url_pattern_any_scheme(text):
    assert feed.URL_RE.search(text)
    assert feed.is_unsafe_description(text)


@pytest.mark.parametrize('text', [
    'evil.tg', 'evil.cloud', 'evil.io', 'evil.ai', 'evil.dev', 'evil.app', 'evil.net', 'evil.co',
    'evil.md', 'evil.zip', 'evil.website', 'Заходи на evil.lol сегодня', 'sub.evil.asia.',
])
def test_strict_description_check_rejects_any_bare_domain(text):
    assert feed.has_bare_domain(text), text
    assert feed.is_unsafe_description(text), text


@pytest.mark.parametrize('text', [
    'Автор переводит Node.js и ASP.NET на config.yaml.',
    'Сравнение Vue.js, package.json, main.py и socket.io.',
    'ADO.NET, tsconfig.json, App.tsx, Main.java, settings.toml',
    'Python 3.12, v1.2.3, т.е., e.g. и U.S.A.',
    'Пишет на team@example.com, код в src/app.',
])
def test_strict_description_check_keeps_tech_names(text):
    assert not feed.has_bare_domain(text), text
    assert not feed.is_unsafe_description(text), text


def test_lead_fallback_keeps_bare_tech_names_but_cuts_paths():
    out = feed.fallback_description(
        '<p>Node.js, README.md и main.py; ссылка evil.icu/login и tg://resolve?domain=x.</p>')
    assert 'Node.js' in out and 'README.md' in out and 'main.py' in out
    for bad in ('evil.icu', '/login', 'tg://', 'resolve'):
        assert bad not in out


def test_unsafe_text_patterns_include_bare_domains():
    assert feed.DOMAIN_RE in feed.UNSAFE_TEXT_PATTERNS
    assert {feed.URL_RE, feed.MENTION_RE, feed.COMMAND_RE} <= set(feed.UNSAFE_TEXT_PATTERNS)


def test_fallback_description_strips_bare_domains():
    out = feed.fallback_description('<p>Идите на evil.ru/login, t.me/scam и bit.ly/abc. Node.js рулит.</p>')
    for bad in ('evil.ru', 't.me', 'bit.ly', '/login', '/scam', '/abc'):
        assert bad not in out
    assert 'Node.js рулит.' in out


def test_strip_control_bidi():
    text = 'a\x00b\x1fc\x7fd\x85e\x9ff\u202ag\u202eh\u2066i\u2069j\u200ek\u200fl\u061cm'
    assert feed.strip_control_bidi(text) == 'abcdefghijklm'
    assert feed.strip_control_bidi('line1\nline2\tx') == 'line1\nline2\tx'
    assert feed.strip_control_bidi('Привет, мир — ok') == 'Привет, мир — ok'
    assert feed.strip_control_bidi('a\rb') == 'ab'
    assert feed.strip_control_bidi(None) == ''


def test_html_to_text():
    assert feed.html_to_text('<p>Hello&nbsp;<b>world</b></p>\n\n<p>A &amp; B</p>') == 'Hello world A & B'
    assert feed.html_to_text('<div>  many   \n spaces </div>') == 'many spaces'
    assert feed.html_to_text('') == ''
    assert feed.html_to_text(None) == ''


def test_fallback_description_cleaned_and_cut():
    lead = ('<p>Intro \u202etext\x07 see https://evil.example/x and @someone, run /start now.</p>'
            '<p>' + 'longword ' * 60 + '</p>')
    out = feed.fallback_description(lead)
    for bad in ('https://', 'evil.example', '@someone', '/start', '\u202e', '\x07', '<p>'):
        assert bad not in out
    assert out.startswith('Intro text see and, run now.')
    assert len(out) <= 250
    assert out.endswith('…')
    assert out[:-1].rstrip().split(' ')[-1] == 'longword'      # never cut mid-word
    assert feed.fallback_description('<p>Short lead.</p>') == 'Short lead.'
    assert feed.fallback_description('') == ''
    assert feed.fallback_description(None) == ''
    assert len(feed.fallback_description('<p>' + 'abc ' * 30 + '</p>', limit=50)) <= 50


def test_strip_control_bidi_removes_format_and_separator_chars():
    # zero-width space/joiner, word joiner, BOM, soft hyphen, line/paragraph separators
    invisible = [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x2028, 0x2029]
    text = 'a' + ''.join(chr(c) for c in invisible) + 'b'
    assert feed.strip_control_bidi(text) == 'ab'
    assert feed.sanitize_title('/st' + chr(0x200B) + 'art') == '/start'


def test_html_to_text_inline_and_script():
    assert feed.html_to_text('<p><b>Py</b>thon and <i>Ru</i>st</p>') == 'Python and Rust'
    assert feed.html_to_text('<p>one</p><p>two</p>line<br>break') == 'one two line break'
    assert feed.html_to_text('<script>alert(1)</script><style>p{}</style><p>text</p>') == 'text'


# =============================================================================
# Handler deadline (audit CA-1): one end-to-end budget per /feed fetch
# =============================================================================


def test_feed_fetch_passes_one_handler_deadline(tmp_db, monkeypatch):
    monkeypatch.setattr(habr_api, 'HANDLER_DEADLINE', 45)
    seen = []

    def fetch(since, *, wait_timeout=None, deadline=None):
        seen.append((wait_timeout, deadline))
        return [item(1)]

    monkeypatch.setattr(habr_api, 'fetch_articles_since', fetch)
    before = time.monotonic()
    feed.fetch_recent(NOW)
    (wait_timeout, deadline), = seen
    assert wait_timeout == habr_api.HANDLER_WAIT_TIMEOUT
    assert before + 45 <= deadline <= time.monotonic() + 45


def test_feed_cache_lock_wait_bounded_by_deadline(tmp_db, monkeypatch):
    """The single-flight wait counts against the same deadline as the fetch."""
    monkeypatch.setattr(habr_api, 'HANDLER_DEADLINE', 0.2)
    monkeypatch.setattr(habr_api, 'fetch_articles_since', FakeList())
    feed._cache_lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(habr_api.HabrBusy):
            feed.fetch_recent(NOW)
        assert 0.15 <= time.monotonic() - started < 2       # not HANDLER_WAIT_TIMEOUT (30 s)
    finally:
        feed._cache_lock.release()
