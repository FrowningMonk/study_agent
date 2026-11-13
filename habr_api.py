"""
Client for the public (undocumented) Habr kek API: https://habr.com/kek/v2/...

The single source of Habr data and Habr article identity for the bot:
    - parse_article_id(url) / canonical_url(habr_id) — strict URL <-> id mapping;
    - fetch_rss / fetch_rss_since — light items from the public RSS feeds (hubs, site top);
    - fetch_articles_since(since, until, hubs) — RSS candidates enriched by fetch_article;
    - fetch_article(habr_id) — one article by id;
    - fetch_hub_catalog() — the whole hub catalog;
    - fetch_comments(habr_id) — the raw comment tree of one article (used by scraper).

Normalized article item (list and by-id share the shape):
    {habr_id: int, title: str, url: str, published_at: datetime (aware, UTC),
     score: int, comments: int, hubs: [(alias, hub_type)], tags: [str],
     lead_text: str, post_type: str}
    - title is the HTML-unescaped plain string from `titleHtml`;
    - lead_text is the RAW `leadData.textHtml` (HTML, unchanged). Callers pass
      (title, lead_text) to `summarizer.generate_short_description`, which strips
      HTML itself, and sanitize the lead for display via `feed` helpers;
    - url is always the canonical form built from the integer id;
    - hubs carry their type: an alias may exist both as a collective hub and as a
      company (corporative) hub (e.g. `bitrix`), so match on (alias, type).
    Items with a wrong field type are rejected (the whole attempt fails), never coerced.

Hub catalog item: {alias, title (unescaped), rating: float, hub_type}. The live
catalog has no per-hub type field and lists collective hubs only, so hub_type is
'collective' unless the API states a type explicitly.

Errors:
    - HabrNotFound — the article is permanently unavailable: 404 with errorCode
      NOT_FOUND / POST_TYPE_MISMATCH, or 403 IN_DRAFTS (hidden by the author into drafts;
      live envelope 2026-11-05: {"httpCode":403,"message":"post drafted",...,
      "errorCode":"IN_DRAFTS"}). Any other 403/404 stays HabrUnavailable;
    - HabrUnavailable — transient / partial / invalid response (`kind`, `status`
      attributes give a short failure category; never show raw bodies to users);
    - HabrBusy — the throttle lock was not acquired within `wait_timeout`.
      NOT a HabrUnavailable subclass. Handlers answer
      "Хабр сейчас отвечает медленно, попробуй позже".

Request policy (one function, `_request`): a process-wide lock, at least
MIN_REQUEST_INTERVAL seconds between requests, HABR_API_TIMEOUT, no redirects,
MAX_RESPONSE_BYTES cap enforced while reading, scraper.USER_AGENT.
Every public fetch takes `wait_timeout`: None (background callers) waits for the
lock indefinitely; handlers pass HANDLER_WAIT_TIMEOUT.

Handler mode also takes `deadline` (a time.monotonic() value from handler_deadline(),
HANDLER_DEADLINE seconds ahead): one end-to-end budget for a whole handler operation,
covering the caller's own single-flight lock wait (via wait_budget) plus every page's
lock wait, request timeout and body read. When it runs out the call raises HabrBusy
(the same "Хабр сейчас отвечает медленно" answer as a lock timeout). Background
callers (scheduler, backfill, the Саммари worker) pass no deadline.

Example:
    >>> import habr_api
    >>> habr_api.parse_article_id('https://habr.com/ru/companies/x/articles/123/?utm_source=a')
    123
"""

import html
import json
import logging
import re
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from scraper import USER_AGENT

logger = logging.getLogger(__name__)

__all__ = [
    'HabrNotFound', 'HabrUnavailable', 'HabrBusy',
    'parse_article_id', 'canonical_url',
    'fetch_articles_since', 'fetch_rss_since', 'fetch_rss', 'hub_feed_path',
    'fetch_article', 'fetch_hub_catalog',
    'fetch_comments', 'handler_deadline', 'wait_budget',
    'HABR_API_TIMEOUT', 'MIN_REQUEST_INTERVAL', 'HANDLER_WAIT_TIMEOUT', 'HANDLER_DEADLINE',
    'MAX_RESPONSE_BYTES',
]

