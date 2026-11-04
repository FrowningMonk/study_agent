"""Tests for habr_api: URL parsing, normalization, pagination, request policy.

The network is a fake `requests.get` (FakeHabr); no request reaches habr.com
(the conftest guard would fail the test).
"""

import copy
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

import habr_api
import scraper
from conftest import SignallingLock

UTC = timezone.utc


# =============================================================================
# Fakes and builders
# =============================================================================


class FakeResponse:
    """Streamed response: body is served in chunks through iter_content."""

    def __init__(self, status: int = 200, body=b'', headers: dict | None = None, chunk: int = 64 * 1024):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._chunk = chunk
        self.bytes_served = 0
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), self._chunk):
            part = self._body[i:i + self._chunk]
            self.bytes_served += len(part)
            yield part

    def close(self):
        self.closed = True


class FakeHabr:
    """Replaces requests.get; routes by a handler and logs every request."""

    def __init__(self, handler):
        self._handler = handler
        self.log: list[dict] = []

    def __call__(self, url, params=None, headers=None, timeout=None, allow_redirects=True, stream=False, **kw):
        self.log.append({
            'url': url, 'params': dict(params or {}), 'headers': dict(headers or {}),
            'timeout': timeout, 'allow_redirects': allow_redirects, 'stream': stream,
        })
        result = self._handler(url, dict(params or {}))
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def pages(self) -> list[int]:
        return [entry['params'].get('page') for entry in self.log]


@pytest.fixture
def fake_habr(monkeypatch):
    def _install(handler):
        if isinstance(handler, FakeResponse) or isinstance(handler, BaseException):
            response = handler
            handler = lambda url, params: response  # noqa: E731
        fake = FakeHabr(handler)
        monkeypatch.setattr(habr_api.requests, 'get', fake)
        return fake
    return _install


def by_page(pages: dict):
    """Handler returning pages[n] for ?page=n; unknown page fails the test."""
    def handler(url, params):
        page = int(params['page'])
        if page not in pages:
            pytest.fail(f'unexpected page requested: {page}')
        response = pages[page]
        return response if isinstance(response, (FakeResponse, BaseException)) else FakeResponse(200, response)
    return handler


@pytest.fixture
def list_fixture(fixture_text):
    return json.loads(fixture_text('habr_list_page.json'))


@pytest.fixture
def article_fixture(fixture_text):
    return json.loads(fixture_text('habr_article.json'))


@pytest.fixture
def hubs_fixture(fixture_text):
    return json.loads(fixture_text('habr_hubs_page.json'))


@pytest.fixture
def make_item(list_fixture):
    template = list_fixture['publicationRefs'][list_fixture['publicationIds'][0]]

    def _make(habr_id: int, published: datetime) -> dict:
        item = copy.deepcopy(template)
        item['id'] = str(habr_id)
        item['timePublished'] = published.isoformat()
        return item
    return _make


def make_page(items: list[dict], pages_count: int) -> dict:
    return {
        'pagesCount': pages_count,
        'publicationIds': [item['id'] for item in items],
        'publicationRefs': {item['id']: item for item in items},
    }


def descending(make_item, start_id: int, newest: datetime, count: int, step=timedelta(minutes=1)) -> list[dict]:
    return [make_item(start_id + i, newest - step * i) for i in range(count)]


SINCE = datetime(2026, 11, 2, 21, 0, 0, tzinfo=UTC)  # 00:00 MSK of 2026-11-03


# =============================================================================
# parse_article_id / canonical_url
# =============================================================================

# Shared table (contract C5): Task 3 reuses these cases as a parity test.
ACCEPTED_URLS = [
    ('https://habr.com/ru/articles/123456/', 123456),
    ('https://habr.com/ru/articles/123456', 123456),
    ('https://habr.com/articles/123456/', 123456),
    ('https://habr.com/en/articles/123456/', 123456),
    ('https://habr.com/ru/post/123456/', 123456),
    ('https://habr.com/post/123456/', 123456),
    ('https://habr.com/ru/news/123456/', 123456),
    ('https://habr.com/ru/companies/yandex/articles/123456/', 123456),
    ('https://habr.com/ru/company/yandex/blog/123456/', 123456),
    ('https://habr.com/ru/specials/1100664/', 1100664),
    ('https://habr.com/ru/amp/publications/123456/', 123456),
    ('https://m.habr.com/ru/articles/123456/', 123456),
    ('https://www.habr.com/ru/articles/123456/', 123456),
    ('https://HABR.COM/ru/articles/123456/', 123456),
    ('http://habr.com/ru/articles/123456/', 123456),
    ('https://habr.com/ru/articles/123456/?utm_campaign=1&utm_source=habrahabr&utm_medium=rss', 123456),
    ('https://habr.com/ru/articles/123456/#comments', 123456),
    ('https://habr.com/ru/articles/123456/comments/', 123456),
    ('https://habr.com/ru/articles/123456/comments', 123456),
    ('https://habr.com/ru/companies/x/articles/123456/comments/#comment_1', 123456),
    ('  https://habr.com/ru/articles/123456/  ', 123456),
]

REJECTED_URLS = [
    'https://habr.com/ru/sandbox/123456/',
    'https://habr.com/ru/posts/123456/',
    'https://evilhabr.com/ru/articles/123456/',
    'https://habr.com.evil.com/ru/articles/123456/',
    'http://169.254.169.254/?habr.com',
    'http://169.254.169.254/ru/articles/123456/?u=habr.com',
    'https://habr.com@evil.com/ru/articles/123456/',
    'https://user:pass@habr.com/ru/articles/123456/',
    'https://user@habr.com/ru/articles/123456/',
    'https://habr.com:8080/ru/articles/123456/',
    'https://habr.com:443/ru/articles/123456/',
    'https://habr.com:/ru/articles/123456/',
    'https://habr.com\\@evil.com/ru/articles/123456/',
    'https://example.com/ru/articles/123456/',
    'https://habr.com./ru/articles/123456/',
    'ftp://habr.com/ru/articles/123456/',
    'javascript://habr.com/ru/articles/123456/',
    'habr.com/ru/articles/123456/',
    'https://habr.com/ru/articles/',
    'https://habr.com/ru/articles/abc/',
    'https://habr.com/ru/articles/0/',
    'https://habr.com/ru/articles/123456/extra/',
    'https://habr.com/ru/hubs/python/',
    'https://habr.com/ru/companies/yandex/',
    'https://habr.com/ru/articles/١٢٣/',
    'https://habr.com/xyz/ru/articles/123456/',
    '',
    None,
    123456,
]


