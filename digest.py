"""
Daily Habr digest — build and render part (Decisions 5-8). No Telegram code.

Public API (used by the delivery/scheduler part of this module, Task 10):
    digest_day(now) -> date
        The digest day: yesterday by MSK relative to `now`.
    day_bounds(day) -> (start_utc, end_utc)
        UTC window [D 00:00 MSK, D+1 00:00 MSK) of the digest day (feed.msk_day_bounds).
    fetch_day_articles(day, wait_timeout=None) -> list[dict]
        Network step, run ONCE per digest for all recipients: fetches the day through
        habr_api (boundary-based), keeps the items inside the window and upserts their
        metadata. HabrUnavailable / HabrBusy propagate.
    build_user_digest(user_id, day_articles, digest_date, describe=_USE_DEFAULT) -> dict
        Pure DB + injected describe: builds the user's site and hubs lists (Decision 6),
        fills top-5 descriptions (Decision 8), persists the full lists (`digest_site`,
        `digest_hubs`) and renders the message parts. Returns
        {"parts": [(text, rows), ...], "list_ids": [...]} where `rows` is keyboard data
        `list[list[tuple[str, str]]]` (or None) ready for `send(chat_id, text, rows)`;
        the footer is already appended to the last part. Both lists empty ->
        parts = render_empty_day(digest_date), list_ids = [].
    render_empty_day(digest_date) -> [(text, None)]
        "За вчера ничего нового" + footer, no keyboard.
    render_page(list_id, offset) -> (text, rows) | None
        A paging page of a persisted digest list (feed.render_list_page).
    dry_run(describe=_USE_DEFAULT) -> dict | None
        "No Telegram" check on a temporary copy of the DB; prints the digest for yesterday.

Descriptions. `describe(title, lead_text) -> str | None` receives the raw normalized Habr
values. `describe` defaults to the private sentinel `_USE_DEFAULT`, resolved at CALL time
to the module attribute `_DEFAULT_DESCRIBE` (None here; Task 10 sets the LLM function).
Explicit `describe=None` always means lead fallback and no LLM call. A stored
`habr_articles.short_description` is reused by every user; an accepted result is stored;
a None/invalid result or an exception is not stored and the lead fallback is shown.
No HTTP or LLM call runs inside an open SQLite transaction: every store helper opens a
short connection of its own.
"""

import logging
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from typing import Callable

import database
import feed
import habr_api
import habr_store

logger = logging.getLogger(__name__)

# =============================================================================
# Constants and user-facing texts (plain text, no parse_mode)
# =============================================================================

TEXT_HEADER_SITE = 'Лучшее на Хабре'
TEXT_HEADER_HUBS = 'Твои хабы'
TEXT_FOOTER = 'Настроить: /subscribe, выключить: /digest off'
TEXT_EMPTY_DAY = 'За вчера ничего нового'
TEXT_SUMMARY_BUTTON = 'Саммари {n}'
TEXT_HABR_UNAVAILABLE = 'Хабр недоступен: {reason}'

DESCRIPTION_LIMIT = 250
SUMMARY_BUTTONS_PER_ROW = 3      # "Саммари N" is long: 3 + 2 keeps labels readable on phones
DRY_RUN_HUBS = ('python', 'programming', 'machine_learning')
DRY_RUN_FAKE_OWNER_ID = 1        # used only when OWNER_ID is unset; the temp DB makes it safe

Describe = Callable[[str, str], 'str | None']

_USE_DEFAULT = object()
_DEFAULT_DESCRIBE: Describe | None = None


# =============================================================================
# Digest day
# =============================================================================


