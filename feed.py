"""
Feed logic shared by `/feed` (bot) and the daily digest (`digest.py`): no Telegram code.

Contents:
    - text cleaning: URL_RE, MENTION_RE, COMMAND_RE, strip_control_bidi, html_to_text,
      fallback_description, sanitize_title;
    - MSK time: MSK, msk_day_bounds, msk_date, format_date_ru;
    - 24 h fetch cache: fetch_recent (5-minute reuse, handler wait mode);
    - ranking (Decision 6): select_feed, select_digest_site, select_digest_hubs;
    - ranked-list persistence: create_list, load_page;
    - rendering (Decision 7): render_page / render_list_page -> (text, rows), where rows
      are `list[list[tuple[str, str]]]` of (button text, callback data);
    - callback codec (Decision 5): encode_summary, encode_more, decode_list_callback,
      resolve_summary_click;
    - InFlightRegistry (Decision 13) and the single module instance REGISTRY;
    - view_source(list_kind) for habr_store.record_view.

Paging offsets. `hl:m:{list_id}:{offset}` carries a 0-based multiple of 10.
    - digest lists (`digest_site`, `digest_hubs`): the digest message itself shows
      positions 1-5, so a page opened by offset covers items [offset+5, offset+15):
      offset 0 -> positions 6-15, offset 10 -> 16-25;
    - `feed` lists: page 1 (positions 1-10) is sent by the command, so a page covers
      items [offset, offset+10) and the first "more" button carries offset 10;
      offset 0 is not a valid "more" offset for a feed list.
The codec accepts an offset only if it is a multiple of 10 valid for the list kind and
the page start lies within the list.

Clock: `_now()` is the single patchable clock source; FETCH_CACHE_TTL is patchable too.
"""

import html
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, NamedTuple

from bs4 import BeautifulSoup

import habr_api
import habr_store

logger = logging.getLogger(__name__)

# =============================================================================
# Constants and user-facing texts
# =============================================================================

MSK = timezone(timedelta(hours=3))

FEED_WINDOW = timedelta(hours=24)
FETCH_CACHE_TTL = timedelta(minutes=5)
LIST_MAX_AGE = timedelta(days=habr_store.RANKED_LIST_TTL_DAYS)
PAGE_SIZE = 10
BUTTONS_PER_ROW = 5
DIGEST_TOP = 5            # positions shown by the digest message itself
TITLE_MAX_LEN = 200
CALLBACK_MAX_BYTES = 64

TEXT_STALE_BUTTON = 'Кнопка устарела'
TEXT_NO_SUBSCRIPTIONS = 'Подписок пока нет — /subscribe'
TEXT_FEED_EMPTY = 'За последние сутки в твоих хабах ничего нового'
TEXT_FEED_END = 'Это все новые статьи за сутки'
TEXT_DIGEST_END = 'Это все статьи за {date}'
TEXT_MORE_BUTTON = 'Ещё 10 ▶'
TEXT_ALREADY_RUNNING = 'Саммари уже готовится'
TEXT_WAITER = 'Саммари уже готовится, пришлю сюда'
TEXT_NO_TITLE = 'Без названия'

DIGEST_KINDS = ('digest_site', 'digest_hubs')
FEED_KIND = 'feed'

_MONTHS_GENITIVE = (
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
)

_VIEW_SOURCES = {'digest_site': 'digest', 'digest_hubs': 'digest', 'feed': 'feed'}

# ASCII digits only and bounded lengths (Decision 5); applied with fullmatch
_LIST_CALLBACK_RE = re.compile(r'hl:([sm]):([0-9]{1,9}):([0-9]{1,4})')
_MAX_LIST_ID = 10 ** 9 - 1
_MAX_POS_OR_OFFSET = 10 ** 4 - 1


def _now() -> datetime:
    """Single clock source (aware UTC); patched in tests."""
    return datetime.now(timezone.utc)


# =============================================================================
# Text cleaning (also used by summarizer and digest; names are a contract)
# =============================================================================