@pytest.mark.parametrize('url,expected', ACCEPTED_URLS)
def test_parse_article_id_accepts_all_url_shapes(url, expected):
    assert habr_api.parse_article_id(url) == expected


@pytest.mark.parametrize('url', REJECTED_URLS)
def test_parse_article_id_rejects_spoofed_and_foreign(url):
    assert habr_api.parse_article_id(url) is None


def test_canonical_url_built_from_integer():
    assert habr_api.canonical_url(1102920) == 'https://habr.com/ru/articles/1102920/'
    for bad in ('1102920', '1/../../x', True, 0, -5, 1.5):
        with pytest.raises((TypeError, ValueError)):
            habr_api.canonical_url(bad)


# =============================================================================
# Normalization
# =============================================================================


def test_list_page_normalization(fake_habr, list_fixture, make_item):
    # Fixture page (real items) followed by a page whose last item crosses the boundary
    fixture_items = [list_fixture['publicationRefs'][i] for i in list_fixture['publicationIds']]
    since = datetime(2026, 11, 1, tzinfo=UTC)
    filler = descending(make_item, 1, datetime(2026, 11, 3, 8, 0, tzinfo=UTC), 100 - len(fixture_items))
    page1 = make_page(fixture_items + filler, 10)
    page2 = make_page(descending(make_item, 500, datetime(2026, 10, 31, tzinfo=UTC), 100), 10)
    fake_habr(by_page({1: page1, 2: page2}))

    items = {item['habr_id']: item for item in habr_api.fetch_articles_since(since)}

    regular = items[1073184]
    assert regular['url'] == 'https://habr.com/ru/articles/1073184/'
    assert regular['published_at'] == datetime(2026, 11, 4, 5, 30, 37, tzinfo=UTC)
    assert regular['published_at'].utcoffset() == timedelta(0)
    assert regular['post_type'] == 'article'
    assert regular['hubs'] == [('infosecurity', 'collective'), ('it-infrastructure', 'collective')]
    assert 'vm' in regular['tags']
    assert isinstance(regular['score'], int) and isinstance(regular['comments'], int)

    corporate = items[1102920]
    assert corporate['title'].startswith('GLOBIT TMS&YMS:')
    assert corporate['hubs'][0] == ('globit', 'corporative')
    assert ('transport', 'collective') in corporate['hubs']

    megaproject = items[1100664]
    assert megaproject['post_type'] == 'megaproject'
    assert megaproject['hubs'] == [('psb', 'corporative'), ('infosecurity', 'collective'), ('finance', 'collective')]

    assert ('DIY', 'collective') in items[1103386]['hubs']
    assert ('1C', 'collective') in items[1103242]['hubs']
    assert set(regular) == {
        'habr_id', 'title', 'url', 'published_at', 'score', 'comments', 'hubs', 'tags', 'lead_text', 'post_type',
    }


def test_lead_text_is_raw_html_and_title_unescaped(fake_habr, article_fixture):
    fake_habr(FakeResponse(200, article_fixture))

    item = habr_api.fetch_article(1102920)

    assert item['lead_text'] == article_fixture['leadData']['textHtml']
    assert '<abbr' in item['lead_text'] and '&lt;p&gt;' in item['lead_text']
    assert '&amp;' in article_fixture['titleHtml']
    assert item['title'] == 'GLOBIT TMS&YMS: Как мы решили проблему с «зоопарком» систем на складе крупного ретейлера «Глобус»'


def _set(path):
    def mutate(item, value):
        target = item
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


def _drop(path):
    def mutate(item, _value):
        target = item
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    return mutate


@pytest.mark.parametrize('mutate,value', [
    (_set(['id']), 1102920),
    (_set(['id']), 'abc'),
    (_set(['id']), '1102921'),            # differs from the publicationIds key
    (_set(['titleHtml']), None),
    (_drop(['titleHtml']), None),
    (_set(['timePublished']), 'yesterday'),
    (_set(['timePublished']), '2026-11-03T08:06:04'),  # naive
    (_set(['postType']), 5),
    (_set(['statistics', 'score']), '10'),
    (_set(['statistics', 'score']), True),
    (_set(['statistics', 'score']), 1.5),
    (_set(['statistics', 'commentsCount']), None),
    (_set(['statistics']), []),
    (_set(['hubs']), {'alias': 'python'}),
    (_set(['hubs']), [{'alias': 5, 'type': 'collective'}]),
    (_set(['hubs']), [{'alias': 'python'}]),
    (_set(['hubs']), ['python']),
    (_set(['hubs']), [{'alias': 'python', 'type': 'weird'}]),
    (_set(['tags']), [{'titleHtml': None}]),
    (_set(['tags']), 'tag'),
    (_set(['leadData']), None),
    (_drop(['leadData', 'textHtml']), None),
], ids=lambda v: repr(v)[:40] if not callable(v) else '')
def test_item_with_bad_field_type_rejected(fake_habr, list_fixture, make_item, mutate, value):
    def page_with(item):
        # The checked item is the page's last one and crosses the boundary, so a
        # valid item makes the attempt succeed: only the bad field can fail it
        page = make_page(descending(make_item, 1, datetime(2026, 11, 3, 12, tzinfo=UTC), 99), 1)
        page['publicationIds'].append('1102920')
        page['publicationRefs']['1102920'] = item
        return page

    item = copy.deepcopy(list_fixture['publicationRefs']['1102920'])
    item['timePublished'] = (SINCE - timedelta(hours=1)).isoformat()
    fake_habr(by_page({1: page_with(copy.deepcopy(item))}))
    assert len(habr_api.fetch_articles_since(SINCE)) == 99  # control: unmutated item passes
    fake_habr(FakeResponse(200, copy.deepcopy(item)))
    assert habr_api.fetch_article(1102920)['habr_id'] == 1102920

    mutate(item, value)
    fake_habr(by_page({1: page_with(item)}))
    with pytest.raises(habr_api.HabrUnavailable):
        habr_api.fetch_articles_since(SINCE)

    fake_habr(FakeResponse(200, item))
    with pytest.raises(habr_api.HabrUnavailable):
        habr_api.fetch_article(1102920)


