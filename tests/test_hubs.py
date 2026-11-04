import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import feed
import habr_api
import habr_store
import hubs

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_modules():
    feed._reset_for_tests()
    hubs._reset_for_tests()
    yield
    feed._reset_for_tests()
    hubs._reset_for_tests()


def _hub(alias, rating=1.0, title=None, hub_type='collective'):
    return {'alias': alias, 'title': title or alias.title(), 'rating': rating, 'hub_type': hub_type}


CATALOG = [_hub('python', 100), _hub('rust', 90), _hub('1C', 80), _hub('DIY', 70), _hub('bitrix', 60)]


class FakeCatalog:
    """Fake habr_api.fetch_hub_catalog that records calls."""

    def __init__(self, result=CATALOG, error=None):
        self.result, self.error, self.calls = result, error, []

    def __call__(self, *, wait_timeout=None, deadline=None):
        self.calls.append(wait_timeout)
        if self.error is not None:
            raise self.error
        return list(self.result)


@pytest.fixture
def catalog(tmp_db, monkeypatch):
    monkeypatch.setattr(habr_store, '_now', lambda: NOW.isoformat(timespec='microseconds'))
    habr_store.replace_hub_catalog(CATALOG)
    return CATALOG


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def test_resolve_alias_case_insensitive(catalog):
    assert hubs.resolve('python').found == ['python']
    assert hubs.resolve('PYTHON').found == ['python']
    assert hubs.resolve('1c').found == ['1C']
    assert hubs.resolve('diy').found == ['DIY']


def test_resolve_hub_url_variants(catalog):
    good = [
        'https://habr.com/ru/hubs/python/',
        'habr.com/ru/hubs/Python/articles/',
        'www.habr.com/en/hubs/rust/?x=1',
        'http://habr.com/hubs/rust#top',
    ]
    result = hubs.resolve(' '.join(good))
    assert result.found == ['python', 'rust']
    assert result.not_found == []

    bad = [
        'https://evil.com/ru/hubs/python/',
        'habr.com.evil.com/ru/hubs/python/',
        'https://habr.com@evil.com/ru/hubs/python/',
        'https://habr.com:8080/ru/hubs/python/',
        'ftp://habr.com/ru/hubs/python/',
        'habr.com',
        'https://habr.com/ru/',
        'https://habr.com/ru/articles/123/',
    ]
    result = hubs.resolve(' '.join(bad))
    assert result.found == []
    assert result.not_found == bad


def test_resolve_splits_found_and_not_found(catalog):
    result = hubs.resolve('python rust xyz123')
    assert result.found == ['python', 'rust']
    assert result.not_found == ['xyz123']


def test_resolve_empty_input(catalog):
    assert hubs.resolve('') == hubs.Resolution([], [])
    assert hubs.resolve('   ') == hubs.Resolution([], [])


def test_resolve_deduplicates_tokens(catalog):
    result = hubs.resolve('python Python https://habr.com/ru/hubs/python/ python')
    assert result.found == ['python']
    assert result.not_found == []


def test_not_found_echo_limited_and_sanitized():
    tokens = [f'bad\u202e{i}\x07' + 'x' * 60 for i in range(15)]
    text = hubs.format_not_found(tokens)
    assert text.startswith('Не найдены: ')
    shown = text[len('Не найдены: '):].split(', ')
    assert shown[-1] == '…'
    shown = shown[:-1]
    assert len(shown) == 10
    for token in shown:
        assert len(token) <= 40
        assert '\u202e' not in token and '\x07' not in token
    assert shown[0].startswith('bad0x')
    assert hubs.format_not_found([]) == ''
    assert hubs.format_not_found(['xyz']) == 'Не найдены: xyz'


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def test_subscribe_idempotent(catalog):
    assert hubs.subscribe(1, ['python']) == ['python']
    assert hubs.subscribe(1, ['python']) == []
    assert hubs.list_subscriptions(1) == ['python']


def test_unsubscribe_removes_only_that_hub(catalog):
    hubs.subscribe(1, ['python', 'rust'])
    hubs.subscribe(2, ['python'])
    assert hubs.unsubscribe(1, 'python') is True
    assert hubs.list_subscriptions(1) == ['rust']
    assert hubs.list_subscriptions(2) == ['python']


# ---------------------------------------------------------------------------
# Catalog freshness
# ---------------------------------------------------------------------------