URL_RE = re.compile(r'(?:https?://|www\.)\S+', re.IGNORECASE)
# Not preceded by a word char, dot or @: e-mail addresses are not mentions
MENTION_RE = re.compile(r'(?<![\w.@])@\w+')
# Not preceded by a word char or slash: path parts like "a/b" are not commands
COMMAND_RE = re.compile(r'(?<![\w/])/[A-Za-z]\w*(?:@\w+)?')

# Bidi controls: LRM, RLM, ALM, embeddings/overrides, isolates
_BIDI_CHARS = frozenset(
    ['\u200e', '\u200f', '\u061c']
    + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)]
)
_KEEP_CONTROLS = frozenset('\n\t')
_WS_RE = re.compile(r'\s+')
_SPACE_BEFORE_PUNCT_RE = re.compile(r'\s+([,.;:!?])')


def strip_control_bidi(text: str) -> str:
    """
    Removes C0/C1 control characters (Unicode category Cc) and bidi controls.
    Newline and tab are kept; callers that need one line collapse whitespace themselves.
    Non-strings give ''.
    """
    if not isinstance(text, str):
        return ''
    return ''.join(
        ch for ch in text
        if ch in _KEEP_CONTROLS or (ch not in _BIDI_CHARS and unicodedata.category(ch) != 'Cc')
    )


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(' ', text).strip()


def html_to_text(html_text: str) -> str:
    """HTML -> plain text: entities unescaped, tags stripped, whitespace collapsed."""
    if not isinstance(html_text, str) or not html_text:
        return ''
    text = BeautifulSoup(html.unescape(html_text), 'html.parser').get_text(' ')
    return _collapse_ws(text)


def _cut_on_word(text: str, limit: int) -> str:
    """Cuts to <= limit chars on a word boundary, adding an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:max(limit - 1, 0)]
    if not text[len(head)].isspace() and ' ' in head:
        head = head.rsplit(' ', 1)[0]
    head = head.rstrip(' ,;:-—')
    return head + '…' if head else ''


def fallback_description(lead_html: str, limit: int = 250) -> str:
    """
    Short plain-text description from the raw lead HTML: control/bidi characters,
    URLs, @mentions and /commands removed, cut to <= `limit` chars on a word boundary.
    Empty lead -> ''.
    """
    text = strip_control_bidi(html_to_text(lead_html))
    for pattern in (URL_RE, MENTION_RE, COMMAND_RE):
        text = pattern.sub(' ', text)
    # Removed fragments leave "word , next": glue punctuation back
    return _cut_on_word(_SPACE_BEFORE_PUNCT_RE.sub(lambda m: m.group(1), _collapse_ws(text)), limit)


def sanitize_title(title: Any, limit: int = TITLE_MAX_LEN) -> str:
    """User-visible title: unescaped, control/bidi removed, one line, <= limit chars."""
    if not isinstance(title, str):
        return TEXT_NO_TITLE
    text = _collapse_ws(strip_control_bidi(html.unescape(title)))
    if not text:
        return TEXT_NO_TITLE
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + '…'
    return text


# =============================================================================
# MSK dates
# =============================================================================


def format_date_ru(day: date) -> str:
    """date(2026, 11, 2) -> '2 ноября' (Russian month genitive)."""
    return f'{day.day} {_MONTHS_GENITIVE[day.month - 1]}'


def msk_date(moment: datetime) -> date:
    """MSK calendar date of an aware datetime."""
    return moment.astimezone(MSK).date()


def msk_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC bounds [D 00:00 MSK, D+1 00:00 MSK) of the MSK day D."""
    start = datetime(day.year, day.month, day.day, tzinfo=MSK)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def view_source(list_kind: str) -> str:
    """Ranked-list kind -> article_views source."""
    try:
        return _VIEW_SOURCES[list_kind]
    except KeyError:
        raise ValueError(f'unknown ranked list kind: {list_kind!r}') from None


# =============================================================================
# 24 h fetch cache
# =============================================================================