# =============================================================================
# fetch_articles_since: pagination
# =============================================================================


def test_fetch_articles_since_stops_at_boundary(fake_habr, make_item):
    page1 = make_page(descending(make_item, 1000, datetime(2026, 11, 4, 12, tzinfo=UTC), 100), 10)
    # Page 2: 98 newer items, then the MSK day edge: 21:00:00Z included, 20:59:59Z excluded
    page2_items = descending(make_item, 2000, datetime(2026, 11, 3, 12, tzinfo=UTC), 98)
    page2_items.append(make_item(2098, SINCE))
    page2_items.append(make_item(2099, SINCE - timedelta(seconds=1)))
    page2 = make_page(page2_items, 10)
    page3 = make_page(descending(make_item, 3000, datetime(2026, 11, 2, tzinfo=UTC), 100), 10)
    fake = fake_habr(by_page({1: page1, 2: page2, 3: page3}))

    items = habr_api.fetch_articles_since(SINCE)

    assert fake.pages == [1, 2]
    ids = [item['habr_id'] for item in items]
    assert 2098 in ids and 2099 not in ids
    assert len(ids) == 199
    assert all(item['published_at'] >= SINCE for item in items)
    assert [item['published_at'] for item in items] == sorted((i['published_at'] for i in items), reverse=True)
    params = fake.log[0]['params']
    assert params == {'sort': 'rating', 'fl': 'ru', 'hl': 'ru', 'perPage': 100, 'page': 1}
    assert fake.log[0]['url'] == 'https://habr.com/kek/v2/articles/'


def test_fetch_articles_since_rejects_naive_boundary(fake_habr):
    fake = fake_habr(lambda url, params: pytest.fail('no request expected'))
    with pytest.raises(ValueError):
        habr_api.fetch_articles_since(datetime(2026, 11, 3))
    assert fake.log == []


def test_fetch_articles_since_dedups_across_pages(fake_habr, make_item):
    page1_items = descending(make_item, 1000, datetime(2026, 11, 4, 12, tzinfo=UTC), 100)
    # An insert at the top shifted the last item of page 1 onto page 2
    shifted = copy.deepcopy(page1_items[-1])
    page2_items = [shifted] + descending(make_item, 2000, datetime(2026, 11, 3, 10, tzinfo=UTC), 99)
    page2_items[-1]['timePublished'] = (SINCE - timedelta(hours=1)).isoformat()
    fake_habr(by_page({1: make_page(page1_items, 10), 2: make_page(page2_items, 10)}))

    items = habr_api.fetch_articles_since(SINCE)

    ids = [item['habr_id'] for item in items]
    assert ids.count(int(shifted['id'])) == 1
    assert len(ids) == len(set(ids)) == 100 + 98


def _error_envelope(code: int, error_code: str) -> dict:
    return {'httpCode': code, 'message': 'x', 'data': {}, 'errorCode': error_code}


def _partial_cases():
    """Each case: name -> builder(make_item) returning the pages dict."""
    newest = datetime(2026, 11, 4, 12, tzinfo=UTC)

    def full(make_item, start, count=100, pages_count=10, crossed=False):
        items = descending(make_item, start, newest, count)
        if crossed:
            items[-1]['timePublished'] = (SINCE - timedelta(hours=1)).isoformat()
        return make_page(items, pages_count)

    def missing(key):
        def build(make_item):
            page = full(make_item, 1000, crossed=True)
            del page[key]
            return {1: page}
        return build

    def id_absent(make_item):
        page = full(make_item, 1000, crossed=True)
        del page['publicationRefs'][page['publicationIds'][3]]
        return {1: page}

    def bad_item_on_page2(make_item):
        page2 = full(make_item, 2000, crossed=True)
        page2['publicationRefs'][page2['publicationIds'][5]]['statistics']['score'] = None
        return {1: full(make_item, 1000), 2: page2}

    def non_digit_id_key(make_item):
        page = full(make_item, 1000, crossed=True)
        key = page['publicationIds'][3]
        page['publicationIds'][3] = 'abc'
        page['publicationRefs']['abc'] = page['publicationRefs'].pop(key)
        return {1: page}

    def wrong_envelope_types(make_item):
        page = full(make_item, 1000, crossed=True)
        page['pagesCount'] = '10'
        return {1: page}

    def refs_not_dict(make_item):
        page = full(make_item, 1000, crossed=True)
        page['publicationRefs'] = list(page['publicationRefs'].values())
        return {1: page}

    def all_newer_pages(make_item, pages_count):
        return {n: full(make_item, n * 1000, pages_count=pages_count) for n in range(1, 21)}

    return {
        'http_503': lambda mi: {1: FakeResponse(503, 'Service Unavailable')},
        'http_503_on_page2': lambda mi: {1: full(mi, 1000), 2: FakeResponse(503, '')},
        'error_envelope_400': lambda mi: {1: full(mi, 1000), 2: FakeResponse(400, _error_envelope(400, 'PAGINATOR_LIMIT'))},
        'error_envelope_with_200': lambda mi: {1: FakeResponse(200, _error_envelope(400, 'PAGINATOR_LIMIT'))},
        'non_json': lambda mi: {1: FakeResponse(200, '<html>maintenance</html>')},
        'deeply_nested_json': lambda mi: {1: FakeResponse(200, '[' * 100_000 + ']' * 100_000)},
        'json_not_object': lambda mi: {1: FakeResponse(200, [1, 2, 3])},
        'missing_publicationIds': missing('publicationIds'),
        'missing_publicationRefs': missing('publicationRefs'),
        'missing_pagesCount': missing('pagesCount'),
        'wrong_envelope_types': wrong_envelope_types,
        'refs_not_dict': refs_not_dict,
        'id_absent_from_refs': id_absent,
        'non_digit_id_key': non_digit_id_key,
        'bad_item_on_page2': bad_item_on_page2,
        'short_page_before_last': lambda mi: {1: full(mi, 1000, count=50, crossed=True)},
        'empty_page_before_last': lambda mi: {1: make_page([], 10)},
        'boundary_not_crossed_before_pagesCount': lambda mi: all_newer_pages(mi, 2),
        'boundary_not_crossed_single_short_last_page': lambda mi: {1: full(mi, 1000, count=30, pages_count=1)},
        'item_cap_1000': lambda mi: all_newer_pages(mi, 20),
        'timeout': lambda mi: {1: requests.exceptions.Timeout('slow')},
        'connection_error': lambda mi: {1: full(mi, 1000), 2: requests.exceptions.ConnectionError('reset')},
        'redirect': lambda mi: {1: FakeResponse(302, '', {'Location': 'https://habr.com/ru/'})},
    }


