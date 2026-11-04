"""
Client for the public (undocumented) Habr kek API: https://habr.com/kek/v2/...

The single source of Habr data and Habr article identity for the bot:
    - parse_article_id(url) / canonical_url(habr_id) — strict URL <-> id mapping;
    - fetch_articles_since(since) — site-wide "Новые" list down to a time boundary;
    - fetch_article(habr_id) — one article by id;
    - fetch_hub_catalog() — the whole hub catalog.

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
    - HabrNotFound — 404 with errorCode NOT_FOUND / POST_TYPE_MISMATCH;
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
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests

from scraper import USER_AGENT

logger = logging.getLogger(__name__)

__all__ = [
    'HabrNotFound', 'HabrUnavailable', 'HabrBusy',
    'parse_article_id', 'canonical_url',
    'fetch_articles_since', 'fetch_article', 'fetch_hub_catalog',
    'handler_deadline', 'wait_budget',
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
LIST_PER_PAGE: int = 100
LIST_MAX_ITEMS: int = 1000          # Habr paginator cap (400 PAGINATOR_LIMIT beyond it)
HUBS_PER_PAGE: int = 100
HUBS_MAX_PAGES: int = 20            # ~5 pages live; a sanity bound against a runaway loop
# Small chunks: urllib3 blocks until a whole chunk arrives, so this bounds how long
# a slow-drip body can hold the lock between whole-body deadline checks
_READ_CHUNK: int = 8 * 1024

NOT_FOUND_CODES = frozenset({'NOT_FOUND', 'POST_TYPE_MISMATCH'})
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

# Throttle state: one per process, shared by handlers, digest and backfill threads
_lock = threading.Lock()
_last_request_at: float | None = None   # time.monotonic() of the last request end


# =============================================================================
# Exceptions
# =============================================================================


class HabrNotFound(Exception):
    """The article does not exist / is not available (kek 404 NOT_FOUND, POST_TYPE_MISMATCH)."""

    def __init__(self, error_code: str):
        super().__init__(f'Habr article not found ({error_code})')
        self.error_code = error_code
        self.status = 404


class HabrUnavailable(Exception):
    """Transient, partial or invalid kek response.

    Attributes:
        kind: short category — 'timeout', 'network', 'http', 'redirect',
            'too_large', 'invalid_json', 'invalid_shape', 'partial'.
        status: HTTP status when one was received, else None.
    """

    def __init__(self, kind: str, status: int | None = None, detail: str = ''):
        message = f'Habr unavailable: {kind}' + (f' (HTTP {status})' if status is not None else '')
        if detail:
            message += f': {detail}'
        super().__init__(message)
        self.kind = kind
        self.status = status


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
    return time.monotonic() + HANDLER_DEADLINE


def _time_left(deadline: float) -> float:
    left = deadline - time.monotonic()
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


def _read_capped(response, read_timeout: float) -> bytes:
    length = response.headers.get('Content-Length')
    if length is not None and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
        raise HabrUnavailable('too_large', response.status_code)
    deadline = time.monotonic() + read_timeout
    body = bytearray()
    for chunk in response.iter_content(chunk_size=_READ_CHUNK):
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise HabrUnavailable('too_large', response.status_code)
        # requests' timeout is per socket read; bound the whole body read too
        if time.monotonic() > deadline:
            raise HabrUnavailable('timeout', response.status_code, 'body read too slow')
    return bytes(body)


def _request(path: str, params: dict, wait_timeout: float | None,
             deadline: float | None = None) -> tuple[int, bytes]:
    """The only place that talks to Habr. Returns (status, body); 3xx is a failure.

    The lock is held only around the HTTP exchange itself. With a `deadline`, the lock
    wait, the request timeout and the body read are cut to the time left; a timeout
    caused by that cut is HabrBusy, not HabrUnavailable.
    """
    global _last_request_at
    url = API_BASE + path
    _acquire(wait_budget(wait_timeout, deadline))
    try:
        _wait_interval()
        timeout = HABR_API_TIMEOUT if deadline is None else min(HABR_API_TIMEOUT, _time_left(deadline))
        cut_by_deadline = timeout < HABR_API_TIMEOUT
        try:
            response = requests.get(
                url,
                params=params,
                headers={'User-Agent': USER_AGENT},
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
            try:
                status = response.status_code
                if 300 <= status < 400:
                    raise HabrUnavailable('redirect', status)
                body = _read_capped(response, timeout)
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
                     deadline: float | None = None) -> dict:
    """200 + JSON object, else HabrUnavailable."""
    status, body = _request(path, params, wait_timeout, deadline)
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


def fetch_articles_since(since: datetime, *, wait_timeout: float | None = None,
                         deadline: float | None = None) -> list[dict]:
    """Site-wide "Новые" list (newest first) down to `since`, deduplicated by id.

    Pages of 100 from page 1 until a page's last item is older than `since`.
    Returns items with published_at >= since in API order. Any partial or invalid
    page fails the whole attempt with HabrUnavailable (no partial list).
    Callers apply their own upper bound locally.
    """
    if not isinstance(since, datetime) or since.tzinfo is None or since.utcoffset() is None:
        raise ValueError('since must be an aware datetime')
    started = time.monotonic()
    items: list[dict] = []
    seen: set[int] = set()
    page = 0
    try:
        while True:
            page += 1
            # sort=rating WITHOUT period is the "Новые" flow; never send news/period
            params = {'sort': 'rating', 'fl': 'ru', 'hl': 'ru', 'perPage': LIST_PER_PAGE, 'page': page}
            data = _get_json_object('articles/', params, wait_timeout, deadline)
            pages_count = _field(data, 'pagesCount', int)
            ids = _field(data, 'publicationIds', list)
            refs = _field(data, 'publicationRefs', dict)

            page_items = []
            for raw_id in ids:
                if not isinstance(raw_id, str) or raw_id not in refs:
                    raise HabrUnavailable('partial', 200, f'id missing from refs on page {page}')
                if not _DIGITS_RE.fullmatch(raw_id):
                    raise _invalid('bad id in publicationIds')
                page_items.append(_normalize_item(refs[raw_id], expected_id=int(raw_id)))

            if len(ids) < LIST_PER_PAGE and page < pages_count:
                raise HabrUnavailable('partial', 200, f'short page {page}/{pages_count}')

            for item in page_items:
                if item['published_at'] >= since and item['habr_id'] not in seen:
                    seen.add(item['habr_id'])
                    items.append(item)

            if page_items and page_items[-1]['published_at'] < since:
                break
            if page >= pages_count or page * LIST_PER_PAGE >= LIST_MAX_ITEMS:
                raise HabrUnavailable('partial', 200, 'boundary not reached')
    except HabrUnavailable as e:
        logger.warning('Habr list fetch failed: page=%d, %s', page, e)
        raise
    logger.info('Habr list fetched: requests=%d, items=%d, duration=%.1fs',
                page, len(items), time.monotonic() - started)
    return items


def fetch_article(habr_id: int, *, wait_timeout: float | None = None,
                  deadline: float | None = None) -> dict:
    """One article by id as a normalized item.

    Raises HabrNotFound on 404 NOT_FOUND / POST_TYPE_MISMATCH; HabrUnavailable on
    everything else (5xx, timeout, non-JSON, invalid shape, other statuses).
    """
    habr_id = _check_id(habr_id)
    started = time.monotonic()
    try:
        status, body = _request(f'articles/{habr_id}/', {'fl': 'ru', 'hl': 'ru'}, wait_timeout, deadline)
        if status == 404:
            error = _decode_json(status, body)
            code = error.get('errorCode') if isinstance(error, dict) else None
            if code in NOT_FOUND_CODES:
                logger.info('Habr article not found: id=%d, code=%s', habr_id, code)
                raise HabrNotFound(code)
            raise HabrUnavailable('http', status)
        if status != 200:
            raise HabrUnavailable('http', status)
        data = _decode_json(status, body)
        item = _normalize_item(data, expected_id=habr_id)
    except HabrUnavailable as e:
        logger.warning('Habr article fetch failed: id=%d, %s', habr_id, e)
        raise
    logger.info('Habr article fetched: id=%d, duration=%.1fs', habr_id, time.monotonic() - started)
    return item


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