_cache_lock = threading.Lock()
_cache: tuple[datetime, list[dict]] | None = None


def fetch_recent(now: datetime | None = None) -> list[dict]:
    """
    Normalized items of the last 24 h for `/feed`. A fetch younger than
    FETCH_CACHE_TTL is reused; otherwise habr_api.fetch_articles_since is called in
    handler mode and the metadata (with hubs and tags) is upserted.
    Single-flight: concurrent callers wait for the running fetch (bounded by
    HANDLER_WAIT_TIMEOUT, then HabrBusy). HabrBusy / HabrUnavailable propagate.
    """
    global _cache
    now = now or _now()
    cached = _fresh_cache(now)
    if cached is not None:
        return cached
    if not _cache_lock.acquire(timeout=habr_api.HANDLER_WAIT_TIMEOUT):
        raise habr_api.HabrBusy('feed fetch is already running')
    try:
        cached = _fresh_cache(now)
        if cached is not None:
            return cached
        items = habr_api.fetch_articles_since(now - FEED_WINDOW, wait_timeout=habr_api.HANDLER_WAIT_TIMEOUT)
        # The network call is finished: only now is a DB transaction opened
        habr_store.upsert_habr_articles(items)
        _cache = (now, items)
        return items
    finally:
        _cache_lock.release()


def _fresh_cache(now: datetime) -> list[dict] | None:
    cache = _cache
    if cache is None:
        return None
    age = now - cache[0]
    if timedelta(0) <= age < FETCH_CACHE_TTL:
        return cache[1]
    return None


# =============================================================================
# Ranking and filtering (Decision 6) — pure functions over normalized items
# =============================================================================


def _score(item: dict) -> int:
    value = item.get('score')
    return value if isinstance(value, int) else 0


def _comments(item: dict) -> int:
    value = item.get('comments')
    return value if isinstance(value, int) else 0


def _published(item: dict) -> datetime | None:
    value = item.get('published_at')
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value
    return None


def _subscribed(item: dict, subscriptions: set[str]) -> bool:
    """True if any COLLECTIVE hub of the item is subscribed (corporative never matches)."""
    for hub in item.get('hubs') or ():
        # (alias, type) tuples from habr_api; {alias, hub_type} dicts from habr_store
        alias, hub_type = (hub.get('alias'), hub.get('hub_type')) if isinstance(hub, dict) else hub
        if hub_type == 'collective' and isinstance(alias, str) and alias.lower() in subscriptions:
            return True
    return False


def _unique_unseen(items, seen: set[int], keep: Callable[[dict], bool]) -> list[dict]:
    result, taken = [], set()
    for item in items:
        habr_id = item.get('habr_id')
        if habr_id in seen or habr_id in taken or _published(item) is None or not keep(item):
            continue
        taken.add(habr_id)
        result.append(item)
    return result


def _lower_set(subscriptions) -> set[str]:
    return {s.lower() for s in subscriptions if isinstance(s, str)}


def select_feed(items, subscriptions, seen, now: datetime) -> list[dict]:
    """
    `/feed` list: published within [now-24h, ...), any collective hub subscribed,
    minus seen; ordered score DESC, published DESC.
    """
    subs = _lower_set(subscriptions)
    since = now - FEED_WINDOW
    selected = _unique_unseen(
        items, set(seen), lambda i: _published(i) >= since and _subscribed(i, subs),
    )
    return sorted(selected, key=lambda i: (_score(i), _published(i)), reverse=True)


def _in_day(item: dict, day: date) -> bool:
    start, end = msk_day_bounds(day)
    return start <= _published(item) < end


def _digest_order(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (_score(i), _comments(i), _published(i)), reverse=True)


def select_digest_site(items, seen, day: date) -> list[dict]:
    """Digest site list: items of the MSK day D minus seen; score, comments, published DESC."""
    return _digest_order(_unique_unseen(items, set(seen), lambda i: _in_day(i, day)))