PARTIAL_CASES = _partial_cases()


@pytest.mark.parametrize('case', list(PARTIAL_CASES))
def test_partial_response_conditions_raise_unavailable(fake_habr, make_item, case):
    fake = fake_habr(by_page(PARTIAL_CASES[case](make_item)))
    result = 'not set'

    with pytest.raises(habr_api.HabrUnavailable):
        result = habr_api.fetch_articles_since(SINCE)

    assert result == 'not set'  # no partial list leaks out
    if case == 'item_cap_1000':
        assert max(fake.pages) == 10  # never asks past the paginator cap


# =============================================================================
# fetch_article
# =============================================================================


@pytest.mark.parametrize('response,expected', [
    (FakeResponse(404, {'httpCode': 404, 'message': 'Resource not found', 'data': {}, 'errorCode': 'NOT_FOUND'}),
     habr_api.HabrNotFound),
    (FakeResponse(404, {'httpCode': 404, 'message': 'Не совпадает тип публикации', 'data': {'type': 'post'},
                        'errorCode': 'POST_TYPE_MISMATCH'}),
     habr_api.HabrNotFound),
    (FakeResponse(404, '<html>Not Found</html>'), habr_api.HabrUnavailable),
    (FakeResponse(404, _error_envelope(404, 'SOMETHING_ELSE')), habr_api.HabrUnavailable),
    (FakeResponse(403, _error_envelope(403, 'FORBIDDEN')), habr_api.HabrUnavailable),
    (FakeResponse(503, 'Service Unavailable'), habr_api.HabrUnavailable),
    (FakeResponse(200, 'not json'), habr_api.HabrUnavailable),
    (FakeResponse(200, '"a string"'), habr_api.HabrUnavailable),
    (requests.exceptions.Timeout('slow'), habr_api.HabrUnavailable),
    (requests.exceptions.ConnectionError('down'), habr_api.HabrUnavailable),
])
def test_fetch_article_not_found(fake_habr, response, expected):
    fake_habr(response)

    with pytest.raises(expected) as excinfo:
        habr_api.fetch_article(1103292)

    if expected is habr_api.HabrNotFound:
        assert not isinstance(excinfo.value, habr_api.HabrUnavailable)
    if isinstance(response, FakeResponse) and response.status_code != 200:
        assert excinfo.value.status == response.status_code


def test_fetch_article_normalizes_fixture(fake_habr, article_fixture):
    fake = fake_habr(FakeResponse(200, article_fixture))

    item = habr_api.fetch_article(1102920)

    assert fake.log[0]['url'] == 'https://habr.com/kek/v2/articles/1102920/'
    assert fake.log[0]['params'] == {'fl': 'ru', 'hl': 'ru'}
    assert item == {
        'habr_id': 1102920,
        'title': 'GLOBIT TMS&YMS: Как мы решили проблему с «зоопарком» систем на складе крупного ретейлера «Глобус»',
        'url': 'https://habr.com/ru/articles/1102920/',
        'published_at': datetime(2026, 11, 3, 8, 6, 4, tzinfo=UTC),
        'score': 10,
        'comments': 0,
        'hubs': [('globit', 'corporative'), ('transport', 'collective'), ('analysis_design', 'collective')],
        'tags': [t['titleHtml'] for t in article_fixture['tags']],
        'lead_text': article_fixture['leadData']['textHtml'],
        'post_type': 'article',
    }


def test_fetch_article_response_for_other_id_rejected(fake_habr, article_fixture):
    fake_habr(FakeResponse(200, article_fixture))
    with pytest.raises(habr_api.HabrUnavailable):
        habr_api.fetch_article(1)


@pytest.mark.parametrize('bad_id', ['1102920', '1/../hubs', True, 0, -1, None])
def test_fetch_article_rejects_non_integer_id(fake_habr, bad_id):
    fake = fake_habr(lambda url, params: pytest.fail('no request expected'))
    with pytest.raises((TypeError, ValueError)):
        habr_api.fetch_article(bad_id)
    assert fake.log == []


# =============================================================================
# Request policy
# =============================================================================