# Constants (patchable in tests)
API_BASE: str = 'https://habr.com/kek/v2/'
HABR_API_TIMEOUT: float = 15
MIN_REQUEST_INTERVAL: float = 1.5
HANDLER_WAIT_TIMEOUT: float = 30
HANDLER_DEADLINE: float = 45        # end-to-end budget of one handler-mode operation
MAX_RESPONSE_BYTES: int = 2 * 1024 * 1024
# Comment trees of the most discussed articles exceed 2 MB (HTML message + author per comment)
COMMENTS_MAX_RESPONSE_BYTES: int = 8 * 1024 * 1024
# Article lists come from the public RSS feeds: since 2026-11 the kek list endpoint
# (`articles/?sort=...`) answers 400 "Bad Request Url" to every non-browser client
RSS_BASE: str = 'https://habr.com/ru/rss/'
RSS_LIMIT: int = 100                # feed max; without `limit` a feed returns 40 items
# Site-wide top feeds: the best articles of a past day are in the weekly top, the
# daily top covers the last 24 h. The plain "all articles" feed spans only ~6 hours.
RSS_TOP_FEEDS: tuple[str, ...] = ('articles/top/daily/', 'articles/top/weekly/')
# Per-article metadata failures tolerated by fetch_articles_since before the attempt fails
ARTICLE_FAILURES_MIN: int = 3
ARTICLE_FAILURES_SHARE: float = 0.1
# A hub feed answering these is skipped (hub renamed/removed), not a failed attempt
HUB_GONE_STATUSES = frozenset({404, 410})
HUBS_PER_PAGE: int = 100
HUBS_MAX_PAGES: int = 20            # ~5 pages live; a sanity bound against a runaway loop
# Small chunks: urllib3 blocks until a whole chunk arrives, so this bounds how long
# a slow-drip body can hold the lock between whole-body deadline checks
_READ_CHUNK: int = 8 * 1024

NOT_FOUND_CODES = frozenset({'NOT_FOUND', 'POST_TYPE_MISMATCH'})
# (status, errorCode) pairs of fetch_article that mean "this article is not available"
_GONE_BY_STATUS = {404: NOT_FOUND_CODES, 403: frozenset({'IN_DRAFTS'})}
HUB_TYPES = frozenset({'collective', 'corporative'})

_ALLOWED_HOSTS = frozenset({'habr.com', 'www.habr.com', 'm.habr.com'})
# Path after an optional two-letter language prefix; /sandbox/ and /posts/ never match
_ARTICLE_PATH_RE = re.compile(
    r'(?:/[a-z]{2})?'
    r'/(?:articles|post|news|specials|amp/publications'
    r'|companies/[A-Za-z0-9_.-]+/articles|company/[A-Za-z0-9_.-]+/blog)'
    r'/([0-9]{1,12})'
    r'(?:/(?:comments/?)?)?'
)
_DIGITS_RE = re.compile(r'[0-9]{1,12}')
_SAFE_CODE_RE = re.compile(r'[A-Z][A-Z0-9_]{0,39}')

# Throttle state: one per process, shared by handlers, digest and backfill threads
_lock = threading.Lock()
_last_request_at: float | None = None   # time.monotonic() of the last request end
# Deadline clock seam (patched in tests instead of the process-wide time.monotonic)
_monotonic = time.monotonic


# =============================================================================
# Exceptions
# =============================================================================


class HabrNotFound(Exception):
    """The article does not exist / is not available: kek 404 NOT_FOUND, POST_TYPE_MISMATCH,
    or 403 IN_DRAFTS (hidden by the author)."""

    def __init__(self, error_code: str, status: int = 404):
        super().__init__(f'Habr article not found ({error_code})')
        self.error_code = error_code
        self.status = status


class HabrUnavailable(Exception):
    """Transient, partial or invalid kek response.

    Attributes:
        kind: short category — 'timeout', 'network', 'http', 'redirect',
            'too_large', 'invalid_json', 'invalid_shape', 'partial'.
        status: HTTP status when one was received, else None.
        error_code: kek `errorCode` of a 4xx error envelope (fetch_article), only when it
            is a short upper-case token (log-safe, never body text), else None.
    """

    def __init__(self, kind: str, status: int | None = None, detail: str = '',
                 error_code: str | None = None):
        message = f'Habr unavailable: {kind}' + (f' (HTTP {status})' if status is not None else '')
        if detail:
            message += f': {detail}'
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.error_code = error_code if isinstance(error_code, str) and _SAFE_CODE_RE.fullmatch(error_code) else None