def _set_catalog_age(monkeypatch, age):
    monkeypatch.setattr(habr_store, '_now', lambda: (NOW - age).isoformat(timespec='microseconds'))
    habr_store.replace_hub_catalog([_hub('old', 5)])
    monkeypatch.setattr(hubs, '_now', lambda: NOW)


def test_catalog_fresh_no_refresh(tmp_db, monkeypatch):
    _set_catalog_age(monkeypatch, timedelta(days=6, hours=23))
    fake = FakeCatalog()
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', fake)
    hubs.ensure_catalog()
    assert fake.calls == []


def test_catalog_refresh_when_older_than_7_days(tmp_db, monkeypatch):
    _set_catalog_age(monkeypatch, timedelta(days=7, seconds=1))
    fake = FakeCatalog()
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', fake)
    monkeypatch.setattr(habr_store, '_now', lambda: NOW.isoformat(timespec='microseconds'))
    hubs.ensure_catalog()
    hubs.ensure_catalog()
    assert len(fake.calls) == 1
    assert habr_store.find_hub('python')['alias'] == 'python'
    assert habr_store.find_hub('old') is None


def test_refresh_passes_handler_wait_timeout(tmp_db, monkeypatch):
    fake = FakeCatalog()
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', fake)
    monkeypatch.setattr(habr_api, 'HANDLER_WAIT_TIMEOUT', 12.5)
    hubs.ensure_catalog()
    assert fake.calls == [12.5]


@pytest.mark.parametrize('error', [habr_api.HabrUnavailable('network'), habr_api.HabrBusy('busy')])
def test_catalog_refresh_failure_uses_stale_copy(tmp_db, monkeypatch, error):
    _set_catalog_age(monkeypatch, timedelta(days=30))
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', FakeCatalog(error=error))
    hubs.ensure_catalog()            # no exception
    assert habr_store.find_hub('old')['alias'] == 'old'


def test_catalog_busy_with_stale_copy_uses_copy(tmp_db, monkeypatch):
    _set_catalog_age(monkeypatch, timedelta(days=8))
    fake = FakeCatalog(error=habr_api.HabrBusy('busy'))
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', fake)
    hubs.ensure_catalog()
    assert len(fake.calls) == 1
    assert hubs.resolve('old').found == ['old']


def test_catalog_refresh_failure_without_copy(tmp_db, monkeypatch):
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', FakeCatalog(error=habr_api.HabrUnavailable('http', 503)))
    with pytest.raises(hubs.CatalogUnavailable):
        hubs.ensure_catalog()


def test_catalog_busy_without_copy_propagates(tmp_db, monkeypatch):
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', FakeCatalog(error=habr_api.HabrBusy('busy')))
    with pytest.raises(habr_api.HabrBusy):
        hubs.ensure_catalog()
    assert not issubclass(habr_api.HabrBusy, hubs.CatalogUnavailable)


def test_catalog_import_all_invalid_without_copy_is_unavailable(tmp_db, monkeypatch):
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', FakeCatalog(result=[_hub('bad alias')]))
    with pytest.raises(hubs.CatalogUnavailable):
        hubs.ensure_catalog()


def test_catalog_refresh_single_flight(tmp_db, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def blocking_fetch(*, wait_timeout=None, deadline=None):
        calls.append(wait_timeout)
        started.set()
        assert release.wait(5)
        return list(CATALOG)

    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', blocking_fetch)
    results, errors = [], []

    def worker():
        try:
            hubs.ensure_catalog()
            results.append(habr_store.find_hub('python')['alias'])
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    first = threading.Thread(target=worker)
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=worker)
    second.start()
    second.join(0.2)                 # second thread is blocked on the refresh lock
    assert second.is_alive()
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(calls) == 1
    assert results == ['python', 'python']


