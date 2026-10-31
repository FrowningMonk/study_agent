import json

import pytest
import requests
from bs4 import BeautifulSoup

import scraper

CHALLENGE_HTML = (
    '<html><body><script>document.cookie="RCPC=abc123; path=/";'
    'var u="{url}";location.replace(u);</script></body></html>'
)


# =============================================================================
# Fakes
# =============================================================================


class FakeResponse:
    def __init__(self, text: str, url: str, status: int = 200, encoding: str = 'utf-8'):
        self.text = text
        self.url = url
        self.status_code = status
        self.encoding = encoding
        self.apparent_encoding = 'windows-1251'

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Returns queued responses in order and records requested URLs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requested: list[str] = []
        self.headers: dict = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, timeout=None):
        self.requested.append(url)
        return self._responses.pop(0)


@pytest.fixture
def fake_session(monkeypatch):
    def _install(*responses):
        session = FakeSession(responses)
        monkeypatch.setattr(scraper.requests, 'Session', lambda: session)
        return session
    return _install


# =============================================================================
# _parse_cookie_challenge
# =============================================================================


def test_parse_cookie_challenge_extracts_cookie_and_url():
    html = CHALLENGE_HTML.format(url='https://infostart.ru/public/1/?r=1')

    assert scraper._parse_cookie_challenge(html) == ('RCPC', 'abc123', 'https://infostart.ru/public/1/?r=1')


def test_parse_cookie_challenge_ignores_regular_page():
    assert scraper._parse_cookie_challenge('<html><h1>Статья</h1></html>') is None


def test_parse_cookie_challenge_ignores_large_page_with_cookie_script():
    html = CHALLENGE_HTML.format(url='/x') + 'x' * scraper._CHALLENGE_MAX_BODY

    assert scraper._parse_cookie_challenge(html) is None


# =============================================================================
# _fetch_page: cookie challenge
# =============================================================================


def test_fetch_page_passes_challenge_with_relative_retry_url(fake_session):
    session = fake_session(
        FakeResponse(CHALLENGE_HTML.format(url='/public/1/?r=1'), 'https://infostart.ru/public/1/'),
        FakeResponse('<h1>OK</h1>', 'https://infostart.ru/public/1/?r=1'),
    )

    soup, error = scraper._fetch_page('https://infostart.ru/public/1/')

    assert error is None
    assert soup.h1.get_text() == 'OK'
    assert session.requested[1] == 'https://infostart.ru/public/1/?r=1'
    assert session.cookies.get('RCPC') == 'abc123'


def test_fetch_page_refuses_challenge_redirect_to_foreign_host(fake_session):
    session = fake_session(
        FakeResponse(CHALLENGE_HTML.format(url='http://169.254.169.254/latest/'), 'https://infostart.ru/public/1/'),
    )

    soup, error = scraper._fetch_page('https://infostart.ru/public/1/')

    assert soup is None
    assert 'посторонний' in error['error']
    assert session.requested == ['https://infostart.ru/public/1/']


def test_fetch_page_reports_challenge_that_repeats(fake_session):
    challenge = CHALLENGE_HTML.format(url='/public/1/')
    fake_session(
        FakeResponse(challenge, 'https://infostart.ru/public/1/'),
        FakeResponse(challenge, 'https://infostart.ru/public/1/'),
    )

    soup, error = scraper._fetch_page('https://infostart.ru/public/1/')

    assert soup is None
    assert 'cookie challenge' in error['error']


def test_fetch_page_fixes_default_latin1_encoding(fake_session):
    response = FakeResponse('<h1>x</h1>', 'https://infostart.ru/public/1/', encoding='ISO-8859-1')
    fake_session(response)

    scraper._fetch_page('https://infostart.ru/public/1/')

    assert response.encoding == 'windows-1251'


# =============================================================================
# Habr comments
# =============================================================================


@pytest.fixture
def habr_api(monkeypatch, fixture_text):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse(fixture_text('habr_comments.json'), url)

    monkeypatch.setattr(scraper.requests, 'get', fake_get)
    return calls


def test_habr_comments_tree_order_numbering_and_filtering(habr_api):
    comments = scraper._fetch_habr_comments('https://habr.com/ru/companies/x/articles/846432/')

    assert habr_api == [scraper.HABR_COMMENTS_API.format(article_id='846432')]
    # DFS order: 100(#1) → 101(#2) → 102(#3, moderated) → 103(#4); 200 suspended, 300 empty
    assert [(c['number'], c['author'], c['reply_to']) for c in comments] == [
        (1, 'alice', None),
        (2, 'author', 1),
        (4, 'аноним', 1),
    ]
    assert comments[0]['text'] == 'Отличная статья, но retry стоит ограничить.'
    assert [c['is_post_author'] for c in comments] == [False, True, False]
    assert [c['score'] for c in comments] == [5, 3, -2]


def test_habr_comments_survive_cycle_in_tree(monkeypatch):
    data = {'comments': {
        '1': {'id': '1', 'parentId': None, 'status': 'published', 'children': ['2'], 'message': 'a'},
        '2': {'id': '2', 'parentId': '1', 'status': 'published', 'children': ['1'], 'message': 'b'},
    }}
    monkeypatch.setattr(scraper.requests, 'get',
                        lambda *a, **kw: FakeResponse(json.dumps(data), 'https://habr.com'))

    comments = scraper._fetch_habr_comments('https://habr.com/ru/articles/1/')

    assert [c['number'] for c in comments] == [1, 2]