class HabrBusy(Exception):
    """The Habr throttle lock was not acquired within `wait_timeout`. Not a HabrUnavailable."""


# =============================================================================
# URL <-> id
# =============================================================================


def parse_article_id(url: Any) -> int | None:
    """Return the Habr article id for a Habr article URL, else None.

    Accepts only scheme http(s), host exactly habr.com / www.habr.com / m.habr.com,
    no userinfo, no port. Paths: /articles/N, /post/N, /news/N, /specials/N,
    /amp/publications/N, /companies/x/articles/N, /company/x/blog/N with an optional
    language prefix and an optional /comments/ suffix; any query/fragment.
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ('http', 'https'):
        return None
    # Raw netloc checks: userinfo ('@'), any port syntax (':'), backslash tricks
    netloc = parts.netloc
    if not netloc or '@' in netloc or ':' in netloc or '\\' in netloc or port is not None:
        return None
    if (parts.hostname or '').lower() not in _ALLOWED_HOSTS:
        return None
    match = _ARTICLE_PATH_RE.fullmatch(parts.path)
    if not match:
        return None
    habr_id = int(match.group(1))
    return habr_id if habr_id > 0 else None


def _check_id(habr_id: Any) -> int:
    if isinstance(habr_id, bool) or not isinstance(habr_id, int):
        raise TypeError(f'habr_id must be int, got {type(habr_id).__name__}')
    if habr_id <= 0:
        raise ValueError('habr_id must be positive')
    return habr_id


def canonical_url(habr_id: int) -> str:
    """Canonical article URL built from the integer id only (Habr redirects it to company/news forms)."""
    return f'https://habr.com/ru/articles/{_check_id(habr_id)}/'


# =============================================================================
# Request core
# =============================================================================


def handler_deadline() -> float:
    """A fresh handler-mode deadline: time.monotonic() + HANDLER_DEADLINE."""
    return _monotonic() + HANDLER_DEADLINE


def _time_left(deadline: float) -> float:
    left = deadline - _monotonic()
    if left <= 0:
        logger.warning('Habr handler deadline exceeded')
        raise HabrBusy('Habr handler deadline exceeded')
    return left


def wait_budget(wait_timeout: float | None, deadline: float | None) -> float | None:
    """Lock wait bounded by `wait_timeout` and by what is left of `deadline` (None = unbounded).

    Raises HabrBusy when the deadline has already passed.
    """
    if deadline is None:
        return wait_timeout
    left = _time_left(deadline)
    return left if wait_timeout is None else min(wait_timeout, left)


def _acquire(wait_timeout: float | None) -> None:
    acquired = _lock.acquire() if wait_timeout is None else _lock.acquire(timeout=max(0.0, wait_timeout))
    if not acquired:
        logger.warning('Habr throttle busy: lock not acquired in %.1fs', wait_timeout)
        raise HabrBusy(f'Habr throttle lock not acquired in {wait_timeout}s')


def _wait_interval() -> None:
    if _last_request_at is None:
        return
    target = _last_request_at + MIN_REQUEST_INTERVAL
    while (remaining := target - time.monotonic()) > 0:
        time.sleep(remaining)


def _read_capped(response, end: float, max_bytes: int) -> bytes:
    """Body up to `max_bytes`, read until the absolute `end` (a _monotonic() value)."""
    length = response.headers.get('Content-Length')
    if length is not None and length.isdigit() and int(length) > max_bytes:
        raise HabrUnavailable('too_large', response.status_code)
    body = bytearray()
    for chunk in response.iter_content(chunk_size=_READ_CHUNK):
        body += chunk
        if len(body) > max_bytes:
            raise HabrUnavailable('too_large', response.status_code)
        # requests' timeout is per socket read; bound the whole exchange too
        if _monotonic() > end:
            raise HabrUnavailable('timeout', response.status_code, 'body read too slow')
    return bytes(body)


def _request(path: str, params: dict, wait_timeout: float | None,
             deadline: float | None = None,
             max_bytes: int | None = None, base: str | None = None) -> tuple[int, bytes]:
    """The only place that talks to Habr. Returns (status, body); 3xx is a failure.

    The lock is held only around the HTTP exchange itself. One absolute end time per
    exchange: now + HABR_API_TIMEOUT, or the handler `deadline` if that is earlier. The
    connect/read timeouts are what is left until it, and the body read stops at it
    (checked between chunks, so a single stalled socket read can still take up to the
    read timeout). A timeout caused by the deadline is HabrBusy, not HabrUnavailable.
    `max_bytes` overrides MAX_RESPONSE_BYTES for one call; `base` overrides API_BASE
    (RSS_BASE for the RSS feeds).
    """
    global _last_request_at
    url = (API_BASE if base is None else base) + path
    max_bytes = MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
    _acquire(wait_budget(wait_timeout, deadline))
    try:
        _wait_interval()
        if deadline is not None:
            _time_left(deadline)                     # HabrBusy if the interval wait used it up
        start = _monotonic()
        own_end = start + HABR_API_TIMEOUT
        end = own_end if deadline is None else min(own_end, deadline)
        cut_by_deadline = end < own_end
        timeout = max(end - start, 0.001)            # connect and per-read socket timeout
        try:
            response = requests.get(
                url,
                params=params,
                headers={'User-Agent': USER_AGENT},
                timeout=(timeout, timeout),
                allow_redirects=False,
                stream=True,
            )
            try:
                status = response.status_code
                if 300 <= status < 400:
                    raise HabrUnavailable('redirect', status)
                body = _read_capped(response, end, max_bytes)
            finally:
                response.close()
        except requests.exceptions.Timeout as e:
            if cut_by_deadline:
                raise HabrBusy('Habr handler deadline exceeded during the request') from e
            raise HabrUnavailable('timeout') from e
        except HabrUnavailable as e:
            if cut_by_deadline and e.kind == 'timeout':
                raise HabrBusy('Habr handler deadline exceeded during the body read') from e
            raise
        except requests.exceptions.RequestException as e:
            raise HabrUnavailable('network', detail=type(e).__name__) from e
        finally:
            _last_request_at = time.monotonic()
    finally:
        _lock.release()
    return status, body


def _decode_json(status: int, body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as e:   # JSONDecodeError, UnicodeDecodeError, deep nesting
        raise HabrUnavailable('invalid_json', status) from e


def _get_json_object(path: str, params: dict, wait_timeout: float | None,
                     deadline: float | None = None, max_bytes: int | None = None) -> dict:
    """200 + JSON object, else HabrUnavailable."""
    status, body = _request(path, params, wait_timeout, deadline, max_bytes)
    if status != 200:
        raise HabrUnavailable('http', status)
    data = _decode_json(status, body)
    if not isinstance(data, dict):
        raise HabrUnavailable('invalid_shape', status, 'response is not an object')
    return data


# =============================================================================
# Shape validation
# =============================================================================


def _invalid(detail: str) -> HabrUnavailable:
    return HabrUnavailable('invalid_shape', detail=detail)


def _field(obj: Any, key: str, kind: type | tuple[type, ...]) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise _invalid(f'missing {key}')
    value = obj[key]
    # bool is an int subclass: never accept it for numeric fields
    if isinstance(value, bool) and bool not in (kind if isinstance(kind, tuple) else (kind,)):
        raise _invalid(f'bad type of {key}')
    if not isinstance(value, kind):
        raise _invalid(f'bad type of {key}')
    return value


def _parse_published(value: str) -> datetime:
    try:
        published = datetime.fromisoformat(value)
    except ValueError as e:
        raise _invalid('bad timePublished') from e
    if published.tzinfo is None or published.utcoffset() is None:
        raise _invalid('naive timePublished')
    return published.astimezone(timezone.utc)


def _normalize_item(raw: Any, expected_id: int | None = None) -> dict:
    """Validate one kek publication and return the normalized item."""
    raw_id = _field(raw, 'id', str)
    if not _DIGITS_RE.fullmatch(raw_id) or int(raw_id) <= 0:
        raise _invalid('bad id')
    habr_id = int(raw_id)
    if expected_id is not None and habr_id != expected_id:
        raise _invalid('id mismatch')

    statistics = _field(raw, 'statistics', dict)
    hubs = []
    for hub in _field(raw, 'hubs', list):
        hub_type = _field(hub, 'type', str)
        if hub_type not in HUB_TYPES:
            raise _invalid('bad hub type')
        hubs.append((_field(hub, 'alias', str), hub_type))
    tags = [html.unescape(_field(tag, 'titleHtml', str)) for tag in _field(raw, 'tags', list)]
    lead = _field(raw, 'leadData', dict)

    return {
        'habr_id': habr_id,
        'title': html.unescape(_field(raw, 'titleHtml', str)),
        'url': canonical_url(habr_id),
        'published_at': _parse_published(_field(raw, 'timePublished', str)),
        'score': _field(statistics, 'score', int),
        'comments': _field(statistics, 'commentsCount', int),
        'hubs': hubs,
        'tags': tags,
        'lead_text': _field(lead, 'textHtml', str),
        'post_type': _field(raw, 'postType', str),
    }


# =============================================================================
# Public fetchers
# =============================================================================


_RSS_FIELDS = ('title', 'guid', 'link', 'pubDate', 'description')
_CDATA_OPEN, _CDATA_CLOSE = '<![CDATA[', ']]>'
_HUB_ALIAS_RE = re.compile(r'[A-Za-z0-9_-]{1,50}')
# Items read from one feed; a real feed has RSS_LIMIT, this bounds the kek requests per feed
RSS_MAX_ITEMS: int = RSS_LIMIT * 2


def _find_outside_cdata(text: str, needle: str, start: int, end: int) -> int:
    """text.find(needle, start, end) ignoring matches inside CDATA sections; -1 if none.

    Linear: each CDATA section is skipped once. Markup quoted inside a description's CDATA
    can never open a fake item or field.
    """
    pos = start
    while True:
        hit = text.find(needle, pos, end)
        cdata = text.find(_CDATA_OPEN, pos, hit if hit >= 0 else end)
        if cdata < 0:
            return hit
        close = text.find(_CDATA_CLOSE, cdata + len(_CDATA_OPEN), end)
        if close < 0:
            return -1
        pos = close + len(_CDATA_CLOSE)


def _rss_elements(text: str, name: str, start: int = 0, end: int | None = None):
    """Yield the raw content of every <name ...>...</name> in text[start:end].

    str.find only, linear in the body size: remote input never reaches a backtracking
    regex and no XML entity is ever expanded. CDATA sections may contain '<'.
    """
    end = len(text) if end is None else end
    open_tag, close_tag = f'<{name}', f'</{name}>'
    pos = start
    while True:
        tag = _find_outside_cdata(text, open_tag, pos, end)
        if tag < 0:
            return
        after = tag + len(open_tag)
        if after >= end or text[after] not in '> \t\r\n':     # <titleX> is another element
            pos = after
            continue
        body = text.find('>', after, end)
        if body < 0:
            return
        body += 1
        first = body
        while first < end and text[first].isspace():
            first += 1
        search_from = body
        if text.startswith(_CDATA_OPEN, first):                  # skip a '</name>' inside CDATA
            search_from = text.find(_CDATA_CLOSE, first, end)
            if search_from < 0:
                return
        content_end = text.find(close_tag, search_from, end)
        if content_end < 0:
            return
        yield text[body:content_end]
        pos = content_end + len(close_tag)


def _rss_text(raw: str | None, *, plain_text: bool = True) -> str | None:
    """Decoded element text, unescaped exactly once.

    Plain content is XML-escaped: unescaped once. CDATA content is raw; for plain-text
    fields (title, category) Habr puts HTML there like kek `titleHtml`, so it is
    unescaped once too; for HTML fields (`plain_text=False`, description) it is kept.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if raw.startswith(_CDATA_OPEN) and raw.endswith(_CDATA_CLOSE):
        cdata = raw[len(_CDATA_OPEN):-len(_CDATA_CLOSE)]
        return html.unescape(cdata) if plain_text else cdata
    return html.unescape(raw)