@pytest.mark.parametrize('status', [301, 302, 307, 308])
def test_redirect_is_not_followed(fake_habr, status):
    target = 'http://169.254.169.254/latest/meta-data/'
    fake = fake_habr(lambda url, params: FakeResponse(status, '', {'Location': target}))

    with pytest.raises(habr_api.HabrUnavailable) as excinfo:
        habr_api.fetch_article(1102920)

    assert excinfo.value.status == status
    assert excinfo.value.kind == 'redirect'
    assert len(fake.log) == 1
    assert fake.log[0]['allow_redirects'] is False
    assert all(target not in entry['url'] for entry in fake.log)


def test_oversized_body_rejected(fake_habr):
    big = FakeResponse(200, b'{"a":"' + b'x' * (habr_api.MAX_RESPONSE_BYTES + 1024 * 1024) + b'"}')
    fake_habr(big)

    with pytest.raises(habr_api.HabrUnavailable):
        habr_api.fetch_article(1102920)

    # The cap is enforced while reading: we stop right after crossing it
    assert big.bytes_served <= habr_api.MAX_RESPONSE_BYTES + 64 * 1024
    assert big.closed


def test_oversized_content_length_rejected_before_reading(fake_habr):
    response = FakeResponse(200, b'{}', {'Content-Length': str(habr_api.MAX_RESPONSE_BYTES + 1)})
    fake_habr(response)

    with pytest.raises(habr_api.HabrUnavailable):
        habr_api.fetch_article(1102920)

    assert response.bytes_served == 0


def test_body_at_cap_is_accepted(fake_habr, article_fixture):
    body = json.dumps(article_fixture).encode('utf-8')
    body += b' ' * (habr_api.MAX_RESPONSE_BYTES - len(body))
    fake_habr(FakeResponse(200, body))

    assert habr_api.fetch_article(1102920)['habr_id'] == 1102920


def test_request_uses_timeout_and_user_agent(fake_habr, article_fixture, hubs_fixture, make_item):
    page = make_page(descending(make_item, 1, datetime(2026, 11, 4, tzinfo=UTC), 100), 10)
    page['publicationRefs'][page['publicationIds'][-1]]['timePublished'] = (SINCE - timedelta(1)).isoformat()
    hubs_fixture['pagesCount'] = 1

    def handler(url, params):
        if url.endswith('/articles/'):
            return FakeResponse(200, page)
        if url.endswith('/hubs/'):
            return FakeResponse(200, hubs_fixture)
        return FakeResponse(200, article_fixture)
    fake = fake_habr(handler)

    habr_api.fetch_articles_since(SINCE)
    habr_api.fetch_article(1102920)
    habr_api.fetch_hub_catalog()

    assert len(fake.log) == 3
    for entry in fake.log:
        assert entry['timeout'] == (habr_api.HABR_API_TIMEOUT, habr_api.HABR_API_TIMEOUT) == (15, 15)
        assert entry['headers']['User-Agent'] == scraper.USER_AGENT
        assert entry['allow_redirects'] is False
        assert entry['stream'] is True
        assert entry['url'].startswith('https://habr.com/kek/v2/')


# =============================================================================
# fetch_hub_catalog
# =============================================================================


def _hub_page(hubs_fixture, aliases_refs: list[tuple[str, dict]], pages_count: int) -> dict:
    return {
        'pagesCount': pages_count,
        'hubIds': [alias for alias, _ in aliases_refs],
        'hubRefs': {alias: ref for alias, ref in aliases_refs},
    }


def _cloned_hubs(hubs_fixture, count: int) -> list[tuple[str, dict]]:
    template = hubs_fixture['hubRefs']['infosecurity']
    result = []
    for i in range(count):
        ref = copy.deepcopy(template)
        ref['alias'] = f'hub_{i}'
        ref['titleHtml'] = f'Hub &amp; {i}'
        result.append((ref['alias'], ref))
    return result


def test_fetch_hub_catalog_pages(fake_habr, hubs_fixture):
    real = [(alias, hubs_fixture['hubRefs'][alias]) for alias in hubs_fixture['hubIds']]
    pages = {1: _hub_page(hubs_fixture, _cloned_hubs(hubs_fixture, 100), 2), 2: _hub_page(hubs_fixture, real, 2)}
    fake = fake_habr(by_page(pages))

    catalog = habr_api.fetch_hub_catalog()

    assert fake.pages == [1, 2]
    assert fake.log[0]['url'] == 'https://habr.com/kek/v2/hubs/'
    assert fake.log[0]['params'] == {'perPage': 100, 'page': 1, 'fl': 'ru', 'hl': 'ru'}
    assert len(catalog) == 106
    by_alias = {hub['alias']: hub for hub in catalog}
    assert by_alias['hub_7'] == {'alias': 'hub_7', 'title': 'Hub & 7', 'rating': 1229.66, 'hub_type': 'collective'}
    assert by_alias['DIY']['title'] == 'DIY или Сделай сам'
    assert '1C' in by_alias
    assert by_alias['tensorflow']['rating'] == 0.0 and isinstance(by_alias['tensorflow']['rating'], float)
    assert by_alias['artificial_intelligence']['rating'] == 2253.2


def _hub_failures():
    def page2_missing(fx):
        return {1: _hub_page(fx, _cloned_hubs(fx, 100), 2),
                2: FakeResponse(404, {'httpCode': 404, 'message': 'Page is out of range', 'data': {},
                                      'errorCode': 'NOT_FOUND'})}

    def mutate_ref(change):
        def build(fx):
            refs = _cloned_hubs(fx, 3)
            change(refs[1][1])
            return {1: _hub_page(fx, refs, 1)}
        return build

    def short_first_page(fx):
        return {1: _hub_page(fx, _cloned_hubs(fx, 40), 2)}

    def ref_missing(fx):
        page = _hub_page(fx, _cloned_hubs(fx, 3), 1)
        del page['hubRefs']['hub_1']
        return {1: page}

    def missing_key(key):
        def build(fx):
            page = _hub_page(fx, _cloned_hubs(fx, 3), 1)
            del page[key]
            return {1: page}
        return build

    return {
        'page2_404': page2_missing,
        'page_503': lambda fx: {1: FakeResponse(503, '')},
        'non_json': lambda fx: {1: FakeResponse(200, '<html/>')},
        'missing_hubIds': missing_key('hubIds'),
        'missing_hubRefs': missing_key('hubRefs'),
        'missing_pagesCount': missing_key('pagesCount'),
        'ref_missing': ref_missing,
        'short_first_page': short_first_page,
        'empty_catalog': lambda fx: {1: _hub_page(fx, [], 1)},
        'rating_string': mutate_ref(lambda ref: ref['statistics'].update(rating='5')),
        'rating_bool': mutate_ref(lambda ref: ref['statistics'].update(rating=True)),
        'title_missing': mutate_ref(lambda ref: ref.pop('titleHtml')),
        'alias_mismatch': mutate_ref(lambda ref: ref.update(alias='other')),
        'unknown_hub_type': mutate_ref(lambda ref: ref.update(type='weird')),
        'timeout': lambda fx: {1: requests.exceptions.Timeout('slow')},
    }