def test_habr_comments_api_failure_returns_empty(monkeypatch):
    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError('down')
    monkeypatch.setattr(scraper.requests, 'get', boom)

    assert scraper._fetch_habr_comments('https://habr.com/ru/articles/1/') == []


def test_habr_comments_url_without_article_id_skips_request(monkeypatch):
    monkeypatch.setattr(scraper.requests, 'get', lambda *a, **kw: pytest.fail('unexpected request'))

    assert scraper._fetch_habr_comments('https://habr.com/ru/feed/') == []


# =============================================================================
# InfoStart
# =============================================================================


@pytest.fixture
def infostart_soup(fixture_text):
    return BeautifulSoup(fixture_text('infostart_article.html'), 'html.parser')


def test_infostart_author_name_and_profile_id(infostart_soup):
    assert scraper._extract_infostart_author(infostart_soup) == ('Дмитрий Сидоренко (dsdred)', '295466')


def test_infostart_author_missing():
    assert scraper._extract_infostart_author(BeautifulSoup('<div></div>', 'html.parser')) == ('Не найден', None)


def test_infostart_tags_skip_empty(infostart_soup):
    assert scraper._extract_infostart_tags(infostart_soup) == ['HTTP-сервис', 'JSON']


def test_infostart_comments_use_site_numbers_and_skip_best_block(infostart_soup):
    comments = scraper._extract_infostart_comments(infostart_soup, '295466')

    # Best block duplicate of #2 is skipped; #3 has no text (image only)
    assert [(c['number'], c['author'], c['reply_to'], c['is_post_author']) for c in comments] == [
        (1, 'reader', None, False),
        (2, 'dsdred', 1, True),
        (4, 'critic', 2, False),
    ]
    assert comments[1]['text'] == 'Не за что ;))'
    assert comments[2]['text'] == 'А почему не через OData?'


def test_infostart_content_strips_service_blocks(infostart_soup):
    content = scraper._extract_infostart_content(infostart_soup)

    assert 'Теперь добавим методы Post и Put.' in content
    assert 'Продолжение статьи' in content
    for junk in ('Файлы из Базы знаний', 'Подписка PRO', 'HTTP-сервис', '194', 'телеграмм-группу'):
        assert junk not in content


def test_infostart_content_keeps_article_wrapped_in_tab_content():
    html = (
        '<div class="kurs-spoiler"><div class="tab-content">'
        '<div class="detail-text"><p>Текст статьи внутри вкладки</p></div>'
        '<div class="tab-file-wrap">Файлы</div>'
        '</div></div>'
    )

    content = scraper._extract_infostart_content(BeautifulSoup(html, 'html.parser'))

    assert 'Текст статьи внутри вкладки' in content


def test_parse_infostart_end_to_end(monkeypatch, infostart_soup):
    monkeypatch.setattr(scraper, '_fetch_page', lambda url: (infostart_soup, None))

    result = scraper._parse_infostart('https://infostart.ru/public/886103/')

    assert result['title'] == 'HTTP Сервисы: Путь к своему сервису. Часть 3'
    assert result['author'] == 'Дмитрий Сидоренко (dsdred)'
    assert result['tags'] == ['HTTP-сервис', 'JSON']
    assert [c['number'] for c in result['comments']] == [1, 2, 4]
    assert 'Не за что' not in result['content']
    assert result['content_length'] == len(result['content'])


# =============================================================================
# GitHub
# =============================================================================


def test_parse_github_extracts_activity_metadata(monkeypatch):
    api = {
        'description': 'Fast tool', 'stargazers_count': 1500, 'language': 'Rust',
        'pushed_at': '2026-10-30T12:34:56Z', 'archived': True, 'license': {'spdx_id': 'Apache-2.0'},
    }
    monkeypatch.setattr(scraper, '_fetch_page', lambda url: (BeautifulSoup('', 'html.parser'), None))
    monkeypatch.setattr(scraper, '_fetch_github_api', lambda url: api)
    monkeypatch.setattr(scraper, '_find_markdown_files', lambda owner, repo: [])
    monkeypatch.setattr(scraper, '_extract_github_readme', lambda soup: 'readme')

    result = scraper._parse_github('https://github.com/o/r')

    assert (result['pushed_at'], result['archived'], result['license']) == ('2026-10-30', True, 'Apache-2.0')
    assert result['stars'] == '1500'


def test_parse_github_without_api_leaves_activity_unknown(monkeypatch):
    monkeypatch.setattr(scraper, '_fetch_page', lambda url: (BeautifulSoup('', 'html.parser'), None))
    monkeypatch.setattr(scraper, '_fetch_github_api', lambda url: None)
    monkeypatch.setattr(scraper, '_find_markdown_files', lambda owner, repo: [])
    monkeypatch.setattr(scraper, '_extract_github_readme', lambda soup: 'readme')

    result = scraper._parse_github('https://github.com/o/r')

    assert (result['pushed_at'], result['archived'], result['license']) == (None, None, None)