def _first(text: str, name: str, start: int, end: int) -> str | None:
    return next(_rss_elements(text, name, start, end), None)


def _parse_rss_item(text: str, start: int, end: int) -> dict | None:
    """The RSS <item> in text[start:end] -> light item; None if it is not a Habr article
    (sandbox, posts...). Raises HabrUnavailable('invalid_shape') on a bad pubDate."""
    fields = {name: _rss_text(_first(text, name, start, end), plain_text=name != 'description')
              for name in _RSS_FIELDS}
    habr_id = parse_article_id(fields['guid']) or parse_article_id(fields['link'])
    if habr_id is None:
        return None
    try:
        published = parsedate_to_datetime(fields['pubDate'] or '')
    except (TypeError, ValueError) as e:
        raise _invalid('bad pubDate') from e
    if published.tzinfo is None or published.utcoffset() is None:
        raise _invalid('naive pubDate')
    return {
        'habr_id': habr_id,
        'title': fields['title'] or '',
        'url': canonical_url(habr_id),
        'published_at': published.astimezone(timezone.utc),
        'lead_text': fields['description'] or '',
        'tags': [_rss_text(raw) or '' for raw in _rss_elements(text, 'category', start, end)],
    }


def fetch_rss(path: str, *, wait_timeout: float | None = None, deadline: float | None = None) -> list[dict]:
    """Light items of one RSS feed (RSS_BASE + path) in feed order:
    {habr_id, title, url, published_at, lead_text, tags}.

    No score, comments or hubs: RSS does not carry them. `lead_text` (the RSS description
    HTML, with a "Читать далее" link) and `tags` (every <category> in the item, hub titles
    included) are raw RSS values, not kek-normalized; callers do not store or show them.
    Non-article entries are skipped;
    an item with a bad date is skipped and counted in a warning (one broken entry must not
    stop every digest). Raises HabrUnavailable on a non-200 or a body that is not an RSS
    channel; HabrBusy on a lock/deadline timeout.
    """
    status, body = _request(path, {'fl': 'ru', 'limit': RSS_LIMIT}, wait_timeout, deadline, base=RSS_BASE)
    if status != 200:
        raise HabrUnavailable('http', status)
    text = body.decode('utf-8', errors='replace')
    if '<rss' not in text or '<channel>' not in text:
        raise HabrUnavailable('invalid_shape', status, 'not an RSS channel')
    items, bad = [], 0
    pos = 0
    while len(items) < RSS_MAX_ITEMS and (start := _find_outside_cdata(text, '<item>', pos, len(text))) >= 0:
        end = _find_outside_cdata(text, '</item>', start, len(text))
        if end < 0:
            break
        pos = end + len('</item>')
        try:
            item = _parse_rss_item(text, start + len('<item>'), end)
        except HabrUnavailable:
            bad += 1
            continue
        if item is not None:
            items.append(item)
    if bad:
        logger.warning('Habr RSS: skipped %d items with a bad date, feed=%s', bad, path)
    return items