def digest_day(now: datetime) -> date:
    """Yesterday by MSK relative to the aware `now`."""
    return feed.msk_date(now) - timedelta(days=1)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC bounds [D 00:00 MSK, D+1 00:00 MSK) of the digest day."""
    return feed.msk_day_bounds(day)


def fetch_day_articles(day: date, wait_timeout: float | None = None) -> list[dict]:
    """
    One shared fetch for all recipients: normalized items published within the MSK day
    `day`, metadata upserted after the network call has finished.
    """
    start, end = day_bounds(day)
    items = habr_api.fetch_articles_since(start, wait_timeout=wait_timeout)
    day_items = [i for i in items if start <= i['published_at'] < end]
    habr_store.upsert_habr_articles(day_items)
    return day_items


# =============================================================================
# Descriptions (Decision 8)
# =============================================================================


def _clean_description(value) -> str | None:
    """Accepted describe/stored value -> plain lines without control/bidi chars; else None."""
    if not isinstance(value, str):
        return None
    lines = [' '.join(line.split()) for line in feed.strip_control_bidi(value).splitlines()]
    text = '\n'.join(line for line in lines if line)
    return text or None


def _call_describe(describe: Describe, item: dict) -> str | None:
    try:
        return _clean_description(describe(item.get('title'), item.get('lead_text')))
    except Exception as e:  # noqa: BLE001 — a failed description never breaks the digest
        logger.warning('Short description failed: habr_id=%s, %s', item.get('habr_id'), type(e).__name__)
        return None


def _descriptions(items: list[dict], describe: Describe | None) -> dict[int, str]:
    """habr_id -> description (stored, generated, or lead fallback; '' if none)."""
    ids = [i['habr_id'] for i in items]
    stored = habr_store.get_habr_articles(ids)          # short read connection, closed
    result: dict[int, str] = {}
    for item in items:
        habr_id = item['habr_id']
        if habr_id in result:
            continue
        text = _clean_description((stored.get(habr_id) or {}).get('short_description'))
        if text is None and describe is not None:
            # No connection is open here: the LLM call never runs inside a transaction
            text = _call_describe(describe, item)
            if text is not None:
                habr_store.set_short_description(habr_id, text)
        if text is None:
            text = feed.fallback_description(item.get('lead_text') or '', limit=DESCRIPTION_LIMIT)
        result[habr_id] = text
    return result


# =============================================================================
# Rendering (Decision 7)
# =============================================================================


def _summary_rows(list_id: int, count: int) -> feed.Rows:
    buttons = [(TEXT_SUMMARY_BUTTON.format(n=n), feed.encode_summary(list_id, n)) for n in range(1, count + 1)]
    return [buttons[i:i + SUMMARY_BUTTONS_PER_ROW] for i in range(0, len(buttons), SUMMARY_BUTTONS_PER_ROW)]


def _render_part(header: str, list_id: int, items: list[dict], descriptions: dict[int, str]) -> tuple[str, feed.Rows]:
    top = items[:feed.DIGEST_TOP]
    blocks = [header]
    for pos, item in enumerate(top, start=1):
        line = feed.render_line(pos, {'title': item.get('title'), 'score': item.get('score'),
                                      'comments': item.get('comments')})
        description = descriptions.get(item['habr_id'])
        blocks.append(f'{line}\n{description}' if description else line)
    rows = _summary_rows(list_id, len(top))
    if len(items) > feed.DIGEST_TOP:
        rows.append(feed.more_row(list_id, 0))
    return '\n\n'.join(blocks), rows


def _with_footer(text: str) -> str:
    return f'{text}\n\n{TEXT_FOOTER}'


def render_empty_day(digest_date: date) -> list[tuple[str, None]]:
    """Both parts empty: one message without a keyboard."""
    return [(_with_footer(TEXT_EMPTY_DAY), None)]


def render_page(list_id: int, offset: int) -> tuple[str, feed.Rows] | None:
    """Paging page of a persisted digest list; the last page ends with 'Это все статьи за …'."""
    return feed.render_list_page(list_id, offset)


# =============================================================================
# Build
# =============================================================================


def build_user_digest(user_id: int, day_articles: list[dict], digest_date: date,
                      describe=_USE_DEFAULT) -> dict:
    """See the module docstring. `day_articles` is the shared fetch for `digest_date`."""
    if describe is _USE_DEFAULT:
        describe = _DEFAULT_DESCRIBE
    seen = habr_store.get_viewed_ids(user_id)
    subscriptions = habr_store.list_subscriptions(user_id)
    site = feed.select_digest_site(day_articles, seen, digest_date)
    hubs = (feed.select_digest_hubs(day_articles, subscriptions, seen, digest_date, site[:feed.DIGEST_TOP])
            if subscriptions else [])
    if not site and not hubs:
        return {'parts': render_empty_day(digest_date), 'list_ids': []}

    descriptions = _descriptions(site[:feed.DIGEST_TOP] + hubs[:feed.DIGEST_TOP], describe)
    parts, list_ids = [], []
    for kind, header, items in (('digest_site', TEXT_HEADER_SITE, site), ('digest_hubs', TEXT_HEADER_HUBS, hubs)):
        if not items:
            continue
        # The full ranked list is persisted so paging continues from position 6
        list_id = feed.create_list(user_id, kind, digest_date, items)
        list_ids.append(list_id)
        parts.append(_render_part(header, list_id, items, descriptions))
    text, rows = parts[-1]
    parts[-1] = (_with_footer(text), rows)
    return {'parts': parts, 'list_ids': list_ids}


# =============================================================================
# Dry run (no Telegram, temp DB only)
# =============================================================================


def _owner_id() -> int:
    try:
        return int(os.environ.get('OWNER_ID', ''))
    except ValueError:
        return DRY_RUN_FAKE_OWNER_ID


def _copy_db(source: str, target: str) -> None:
    """Consistent copy of the live DB (read-only open + sqlite backup, WAL-safe)."""
    src = sqlite3.connect(f'file:{os.path.abspath(source)}?mode=ro', uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _print(text: str) -> None:
    """Prints to stdout; characters the console cannot encode are replaced, not fatal."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or 'utf-8'
        print(text.encode(encoding, errors='replace').decode(encoding))