HUB_FAILURES = _hub_failures()


@pytest.mark.parametrize('case', list(HUB_FAILURES))
def test_fetch_hub_catalog_failures_raise_unavailable(fake_habr, hubs_fixture, case):
    fake_habr(by_page(HUB_FAILURES[case](hubs_fixture)))

    with pytest.raises(habr_api.HabrUnavailable):
        habr_api.fetch_hub_catalog()


def test_hub_catalog_items_carry_hub_type(fake_habr, hubs_fixture):
    hubs_fixture['pagesCount'] = 1
    hubs_fixture['hubRefs']['DIY']['type'] = 'corporative'      # explicit API type is kept
    hubs_fixture['hubRefs']['1C']['type'] = 'collective'
    fake_habr(by_page({1: hubs_fixture}))

    catalog = {hub['alias']: hub for hub in habr_api.fetch_hub_catalog()}

    assert len(catalog) == len(hubs_fixture['hubIds'])  # not filtered
    assert catalog['DIY']['hub_type'] == 'corporative'
    assert catalog['1C']['hub_type'] == 'collective'
    # The live catalog has no per-hub type field: it lists collective hubs only
    assert 'type' not in hubs_fixture['hubRefs']['infosecurity']
    assert catalog['infosecurity']['hub_type'] == 'collective'


# =============================================================================
# Throttle and wait modes
# =============================================================================


def _ok_article(article_fixture):
    return lambda url, params: FakeResponse(200, article_fixture)


def test_throttle_spaces_requests_across_threads(monkeypatch, article_fixture):
    interval = 0.2
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', interval)
    spans: list[tuple[float, float]] = []
    in_flight = 0
    max_in_flight = 0
    counter_lock = threading.Lock()
    lock = SignallingLock()
    monkeypatch.setattr(habr_api, '_lock', lock)
    barrier = threading.Barrier(2)

    def slow_get(url, **kwargs):
        nonlocal in_flight, max_in_flight
        with counter_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        start = time.monotonic()
        # The first request stays in flight until the other thread blocks on the
        # throttle lock: contention is guaranteed, not just likely
        assert lock.contended.wait(5)
        time.sleep(0.05)
        end = time.monotonic()
        with counter_lock:
            in_flight -= 1
            spans.append((start, end))
        return FakeResponse(200, article_fixture)

    monkeypatch.setattr(habr_api.requests, 'get', slow_get)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(5)
            for _ in range(2):
                habr_api.fetch_article(1102920)
        except BaseException as e:  # surface in the main thread
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert not errors
    assert lock.contended.is_set()
    assert len(spans) == 4
    assert max_in_flight == 1
    spans.sort()
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        assert next_start - prev_end >= interval


def test_wait_timeout_raises_habr_busy(fake_habr, monkeypatch, article_fixture):
    fake = fake_habr(_ok_article(article_fixture))
    assert not issubclass(habr_api.HabrBusy, habr_api.HabrUnavailable)

    habr_api._lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(habr_api.HabrBusy) as excinfo:
            habr_api.fetch_article(1102920, wait_timeout=0.1)
        assert not isinstance(excinfo.value, habr_api.HabrUnavailable)
        assert 0.1 <= time.monotonic() - started < 2
        for call in (lambda: habr_api.fetch_articles_since(SINCE, wait_timeout=0.05),
                     lambda: habr_api.fetch_hub_catalog(wait_timeout=0.05)):
            with pytest.raises(habr_api.HabrBusy):
                call()
        assert fake.log == []

        # Background mode: waits as long as needed, succeeds after release
        results: list = []
        waiter = threading.Thread(target=lambda: results.append(habr_api.fetch_article(1102920, wait_timeout=None)))
        waiter.start()
        waiter.join(timeout=0.3)
        assert waiter.is_alive() and results == []
    finally:
        habr_api._lock.release()
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert results[0]['habr_id'] == 1102920


def test_handler_wait_timeout_constant(fake_habr, monkeypatch, article_fixture):
    assert habr_api.HANDLER_WAIT_TIMEOUT == 30
    monkeypatch.setattr(habr_api, 'HANDLER_WAIT_TIMEOUT', 0.05)
    fake_habr(_ok_article(article_fixture))

    habr_api._lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(habr_api.HabrBusy):
            habr_api.fetch_article(1102920, wait_timeout=habr_api.HANDLER_WAIT_TIMEOUT)
        assert time.monotonic() - started < 2
    finally:
        habr_api._lock.release()


# =============================================================================
# Comments (audit CA-2: scraper comments go through the same request policy)
# =============================================================================


def test_fetch_comments_through_request_policy(fake_habr, fixture_text):
    body = json.loads(fixture_text('habr_comments.json'))
    fake = fake_habr(lambda url, params: FakeResponse(200, body))

    comments = habr_api.fetch_comments(846432)

    assert comments == body['comments']
    entry, = fake.log
    assert entry['url'] == 'https://habr.com/kek/v2/articles/846432/comments/'
    assert entry['params'] == {'fl': 'ru', 'hl': 'ru'}
    assert entry['allow_redirects'] is False and entry['stream'] is True
    assert entry['headers']['User-Agent'] == scraper.USER_AGENT
    assert habr_api._last_request_at is not None        # throttle state updated