def hub_feed_path(alias: str) -> str:
    """RSS path of a hub's "all articles" feed; an unsafe alias raises ValueError."""
    if not isinstance(alias, str) or not _HUB_ALIAS_RE.fullmatch(alias):
        raise ValueError(f'unsafe hub alias: {alias!r:.60}')
    return f'hubs/{alias}/articles/'


def fetch_rss_since(since: datetime, *, until: datetime | None = None, hubs: Iterable[str] = (),
                    top: bool = False, wait_timeout: float | None = None,
                    deadline: float | None = None) -> list[dict]:
    """Light items with since <= published_at < until from the hub feeds of `hubs` (and the
    site top feeds when `top`), deduplicated by id, newest first.

    Each item gets `hubs` = [(alias, 'collective')] of the requested hub feeds it was found
    in (an article found in the top feeds only has none). A hub feed whose RSS_LIMIT items
    all fall after `since` cannot cover the window: logged, the rest is still used.
    A hub feed answering HUB_GONE_STATUSES (hub renamed/removed) is skipped with a
    warning; any other failure of any feed fails the whole call (HabrUnavailable / HabrBusy).
    Hub aliases are deduplicated case-insensitively and requested with their own case;
    an unsafe alias (see hub_feed_path) is skipped with a warning.
    """
    if not isinstance(since, datetime) or since.tzinfo is None or since.utcoffset() is None:
        raise ValueError('since must be an aware datetime')
    feeds, seen = [], set()
    for alias in hubs:
        key = alias.lower() if isinstance(alias, str) else alias
        if key in seen:
            continue
        seen.add(key)
        try:
            feeds.append((hub_feed_path(alias), alias))
        except ValueError:
            # one bad stored alias must not abort the digest for everyone
            logger.warning('Habr hub feed skipped: unsafe alias %r', str(alias)[:60])
    if top:
        feeds += [(path, None) for path in RSS_TOP_FEEDS]
    started = time.monotonic()
    found: dict[int, dict] = {}
    path = ''
    try:
        for path, alias in feeds:
            try:
                items = fetch_rss(path, wait_timeout=wait_timeout, deadline=deadline)
            except HabrUnavailable as e:
                # A renamed/removed hub must not block every digest and /feed forever
                if alias is None or e.kind != 'http' or e.status not in HUB_GONE_STATUSES:
                    raise
                logger.warning('Habr hub feed skipped: hub=%s, HTTP %s', alias, e.status)
                continue
            if alias is not None and len(items) >= RSS_LIMIT and all(i['published_at'] >= since for i in items):
                logger.warning('Habr RSS does not cover the window: hub=%s', alias)
            for item in items:
                if item['published_at'] < since or (until is not None and item['published_at'] >= until):
                    continue
                kept = found.setdefault(item['habr_id'], {**item, 'hubs': []})
                if alias is not None and (alias, 'collective') not in kept['hubs']:
                    kept['hubs'].append((alias, 'collective'))
    except HabrUnavailable as e:
        logger.warning('Habr RSS fetch failed: feed=%s, %s', path, e)
        raise
    result = sorted(found.values(), key=lambda i: i['published_at'], reverse=True)
    logger.info('Habr RSS fetched: feeds=%d, items=%d, duration=%.1fs',
                len(feeds), len(result), time.monotonic() - started)
    return result