def _print_result(result: dict) -> None:
    for text, _ in result['parts']:
        _print(text)
        _print('-' * 40)
    for list_id in result['list_ids']:
        page = render_page(list_id, 0) if habr_store.get_ranked_list(list_id, limit=0)['total'] > feed.DIGEST_TOP \
            else None
        if page is not None:
            _print(f'[Ещё 10 ▶, list {list_id}]')
            _print(page[0])
            _print('-' * 40)


def dry_run(describe=_USE_DEFAULT, *, now: datetime | None = None) -> dict | None:
    """
    Builds and prints the owner's digest for yesterday (MSK) on a temporary DB copy:
    nothing is sent, the production DB is never written, no digest_runs/deliveries rows.
    Returns the build result, or None if Habr is unavailable (short reason printed).
    """
    if describe is _USE_DEFAULT:
        describe = _DEFAULT_DESCRIBE
    day = digest_day(now or feed._now())
    owner_id = _owner_id()
    saved_path, saved_dir = database.DB_PATH, database.DATA_DIR
    with tempfile.TemporaryDirectory(prefix='digest_dry_run_', ignore_cleanup_errors=True) as tmp:
        temp_path = os.path.join(tmp, 'study_agent.db')
        if os.path.isfile(saved_path):
            _copy_db(saved_path, temp_path)
        try:
            database.DATA_DIR, database.DB_PATH = tmp, temp_path
            database.init_db()
            if not habr_store.list_subscriptions(owner_id):
                for alias in DRY_RUN_HUBS:
                    habr_store.add_subscription(owner_id, alias)
            try:
                articles = fetch_day_articles(day, wait_timeout=None)
            except habr_api.HabrUnavailable as e:
                reason = e.kind + (f' (HTTP {e.status})' if e.status is not None else '')
                _print(TEXT_HABR_UNAVAILABLE.format(reason=reason))
                return None
            _print(f'Дайджест за {feed.format_date_ru(day)}: статей за день {len(articles)}, '
                   f'получатель {owner_id}')
            _print('=' * 40)
            result = build_user_digest(owner_id, articles, day, describe=describe)
            _print_result(result)
            return result
        finally:
            database.DB_PATH, database.DATA_DIR = saved_path, saved_dir