def test_catalog_single_flight_reuses_failed_outcome(tmp_db, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def blocking_fail(*, wait_timeout=None, deadline=None):
        calls.append(wait_timeout)
        started.set()
        assert release.wait(5)
        raise habr_api.HabrUnavailable('network')

    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', blocking_fail)
    errors = []

    def worker():
        try:
            hubs.ensure_catalog()
        except Exception as e:
            errors.append(type(e))

    threads = [threading.Thread(target=worker)]
    threads[0].start()
    assert started.wait(5)
    threads.append(threading.Thread(target=worker))
    threads[1].start()
    threads[1].join(0.2)
    release.set()
    for t in threads:
        t.join(5)
        assert not t.is_alive()
    assert len(calls) == 1
    assert errors == [hubs.CatalogUnavailable, hubs.CatalogUnavailable]


def test_catalog_import_rejects_bad_aliases(tmp_db, monkeypatch, caplog):
    assert hubs.HUB_ALIAS_RE is habr_store.HUB_ALIAS_RE
    items = [_hub('python'), _hub('a' * 51), _hub('has space'), _hub('a:b'), _hub('питон'), _hub('ok_alias-2')]
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', FakeCatalog(result=items))
    with caplog.at_level(logging.WARNING, logger='hubs'):
        hubs.ensure_catalog()
    stored = sorted(h['alias'] for h in habr_store.top_hubs(100))
    assert stored == ['ok_alias-2', 'python']
    assert sum('dropped unsafe alias' in r.message for r in caplog.records) == 4


def test_catalog_import_only_collective(tmp_db, monkeypatch):
    items = [_hub('python'), _hub('megacorp', hub_type='corporative'), _hub('x', hub_type=None)]
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', FakeCatalog(result=items))
    hubs.ensure_catalog()
    assert [h['alias'] for h in habr_store.top_hubs(100)] == ['python']


# ---------------------------------------------------------------------------
# Top-30 and hub callbacks
# ---------------------------------------------------------------------------


def test_top30_ordered_by_rating(tmp_db):
    habr_store.replace_hub_catalog([_hub(f'hub{i:02d}', rating=float(i)) for i in range(40)])
    top = hubs.top_hubs()
    assert len(top) == 30
    assert [h['rating'] for h in top] == sorted((float(i) for i in range(10, 40)), reverse=True)


def test_top30_ties_stable_by_alias(tmp_db):
    habr_store.replace_hub_catalog([_hub('b', 5), _hub('a', 5), _hub('c', 9)])
    assert [h['alias'] for h in hubs.top_hubs()] == ['c', 'a', 'b']


def test_hub_callback_data_within_64_bytes():
    alias = 'A' * 50
    for data in (hubs.subscribe_callback(alias), hubs.unsubscribe_callback(alias)):
        assert len(data.encode('utf-8')) <= 64
    assert hubs.subscribe_callback('python') == 'hs:a:python'
    assert hubs.unsubscribe_callback('1C') == 'hs:d:1C'
    with pytest.raises(ValueError):
        hubs.subscribe_callback('A' * 51)
    with pytest.raises(ValueError):
        hubs.unsubscribe_callback('bad alias')


def test_hub_callback_add_alias_must_be_in_catalog(catalog, caplog):
    assert hubs.parse_hub_callback('hs:a:python', 7) == ('a', 'python')
    assert hubs.parse_hub_callback('hs:a:1c', 7) == ('a', '1C')
    with caplog.at_level(logging.WARNING, logger='hubs'):
        for data in ('hs:a:unknown', 'hs:x:python', 'hs:a:python\n', 'hs:a:', 'hs:a:py thon', 'HS:a:python',
                     'hs:a:' + 'a' * 51, None, 123):
            assert hubs.parse_hub_callback(data, 7) is None, data
    assert all('user_id=7' in r.message for r in caplog.records)


def test_hub_callback_delete_validates_own_subscriptions(catalog, monkeypatch):
    hubs.subscribe(1, ['python'])
    habr_store.add_subscription(1, 'gone')           # hub dropped from a refreshed catalog
    assert habr_store.find_hub('gone') is None
    assert hubs.parse_hub_callback('hs:d:gone', 1) == ('d', 'gone')
    assert hubs.parse_hub_callback('hs:d:python', 1) == ('d', 'python')
    assert hubs.parse_hub_callback('hs:d:rust', 1) is None        # not subscribed
    assert hubs.parse_hub_callback('hs:d:python', 2) is None      # other user
    assert hubs.parse_hub_callback('hs:d:py:thon', 1) is None
    assert hubs.parse_hub_callback('hs:d:python\n', 1) is None


def test_reset_for_tests_replaces_refresh_lock():
    hubs._refresh_lock.acquire()
    hubs._reset_for_tests()
    assert hubs._refresh_lock.acquire(timeout=0.1)
    hubs._refresh_lock.release()


def test_resolve_reads_catalog_once_for_many_tokens(catalog, monkeypatch):
    calls = []
    real_top = habr_store.top_hubs
    monkeypatch.setattr(habr_store, 'top_hubs', lambda n: calls.append(n) or real_top(n))
    monkeypatch.setattr(habr_store, 'find_hub', lambda alias: pytest.fail('per-token lookup'))
    tokens = ' '.join([f'unknown{i}' for i in range(500)] + ['PYTHON', '1c'])
    result = hubs.resolve(tokens)
    assert result.found == ['python', '1C']
    assert len(result.not_found) == 500
    assert len(calls) == 1 and calls[0] >= habr_api.HUBS_MAX_PAGES * habr_api.HUBS_PER_PAGE
    assert hubs.resolve('') == hubs.Resolution([], [])
    assert len(calls) == 1                              # empty input reads nothing


def test_catalog_failure_with_stale_copy_retries_after_cooldown(tmp_db, monkeypatch):
    _set_catalog_age(monkeypatch, timedelta(days=8))
    fake = FakeCatalog(error=habr_api.HabrUnavailable('network'))
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', fake)
    hubs.ensure_catalog(NOW)
    hubs.ensure_catalog(NOW + timedelta(minutes=9))
    assert len(fake.calls) == 1                         # stale copy used, Habr not retried
    hubs.ensure_catalog(NOW + timedelta(minutes=10))
    assert len(fake.calls) == 2
    fake.error = None
    hubs.ensure_catalog(NOW + timedelta(minutes=30))
    assert len(fake.calls) == 3
    assert habr_store.find_hub('python') is not None


def test_reset_for_tests_clears_failure_cooldown(tmp_db, monkeypatch):
    _set_catalog_age(monkeypatch, timedelta(days=8))
    fake = FakeCatalog(error=habr_api.HabrUnavailable('network'))
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', fake)
    hubs.ensure_catalog(NOW)
    hubs._reset_for_tests()
    hubs.ensure_catalog(NOW + timedelta(minutes=1))
    assert len(fake.calls) == 2


# =============================================================================
# Handler deadline (audit CA-1): one end-to-end budget per catalog refresh
# =============================================================================


def test_refresh_passes_one_handler_deadline(tmp_db, monkeypatch):
    seen = []

    def fetch(*, wait_timeout=None, deadline=None):
        seen.append((wait_timeout, deadline))
        return list(CATALOG)

    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', fetch)
    before = time.monotonic()
    hubs.ensure_catalog()
    (wait_timeout, deadline), = seen
    assert wait_timeout == habr_api.HANDLER_WAIT_TIMEOUT
    assert before + habr_api.HANDLER_DEADLINE <= deadline <= time.monotonic() + habr_api.HANDLER_DEADLINE


def test_refresh_lock_wait_bounded_by_deadline(tmp_db, monkeypatch):
    """No stored copy, refresh lock held elsewhere: HabrBusy at the deadline, not after 30 s."""
    monkeypatch.setattr(habr_api, 'HANDLER_DEADLINE', 0.2)
    fake = FakeCatalog()
    monkeypatch.setattr(habr_api, 'fetch_hub_catalog', fake)
    hubs._refresh_lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(habr_api.HabrBusy):
            hubs.ensure_catalog()
        assert 0.15 <= time.monotonic() - started < 2
    finally:
        hubs._refresh_lock.release()
    assert fake.calls == []


def test_slow_multi_page_catalog_gives_busy_within_budget(tmp_db, monkeypatch):
    """Real habr_api pagination against a slow-but-answering fake Habr (security finding 2)."""
    monkeypatch.setattr(habr_api, 'HANDLER_DEADLINE', 0.4)
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0)
    pages_requested: list[int] = []

    class SlowPage:
        status_code = 200
        headers: dict = {}

        def __init__(self, page):
            ids = [f'hub_{page}_{i}' for i in range(habr_api.HUBS_PER_PAGE)]
            refs = {a: {'alias': a, 'titleHtml': a, 'statistics': {'rating': 1}} for a in ids}
            self.body = json.dumps({'pagesCount': 5, 'hubIds': ids, 'hubRefs': refs}).encode()

        def iter_content(self, chunk_size=1):
            time.sleep(0.15)          # Habr answers, but slowly
            yield self.body

        def close(self):
            pass

    def get(url, params=None, **kwargs):
        pages_requested.append(params['page'])
        return SlowPage(params['page'])

    monkeypatch.setattr(habr_api.requests, 'get', get)
    started = time.monotonic()
    with pytest.raises(habr_api.HabrBusy):
        hubs.ensure_catalog()                 # no stored copy: HabrBusy propagates
    assert time.monotonic() - started < 1.5
    assert 1 <= len(pages_requested) < 5
    assert habr_store.catalog_fetched_at() is None