def fetch_articles_since(since: datetime, *, until: datetime | None = None, hubs: Iterable[str] = (),
                         top: bool = False, wait_timeout: float | None = None,
                         deadline: float | None = None) -> list[dict]:
    """Full normalized items published in [since, until): candidates from the RSS feeds
    (fetch_rss_since), then one fetch_article per candidate for score, comments and hubs.

    Newest first. Articles gone meanwhile (HabrNotFound) are dropped. Individual
    HabrUnavailable failures are dropped too, up to max(ARTICLE_FAILURES_MIN,
    ARTICLE_FAILURES_SHARE of the candidates); more fail the attempt with kind 'partial'.
    HabrBusy propagates.
    """
    candidates = fetch_rss_since(since, until=until, hubs=hubs, top=top,
                                 wait_timeout=wait_timeout, deadline=deadline)
    allowed = max(ARTICLE_FAILURES_MIN, int(len(candidates) * ARTICLE_FAILURES_SHARE))
    started = time.monotonic()
    items, failed, gone = [], 0, 0
    for candidate in candidates:
        try:
            items.append(fetch_article(candidate['habr_id'], wait_timeout=wait_timeout, deadline=deadline))
        except HabrNotFound:
            gone += 1
        except HabrUnavailable as e:
            failed += 1
            if failed > allowed:
                raise HabrUnavailable('partial', e.status, f'{failed} article fetches failed') from e
    logger.info('Habr articles fetched: candidates=%d, items=%d, gone=%d, failed=%d, duration=%.1fs',
                len(candidates), len(items), gone, failed, time.monotonic() - started)
    return items