def select_digest_hubs(items, subscriptions, seen, day: date, site_top) -> list[dict]:
    """
    Digest hubs list: the same day set where any collective hub is subscribed, minus
    seen and minus `site_top` (items or habr_ids of the site top-5); each article once.
    """
    subs = _lower_set(subscriptions)
    excluded = set(seen) | {t['habr_id'] if isinstance(t, dict) else t for t in site_top}
    return _digest_order(
        _unique_unseen(items, excluded, lambda i: _in_day(i, day) and _subscribed(i, subs)),
    )


# =============================================================================
# Ranked-list persistence
# =============================================================================


def create_list(user_id: int, kind: str, list_date: date, items) -> int:
    """Persists a ranked list snapshot (positions 1..N in the given order); returns list_id."""
    return habr_store.create_ranked_list(
        user_id, kind, list_date.isoformat(), [i['habr_id'] if isinstance(i, dict) else i for i in items],
    )


def page_start(kind: str, offset: int) -> int:
    """0-based index of the first item of the page opened by `offset` (see module doc)."""
    return offset + DIGEST_TOP if kind in DIGEST_KINDS else offset


def next_offset(kind: str, offset: int, total: int) -> int | None:
    """Offset for the "more" button after the page opened by `offset`, None if nothing remains."""
    return offset + PAGE_SIZE if page_start(kind, offset) + PAGE_SIZE < total else None


def load_page(list_id: int, offset: int) -> dict | None:
    """
    List header plus the page opened by `offset`: `entries` = [(position, metadata|None)].
    None if the list does not exist. Metadata comes from habr_store (no network).
    """
    header = habr_store.get_ranked_list(list_id, offset=0, limit=0)
    if header is None:
        return None
    page = habr_store.get_ranked_list(list_id, offset=page_start(header['kind'], offset), limit=PAGE_SIZE)
    meta = habr_store.get_habr_articles(i['habr_id'] for i in page['items'])
    return {**header, 'offset': offset,
            'entries': [(i['position'], meta.get(i['habr_id'])) for i in page['items']]}


# =============================================================================
# Rendering (Decision 7)
# =============================================================================

Rows = list[list[tuple[str, str]]]


def _fmt_int(value: Any) -> str:
    return str(value) if isinstance(value, int) else '—'


def render_line(position: int, meta: dict | None) -> str:
    """'11. Title — рейтинг 42, комментарии 7'; missing fields render as safe placeholders."""
    meta = meta or {}
    comments = meta.get('comments_count', meta.get('comments'))
    return f'{position}. {sanitize_title(meta.get("title"))} — рейтинг {_fmt_int(meta.get("score"))}, ' \
           f'комментарии {_fmt_int(comments)}'