@pytest.mark.parametrize('body', [{}, {'comments': None}, {'comments': []}])
def test_fetch_comments_empty_variants(fake_habr, body):
    fake_habr(FakeResponse(200, body))
    assert habr_api.fetch_comments(1) == {}


@pytest.mark.parametrize('response, kind', [
    (FakeResponse(301, '', {'Location': 'http://169.254.169.254/'}), 'redirect'),
    (FakeResponse(200, b'x' * 10, {'Content-Length': str(8 * 1024 * 1024 + 1)}), 'too_large'),
    (FakeResponse(404, {'errorCode': 'NOT_FOUND'}), 'http'),
    (FakeResponse(503, ''), 'http'),
    (FakeResponse(200, 'not json'), 'invalid_json'),
    (FakeResponse(200, {'comments': 'bad'}), 'invalid_shape'),
])
def test_fetch_comments_failures_raise_unavailable(fake_habr, response, kind):
    fake = fake_habr(response)
    with pytest.raises(habr_api.HabrUnavailable) as excinfo:
        habr_api.fetch_comments(1)
    assert excinfo.value.kind == kind
    assert len(fake.log) == 1


def test_fetch_comments_own_size_cap(fake_habr, caplog):
    """Comments get 8 MB (busy discussions exceed the 2 MB article cap); over it: INFO, dropped."""
    assert habr_api.COMMENTS_MAX_RESPONSE_BYTES == 8 * 1024 * 1024
    filler = 'x' * (3 * 1024 * 1024)
    fake_habr(FakeResponse(200, {'comments': {'1': {'id': '1', 'message': filler}}}))
    assert habr_api.fetch_comments(1)['1']['message'] == filler        # > 2 MB is fine
    with pytest.raises(habr_api.HabrUnavailable):
        habr_api.fetch_article(1)                                      # the article cap stays 2 MB

    big = FakeResponse(200, b'{"comments": {"1": {"message": "' + b'x' * (9 * 1024 * 1024) + b'"}}}')
    fake_habr(big)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger='habr_api'):
        with pytest.raises(habr_api.HabrUnavailable) as excinfo:
            habr_api.fetch_comments(7)
    assert excinfo.value.kind == 'too_large'
    assert big.bytes_served <= habr_api.COMMENTS_MAX_RESPONSE_BYTES + 64 * 1024
    assert any(r.levelno == logging.INFO and 'comments dropped' in r.getMessage() and 'id=7' in r.getMessage()
               for r in caplog.records)


def test_fetch_comments_drops_malformed_entries(fake_habr):
    good = {'id': '1', 'parentId': None, 'message': 'ok'}
    fake_habr(FakeResponse(200, {'comments': {'1': good, '2': 'oops', '3': None, '4': [1], '5': 7}}))
    assert habr_api.fetch_comments(1) == {'1': good}


def test_fetch_comments_log_has_category_not_detail(monkeypatch, caplog):
    def broken(*a, **kw):
        raise habr_api.HabrUnavailable('invalid_shape', 200, 'SECRET-BODY')
    monkeypatch.setattr(habr_api, '_get_json_object', broken)
    with caplog.at_level(logging.INFO, logger='habr_api'):
        with pytest.raises(habr_api.HabrUnavailable):
            habr_api.fetch_comments(5)
    text = ' '.join(r.getMessage() for r in caplog.records)
    assert 'SECRET-BODY' not in text
    assert 'invalid_shape' in text and '200' in text and 'id=5' in text


def test_fetch_comments_rejects_bad_id(fake_habr):
    fake = fake_habr(FakeResponse(200, {}))
    for bad in (0, -1, '1', True):
        with pytest.raises((TypeError, ValueError)):
            habr_api.fetch_comments(bad)
    assert fake.log == []


def test_fetch_comments_waits_on_shared_throttle(fake_habr, monkeypatch):
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0.2)
    fake_habr(FakeResponse(200, {'comments': {}}))
    habr_api.fetch_comments(1)
    started = time.monotonic()
    habr_api.fetch_comments(1)
    assert time.monotonic() - started >= 0.15


# =============================================================================
# Handler deadline (one end-to-end budget per handler operation)
# =============================================================================


def test_handler_deadline_constant_and_wait_budget(monkeypatch):
    assert habr_api.HANDLER_DEADLINE == 45
    monkeypatch.setattr(habr_api, '_monotonic', lambda: 100.0)      # module seam, not time.monotonic
    assert habr_api.handler_deadline() == 145.0
    assert habr_api.wait_budget(None, None) is None              # background: unbounded
    assert habr_api.wait_budget(30, None) == 30
    assert habr_api.wait_budget(30, 110.0) == 10.0               # deadline is tighter
    assert habr_api.wait_budget(5, 110.0) == 5                   # wait_timeout is tighter
    assert habr_api.wait_budget(None, 103.0) == 3.0
    with pytest.raises(habr_api.HabrBusy):
        habr_api.wait_budget(30, 100.0)


def test_expired_deadline_raises_busy_without_request(fake_habr, article_fixture):
    fake = fake_habr(_ok_article(article_fixture))
    with pytest.raises(habr_api.HabrBusy):
        habr_api.fetch_article(1102920, wait_timeout=30, deadline=time.monotonic() - 1)
    assert fake.log == []
    assert not habr_api._lock.locked()


def test_deadline_bounds_lock_wait(fake_habr, article_fixture):
    fake = fake_habr(_ok_article(article_fixture))
    habr_api._lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(habr_api.HabrBusy):
            habr_api.fetch_article(1102920, wait_timeout=30, deadline=started + 0.2)
        assert 0.15 <= time.monotonic() - started < 2
    finally:
        habr_api._lock.release()
    assert fake.log == []