def _error_code(body: bytes) -> str | None:
    """errorCode of a kek error envelope; None for a non-JSON body (e.g. an HTML 403/404 page)."""
    try:
        error = json.loads(body)
    except (ValueError, RecursionError):
        return None
    code = error.get('errorCode') if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def fetch_article(habr_id: int, *, wait_timeout: float | None = None,
                  deadline: float | None = None) -> dict:
    """One article by id as a normalized item.

    Raises HabrNotFound on 404 NOT_FOUND / POST_TYPE_MISMATCH and 403 IN_DRAFTS;
    HabrUnavailable on everything else (5xx, timeout, non-JSON, invalid shape, other
    statuses and error codes).
    """
    habr_id = _check_id(habr_id)
    started = time.monotonic()
    try:
        status, body = _request(f'articles/{habr_id}/', {'fl': 'ru', 'hl': 'ru'}, wait_timeout, deadline)
        if status != 200:
            # 4xx: the errorCode decides "gone" vs "unknown"; it is kept (sanitized) for logs
            code = _error_code(body) if 400 <= status < 500 else None   # body already size-capped
            if code in _GONE_BY_STATUS.get(status, ()):
                logger.info('Habr article unavailable: id=%d, status=%d, code=%s', habr_id, status, code)
                raise HabrNotFound(code, status)
            raise HabrUnavailable('http', status, error_code=code)
        data = _decode_json(status, body)
        try:
            item = _normalize_item(data, expected_id=habr_id)
        except HabrUnavailable as e:
            e.status = status if e.status is None else e.status   # a 200 whose content is unusable
            raise
    except HabrUnavailable as e:
        logger.warning('Habr article fetch failed: id=%d, %s', habr_id, e)
        raise
    logger.info('Habr article fetched: id=%d, duration=%.1fs', habr_id, time.monotonic() - started)
    return item