def number_rows(list_id: int, positions) -> Rows:
    """Numeric "summary" buttons in rows of 5."""
    buttons = [(str(p), encode_summary(list_id, p)) for p in positions]
    return [buttons[i:i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]


def more_row(list_id: int, offset: int) -> list[tuple[str, str]]:
    return [(TEXT_MORE_BUTTON, encode_more(list_id, offset))]


def end_text(kind: str, list_date: date | str) -> str:
    if kind == FEED_KIND:
        return TEXT_FEED_END
    if isinstance(list_date, str):
        list_date = date.fromisoformat(list_date)
    return TEXT_DIGEST_END.format(date=format_date_ru(list_date))


def render_page(list_id: int, kind: str, list_date: date | str, entries, total: int,
                offset: int) -> tuple[str, Rows]:
    """
    One page: numbered lines, numeric buttons in rows of 5, `Ещё 10 ▶` only if more
    items remain, otherwise the end text. `entries` = [(position, metadata|None)].
    """
    lines = [render_line(pos, meta) for pos, meta in entries]
    rows = number_rows(list_id, [pos for pos, _ in entries])
    more = next_offset(kind, offset, total)
    if more is not None:
        rows.append(more_row(list_id, more))
    else:
        lines += ['', end_text(kind, list_date)]
    return '\n'.join(lines), rows


def render_list_page(list_id: int, offset: int) -> tuple[str, Rows] | None:
    """Loads and renders the page opened by `offset`; None if the list does not exist."""
    page = load_page(list_id, offset)
    if page is None:
        return None
    return render_page(list_id, page['kind'], page['list_date'], page['entries'], page['total'], offset)


def build_feed(user_id: int, now: datetime | None = None) -> tuple[str, Rows]:
    """
    `/feed` first page. No subscriptions -> TEXT_NO_SUBSCRIPTIONS without any fetch;
    nothing new -> TEXT_FEED_EMPTY; otherwise a persisted `feed` list and its page 1.
    HabrBusy / HabrUnavailable from the fetch propagate to the handler.
    """
    now = now or _now()
    subscriptions = habr_store.list_subscriptions(user_id)
    if not subscriptions:
        return TEXT_NO_SUBSCRIPTIONS, []
    items = fetch_recent(now)
    selected = select_feed(items, subscriptions, habr_store.get_viewed_ids(user_id), now)
    if not selected:
        return TEXT_FEED_EMPTY, []
    list_id = create_list(user_id, FEED_KIND, msk_date(now), selected)
    page = selected[:PAGE_SIZE]
    entries = [(pos, {'title': i.get('title'), 'score': i.get('score'), 'comments_count': i.get('comments')})
               for pos, i in enumerate(page, start=1)]
    return render_page(list_id, FEED_KIND, msk_date(now), entries, len(selected), 0)


# =============================================================================
# Callback codec (Decision 5)
# =============================================================================


def _check_codec_args(list_id: int, value: int, minimum: int) -> None:
    if not (isinstance(list_id, int) and 1 <= list_id <= _MAX_LIST_ID):
        raise ValueError(f'list_id out of range: {list_id!r}')
    if not (isinstance(value, int) and minimum <= value <= _MAX_POS_OR_OFFSET):
        raise ValueError(f'position/offset out of range: {value!r}')


def encode_summary(list_id: int, position: int) -> str:
    _check_codec_args(list_id, position, 1)
    return f'hl:s:{list_id}:{position}'


def encode_more(list_id: int, offset: int) -> str:
    _check_codec_args(list_id, offset, 0)
    return f'hl:m:{list_id}:{offset}'


@dataclass(frozen=True)
class ListCallback:
    """A validated `hl:` click. `value` is the position (s) or the offset (m)."""
    action: str
    list_id: int
    value: int
    kind: str
    list_date: str
    total: int
    habr_id: int | None = None


def _stale(user_id: int, data: Any, reason: str) -> None:
    """Logs a rejected click (user id, reason, short repr only) and returns None."""
    logger.warning('Stale list button: user_id=%s, reason=%s, data=%.40r', user_id, reason, data)
    return None


def decode_list_callback(data: Any, user_id: int, now: datetime | None = None) -> ListCallback | None:
    """
    Validates `hl:s:{list_id}:{pos}` / `hl:m:{list_id}:{offset}` against the store:
    list exists, belongs to `user_id`, age <= 30 days, position exists (s) or offset is
    a multiple of 10 valid for the list kind with the page start inside the list (m).
    Any failure -> None (answer TEXT_STALE_BUTTON), logged at WARNING. Never touches
    Habr or the LLM; malformed data never reaches the store.
    """
    match = _LIST_CALLBACK_RE.fullmatch(data) if isinstance(data, str) else None
    if match is None:
        return _stale(user_id, data, 'format')
    action, list_id, value = match.group(1), int(match.group(2)), int(match.group(3))
    if list_id < 1:
        return _stale(user_id, data, 'format')

    header = habr_store.get_ranked_list(list_id, offset=0, limit=0)
    if header is None:
        return _stale(user_id, data, 'missing')
    if header['user_id'] != user_id:
        return _stale(user_id, data, 'foreign')
    try:
        created = datetime.fromisoformat(header['created_at'])
    except (TypeError, ValueError):
        return _stale(user_id, data, 'bad created_at')
    if created.tzinfo is None:
        return _stale(user_id, data, 'bad created_at')
    if (now or _now()) - created > LIST_MAX_AGE:
        return _stale(user_id, data, 'expired')

    total, kind = header['total'], header['kind']
    habr_id = None
    if action == 's':
        if not 1 <= value <= total:
            return _stale(user_id, data, 'position')
        item = habr_store.get_ranked_list(list_id, offset=value - 1, limit=1)
        if item is None or not item['items'] or item['items'][0]['position'] != value:
            return _stale(user_id, data, 'position')
        habr_id = item['items'][0]['habr_id']
    else:
        minimum = 0 if kind in DIGEST_KINDS else PAGE_SIZE
        if value % PAGE_SIZE or value < minimum or page_start(kind, value) >= total:
            return _stale(user_id, data, 'offset')
    return ListCallback(action, list_id, value, kind, header['list_date'], total, habr_id)


def resolve_summary_click(data: Any, user_id: int, now: datetime | None = None) -> ListCallback | None:
    """Valid `hl:s` click (with habr_id) or None (stale, or not a summary click)."""
    result = decode_list_callback(data, user_id, now)
    if result is None or result.action != 's':
        if result is not None:
            _stale(user_id, data, 'not a summary click')
        return None
    return result


# =============================================================================
# In-flight summary registry (Decision 13)
# =============================================================================

CLICK_STARTED = 'started'
CLICK_ALREADY_RUNNING = 'already_running'
CLICK_WAITER = 'waiter'


class ClickResult(NamedTuple):
    kind: str             # CLICK_STARTED / CLICK_ALREADY_RUNNING / CLICK_WAITER
    text: str | None      # answer for the user; None when the caller starts generation


class GenerationOutcome(NamedTuple):
    result: Any
    error: Exception | None
    waiters: set[int]


@dataclass
class _Entry:
    owner_chat: int
    waiters: set[int]


class InFlightRegistry:
    """
    One summary generation per habr_id. The first clicker owns the generation; another
    chat becomes a waiter that receives the same result; a repeated click from the owner
    or an existing waiter registers nothing new.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[int, _Entry] = {}

    def click(self, habr_id: int, chat_id: int) -> ClickResult:
        with self._lock:
            entry = self._entries.get(habr_id)
            if entry is None:
                self._entries[habr_id] = _Entry(chat_id, set())
                return ClickResult(CLICK_STARTED, None)
            if chat_id == entry.owner_chat or chat_id in entry.waiters:
                return ClickResult(CLICK_ALREADY_RUNNING, TEXT_ALREADY_RUNNING)
            entry.waiters.add(chat_id)
            return ClickResult(CLICK_WAITER, TEXT_WAITER)

    def finish(self, habr_id: int) -> set[int]:
        """Clears the entry; returns the waiter chat ids (owner not included)."""
        with self._lock:
            entry = self._entries.pop(habr_id, None)
        return set(entry.waiters) if entry else set()

    def run(self, habr_id: int, generate: Callable[[], Any]) -> GenerationOutcome:
        """
        Runs `generate()` for a started click and ALWAYS clears the entry (finally).
        An Exception from `generate` is returned in the outcome so the owner and the
        waiters get the same error; BaseException still clears the entry, then propagates.
        """
        result, error = None, None
        try:
            result = generate()
        except Exception as e:  # noqa: BLE001 — handed to the caller with the waiters
            error = e
        finally:
            waiters = self.finish(habr_id)
        return GenerationOutcome(result, error, waiters)

    def is_running(self, habr_id: int) -> bool:
        with self._lock:
            return habr_id in self._entries

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


REGISTRY = InFlightRegistry()


def _reset_for_tests() -> None:
    """Clears the fetch cache and the module REGISTRY (same object kept)."""
    global _cache, _cache_lock
    # A fresh lock: a test that failed mid-fetch must not leave the next one blocked
    _cache_lock = threading.Lock()
    _cache = None
    REGISTRY.clear()