def test_deadline_cuts_request_timeout(fake_habr, monkeypatch, article_fixture):
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0)
    fake = fake_habr(_ok_article(article_fixture))
    habr_api.fetch_article(1102920, wait_timeout=30, deadline=time.monotonic() + 5)
    habr_api.fetch_article(1102920, wait_timeout=None)
    connect, read = fake.log[0]['timeout']
    assert 0 < connect <= 5 and 0 < read <= 5
    assert fake.log[1]['timeout'] == (habr_api.HABR_API_TIMEOUT, habr_api.HABR_API_TIMEOUT)


def test_slow_headers_then_slow_body_end_at_one_deadline(monkeypatch, article_fixture):
    """Headers that use most of the budget leave the body only the rest: the whole exchange
    ends at the absolute deadline, not at deadline + a fresh body window (audit CAR-1)."""
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0)
    body = json.dumps(article_fixture).encode()

    class SlowBody(FakeResponse):
        def iter_content(self, chunk_size=1):
            for i in range(0, len(body), 64):
                time.sleep(0.05)
                yield body[i:i + 64]

    def slow_headers(url, **kwargs):
        time.sleep(0.3)                         # headers take most of the 0.4 s budget
        return SlowBody(200, body)

    monkeypatch.setattr(habr_api.requests, 'get', slow_headers)
    started = time.monotonic()
    with pytest.raises(habr_api.HabrBusy):
        habr_api.fetch_article(1102920, wait_timeout=30, deadline=started + 0.4)
    elapsed = time.monotonic() - started
    assert elapsed < 0.4 + 0.2                  # one chunk past the deadline at most


def test_body_window_is_not_restarted_after_headers(monkeypatch, article_fixture):
    """Without a deadline the HABR_API_TIMEOUT window also covers headers + body together."""
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0)
    monkeypatch.setattr(habr_api, 'HABR_API_TIMEOUT', 0.4)
    body = json.dumps(article_fixture).encode()

    class SlowBody(FakeResponse):
        def iter_content(self, chunk_size=1):
            for i in range(0, len(body), 64):
                time.sleep(0.05)
                yield body[i:i + 64]

    def slow_headers(url, **kwargs):
        time.sleep(0.3)
        return SlowBody(200, body)

    monkeypatch.setattr(habr_api.requests, 'get', slow_headers)
    started = time.monotonic()
    with pytest.raises(habr_api.HabrUnavailable) as excinfo:
        habr_api.fetch_article(1102920)
    assert excinfo.value.kind == 'timeout'
    assert time.monotonic() - started < 0.4 + 0.2


def test_timeout_cut_by_deadline_is_busy_not_unavailable(fake_habr, monkeypatch):
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0)
    fake_habr(requests.exceptions.ReadTimeout('slow'))
    with pytest.raises(habr_api.HabrBusy):
        habr_api.fetch_article(1102920, wait_timeout=30, deadline=time.monotonic() + 5)
    # Full-length timeout (no deadline, or a deadline farther than HABR_API_TIMEOUT) stays HabrUnavailable
    for kwargs in ({}, {'deadline': time.monotonic() + habr_api.HABR_API_TIMEOUT + 60}):
        with pytest.raises(habr_api.HabrUnavailable) as excinfo:
            habr_api.fetch_article(1102920, wait_timeout=30, **kwargs)
        assert excinfo.value.kind == 'timeout'


def test_slow_body_cut_by_deadline_is_busy(fake_habr, monkeypatch, article_fixture):
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0)

    class DripResponse(FakeResponse):
        def iter_content(self, chunk_size=1):
            for part in (b'{', b'"a"', b':1}'):
                time.sleep(0.15)
                yield part

    fake_habr(DripResponse(200))
    started = time.monotonic()
    with pytest.raises(habr_api.HabrBusy):
        habr_api.fetch_article(1102920, wait_timeout=30, deadline=started + 0.2)
    assert time.monotonic() - started < 2


def test_multi_page_catalog_bounded_by_one_deadline(fake_habr, monkeypatch, hubs_fixture):
    """Slow-but-answering Habr: a 5-page handler-mode catalog fetch gives up at the deadline."""
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0)
    pages = {n: _hub_page(hubs_fixture, _cloned_hubs(hubs_fixture, 100), 5) for n in range(1, 6)}
    route = by_page(pages)

    def slow(url, params):
        time.sleep(0.15)
        return route(url, params)

    fake = fake_habr(slow)
    started = time.monotonic()
    with pytest.raises(habr_api.HabrBusy):
        habr_api.fetch_hub_catalog(wait_timeout=30, deadline=started + 0.4)
    assert time.monotonic() - started < 1.5
    assert 1 <= len(fake.log) < 5
    assert not habr_api._lock.locked()


def test_multi_page_list_bounded_by_one_deadline(fake_habr, monkeypatch, make_item):
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0)
    newest = datetime(2026, 11, 4, 12, tzinfo=UTC)

    def slow(url, params):
        time.sleep(0.15)
        page = int(params['page'])
        refs = {str(page * 1000 + i): make_item(page * 1000 + i, newest - timedelta(minutes=page))
                for i in range(100)}
        return FakeResponse(200, {'pagesCount': 10, 'publicationIds': list(refs), 'publicationRefs': refs})

    fake = fake_habr(slow)
    started = time.monotonic()
    with pytest.raises(habr_api.HabrBusy):
        habr_api.fetch_articles_since(newest - timedelta(days=1), wait_timeout=30, deadline=started + 0.4)
    assert time.monotonic() - started < 1.5
    assert 1 <= len(fake.log) < 10


def test_lock_released_after_failure(fake_habr):
    fake_habr(requests.exceptions.ConnectionError('down'))
    with pytest.raises(habr_api.HabrUnavailable):
        habr_api.fetch_article(1102920)
    assert not habr_api._lock.locked()
    assert habr_api._last_request_at is not None