def fetch_comments(habr_id: int, *, wait_timeout: float | None = None,
                   deadline: float | None = None) -> dict:
    """Raw comment tree of one article: the kek `comments` object ({id: comment}), {} if none.

    Goes through the same request policy as every other kek call (throttle, no
    redirects), with its own size cap COMMENTS_MAX_RESPONSE_BYTES. Entries that are not
    objects are dropped, so callers can treat every value as a dict. The tree walk and
    formatting stay in scraper. Raises HabrUnavailable on any non-200, invalid JSON or a
    non-object `comments` (an oversized tree is logged at INFO: comments dropped);
    HabrBusy on a lock/deadline timeout. Logs carry type/kind/status only.
    """
    habr_id = _check_id(habr_id)
    try:
        data = _get_json_object(f'articles/{habr_id}/comments/', {'fl': 'ru', 'hl': 'ru'},
                                wait_timeout, deadline, COMMENTS_MAX_RESPONSE_BYTES)
        comments = data.get('comments') or {}
        if not isinstance(comments, dict):
            raise _invalid('comments is not an object')
    except HabrUnavailable as e:
        if e.kind == 'too_large':
            logger.info('Habr comments dropped, response over %d bytes: id=%d',
                        COMMENTS_MAX_RESPONSE_BYTES, habr_id)
        else:
            logger.warning('Habr comments fetch failed: id=%d, kind=%s, status=%s',
                           habr_id, e.kind, e.status)
        raise
    kept = {cid: c for cid, c in comments.items() if isinstance(c, dict)}
    if len(kept) != len(comments):
        logger.warning('Habr comments: dropped %d malformed entries, id=%d', len(comments) - len(kept), habr_id)
    return kept


def _normalize_hub(alias: str, ref: Any) -> dict:
    if _field(ref, 'alias', str) != alias:
        raise _invalid('hub alias mismatch')
    # Deviation from "hub_type taken from the API": the live catalog (verified
    # 2026-11-04, 439 hubs) has no per-hub type field and is the collective-hub
    # namespace only (company blogs are not listed; an alias such as `bitrix` can
    # exist both as a collective hub here and as a corporative hub on articles).
    # An explicit API type, when present, is validated and taken as-is.
    hub_type = ref.get('type', 'collective')
    if hub_type not in HUB_TYPES:
        raise _invalid('bad hub type')
    rating = _field(_field(ref, 'statistics', dict), 'rating', (int, float))
    return {
        'alias': alias,
        'title': html.unescape(_field(ref, 'titleHtml', str)),
        'rating': float(rating),
        'hub_type': hub_type,
    }


def fetch_hub_catalog(*, wait_timeout: float | None = None, deadline: float | None = None) -> list[dict]:
    """Whole hub catalog: [{alias, title, rating, hub_type}] in API order (rating desc).

    Not filtered; any failed, short or malformed page raises HabrUnavailable.
    """
    started = time.monotonic()
    hubs: list[dict] = []
    seen: set[str] = set()
    page = 0
    pages_count = 1
    try:
        while page < pages_count:
            page += 1
            params = {'perPage': HUBS_PER_PAGE, 'page': page, 'fl': 'ru', 'hl': 'ru'}
            data = _get_json_object('hubs/', params, wait_timeout, deadline)
            count = _field(data, 'pagesCount', int)
            if page == 1:
                if not 1 <= count <= HUBS_MAX_PAGES:
                    raise HabrUnavailable('partial', 200, f'bad pagesCount {count}')
                pages_count = count
            elif count != pages_count:
                raise HabrUnavailable('partial', 200, 'pagesCount changed')
            ids = _field(data, 'hubIds', list)
            refs = _field(data, 'hubRefs', dict)
            if len(ids) < HUBS_PER_PAGE and page < pages_count:
                raise HabrUnavailable('partial', 200, f'short hubs page {page}/{pages_count}')
            for alias in ids:
                if not isinstance(alias, str) or alias not in refs:
                    raise HabrUnavailable('partial', 200, f'hub missing from refs on page {page}')
                hub = _normalize_hub(alias, refs[alias])
                if alias not in seen:
                    seen.add(alias)
                    hubs.append(hub)
        if not hubs:
            raise HabrUnavailable('partial', 200, 'empty catalog')
    except HabrUnavailable as e:
        logger.warning('Habr hub catalog fetch failed: page=%d, %s', page, e)
        raise
    logger.info('Habr hub catalog fetched: requests=%d, hubs=%d, duration=%.1fs',
                page, len(hubs), time.monotonic() - started)
    return hubs
