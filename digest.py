"""
Daily Habr digest (Decisions 5-12): build/render, delivery state machine, scheduler loop,
/digest_now runner. No Telegram markup code and no `bot` import: messages go out through an
injected `send(chat_id, text, rows)` whose `rows` are keyboard data.

Delivery API (consumed by bot.py, Task 11):
    run_scheduler(send, owner_id, is_allowed)
        Daemon-thread body: closes previous-day runs, then tick() every 60 s.
    tick(now, *, send, owner_id, is_allowed, describe=_USE_DEFAULT) -> None
        One scheduler step for an aware `now`.
    start_digest_now(send, owner_id, final) -> bool
        Owner's digest in the background; False = already running (send TEXT_BUSY).

Build API:
    digest_day(now) -> date
        The digest day: yesterday by MSK relative to `now`.
    day_bounds(day) -> (start_utc, end_utc)
        UTC window [D 00:00 MSK, D+1 00:00 MSK) of the digest day (feed.msk_day_bounds).
    fetch_day_articles(day, wait_timeout=None) -> list[dict]
        Network step, run ONCE per digest for all recipients: candidates of the day from
        the Habr RSS feeds (site top + all subscribed hubs), full metadata per article via
        kek, keeps the items inside the window and upserts their metadata.
        HabrUnavailable / HabrBusy propagate.
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
import re
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Callable

import database
import feed
import habr_api
import habr_store
import summarizer

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

DESCRIPTION_LIMIT = 250          # lead fallback cut
DESCRIPTION_MAX_LINES = 3        # accepted describe output (Decision 8)
DESCRIPTION_MAX_CHARS = 400
SUMMARY_BUTTONS_PER_ROW = 3      # "Саммари N" is long: 3 + 2 keeps labels readable on phones
DRY_RUN_HUBS = ('python', 'programming', 'machine_learning')
DRY_RUN_FAKE_OWNER_ID = 1        # used only when OWNER_ID is unset; the temp DB makes it safe

Describe = Callable[[str, str], 'str | None']

_USE_DEFAULT = object()
# Resolved at call time by build_user_digest / dry_run / tick / start_digest_now
_DEFAULT_DESCRIBE: Describe | None = summarizer.generate_short_description


# =============================================================================
# Digest day
# =============================================================================


def _now() -> datetime:
    """Clock source: aware datetime in MSK. Patched in tests."""
    return datetime.now(feed.MSK)


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
    # Candidates: the site top feeds (site list) + every subscribed hub's feed (hubs lists)
    items = habr_api.fetch_articles_since(start, until=end, hubs=habr_store.list_all_subscribed_hubs(),
                                          top=True, wait_timeout=wait_timeout)
    # kek published_at may differ from the RSS pubDate: the window is re-checked on kek data
    day_items = [i for i in items if start <= i['published_at'] < end]
    habr_store.upsert_habr_articles(day_items)
    return day_items


# =============================================================================
# Descriptions (Decision 8)
# =============================================================================


def _clean_description(value) -> str | None:
    """
    Accepted describe/stored value -> plain lines without control/bidi chars; else None.
    Defense in depth (summarizer validates too): a description is broadcast to every
    recipient, so one with URLs (also bare domains), @mentions, /commands, > 3 lines or > 400 chars is rejected.
    """
    if not isinstance(value, str):
        return None
    lines = [' '.join(line.split()) for line in feed.strip_control_bidi(value).splitlines()]
    lines = [line for line in lines if line]
    text = '\n'.join(lines)
    if not text or len(lines) > DESCRIPTION_MAX_LINES or len(text) > DESCRIPTION_MAX_CHARS:
        return None
    if feed.is_unsafe_description(text):
        return None
    return text


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
            if text is not None and not habr_store.set_short_description(habr_id, text):
                # Precondition: day_articles were upserted (fetch_day_articles does it)
                logger.warning('Short description not cached, no metadata row: habr_id=%s', habr_id)
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
    """Both parts empty: one message without a keyboard. `digest_date` is part of the
    fixed Task 10 API (the text itself says "вчера" and does not need it)."""
    return [(_with_footer(TEXT_EMPTY_DAY), None)]


def render_page(list_id: int, offset: int) -> tuple[str, feed.Rows] | None:
    """Paging page of a persisted digest list (used by dry_run); the last page ends with
    'Это все статьи за …'."""
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
    # as_uri() percent-encodes the path: '?' or '#' in it cannot change the URI
    src = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)
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
    day = digest_day(now or _now())
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


# =============================================================================
# Delivery: time settings, texts, injectable hooks (Decisions 9-12)
# =============================================================================

DEFAULT_DIGEST_TIME = dtime(18, 0)
SKIP_TIME = dtime(23, 59)               # an unfinished run is skipped at this MSK time
RETRY_DELAY = timedelta(minutes=15)
WARN_AFTER = timedelta(hours=3)
TICK_SECONDS = 60
RECIPIENT_PAUSE = 0.1                   # seconds between recipients (Telegram flood limits)
RETRY_AFTER_DEFAULT = 5                 # 429 without a usable retry_after
RETRY_AFTER_MAX = 60                    # a longer 429 wait fails the delivery instead

TEXT_BUSY = 'Уже собираю дайджест'
TEXT_OWNER_WARNING = 'Дайджест за сегодня ещё не разослан: {reason}'
TEXT_OWNER_SKIPPED = 'Дайджест за {date} пропущен: {reason}'
TEXT_DIGEST_NOW_FAILED = 'Дайджест не собран: {reason}'
REASON_BOT_DOWN = 'бот не работал'

# Short owner-facing categories (never raw exception text: it may carry URLs or tokens)
REASON_HTTP = 'Хабр не ответил (HTTP {status})'
REASON_PARTIAL = 'Хабр вернул неполный список'
REASON_TIMEOUT = 'Хабр не ответил вовремя'
REASON_NETWORK = 'Хабр недоступен (ошибка сети)'
REASON_BAD_RESPONSE = 'Хабр вернул некорректный ответ'
REASON_INTERNAL = 'внутренняя ошибка бота'

_DIGEST_TIME_RE = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])')

Send = Callable[[int, str, 'list | None'], None]

_sleep = time.sleep                     # patched in tests (429 waits, pauses, the loop)

# One build at a time: /digest_now and the scheduled run never describe the same article twice
_BUILD_LOCK = threading.Lock()
_digest_now_lock = threading.Lock()
_digest_now_running = False
_cleanup_done_for: date | None = None


def _spawn(target: Callable[[], None]) -> None:
    """Starts the /digest_now work in a daemon thread. Patched in tests."""
    threading.Thread(target=target, daemon=True, name='digest-now').start()


def _reset_for_tests() -> None:
    """Clears module state shared across tests (conftest autouse fixture)."""
    global _digest_now_running, _cleanup_done_for
    with _digest_now_lock:
        _digest_now_running = False
    _cleanup_done_for = None


def digest_time() -> dtime:
    """DIGEST_TIME (strict HH:MM, MSK), read at call time; invalid -> 18:00 + warning.

    A time at or after SKIP_TIME is invalid too: the run would start when the day is
    already skipped, so no digest would ever be created.
    """
    raw = os.environ.get('DIGEST_TIME')
    if not raw:
        return DEFAULT_DIGEST_TIME
    match = _DIGEST_TIME_RE.fullmatch(raw)
    value = dtime(int(match.group(1)), int(match.group(2))) if match else None
    if value is None or value >= SKIP_TIME:
        logger.warning('Invalid DIGEST_TIME %r, using %s', raw[:20], DEFAULT_DIGEST_TIME.strftime('%H:%M'))
        return DEFAULT_DIGEST_TIME
    return value


def _at(day: date, moment: dtime) -> datetime:
    return datetime.combine(day, moment, tzinfo=feed.MSK)


def _parse_ts(value) -> datetime | None:
    """Stored aware ISO timestamp -> datetime; anything unusable -> None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def failure_reason(exc: BaseException) -> str:
    """Short owner-facing category for an attempt failure."""
    if isinstance(exc, habr_api.HabrUnavailable):
        if exc.kind == 'partial':
            return REASON_PARTIAL
        if exc.kind == 'http' and isinstance(exc.status, int):
            return REASON_HTTP.format(status=exc.status)
        if exc.kind == 'timeout':
            return REASON_TIMEOUT
        if exc.kind == 'network':
            return REASON_NETWORK
        return REASON_BAD_RESPONSE
    return REASON_INTERNAL


def _notify_owner(send: Send, owner_id: int, text: str) -> None:
    """Owner alert through the same `send`; a failure is logged and never breaks the caller."""
    try:
        send(owner_id, text, None)
    except Exception as e:  # noqa: BLE001
        logger.warning('Owner alert failed: %s', type(e).__name__)


# =============================================================================
# Telegram error policy (Decision 11)
# =============================================================================

SEND_OK = 'sent'
SEND_BLOCKED = 'blocked'
SEND_FAILED = 'failed'


def _telegram_error(exc: BaseException):
    """The ApiTelegramException or None. telebot is imported lazily (isinstance only), so
    importing digest pulls in neither telebot nor bot."""
    from telebot.apihelper import ApiTelegramException
    return exc if isinstance(exc, ApiTelegramException) else None


def _is_blocked(exc: BaseException) -> bool:
    err = _telegram_error(exc)
    if err is None:
        return False
    if err.error_code == 403:
        return True
    return err.error_code == 400 and 'chat not found' in str(err.description or '').lower()


def _retry_after(exc: BaseException) -> float | None:
    """Seconds to wait for a 429, else None. A missing/invalid retry_after -> small default;
    one above RETRY_AFTER_MAX -> None (no retry: the scheduler thread is never stalled)."""
    err = _telegram_error(exc)
    if err is None or err.error_code != 429:
        return None
    result_json = err.result_json if isinstance(err.result_json, dict) else {}
    params = result_json.get('parameters')
    value = params.get('retry_after') if isinstance(params, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return RETRY_AFTER_DEFAULT
    return value if value <= RETRY_AFTER_MAX else None


def _send_parts(send: Send, chat_id: int, parts: list) -> str:
    """
    Sends all digest messages to one chat. Returns SEND_OK, SEND_BLOCKED (403 / 400 chat
    not found) or SEND_FAILED. A 429 is retried once per recipient after retry_after.
    A partial send (a later part fails) is SEND_FAILED and is not resent.
    """
    retried = False
    for text, rows in parts:
        while True:
            try:
                send(chat_id, text, rows)
                break
            except Exception as e:  # noqa: BLE001 — every error is classified, nothing propagates
                if _is_blocked(e):
                    return SEND_BLOCKED
                wait = _retry_after(e)
                if wait is not None and not retried:
                    retried = True
                    logger.info('Digest send 429: user_id=%s, retry in %ss', chat_id, wait)
                    _sleep(wait)
                    continue
                logger.error('Digest send failed: user_id=%s, %s', chat_id, type(e).__name__)
                return SEND_FAILED
    return SEND_OK


# =============================================================================
# Scheduler tick (Decisions 9, 10)
# =============================================================================


def _recipients(owner_id: int, is_allowed) -> list[int]:
    """Owner + every known user that passes the injected access rule (`is_allowed`, the same
    check re-run at send time), minus digest disabled (missing setting = enabled).

    Errors propagate on purpose: a DB error (e.g. 'database is locked') aborts the tick
    before the snapshot and the run row exist, so the next tick retries instead of
    silently leaving the user out of today's run.
    """
    ids = [owner_id] + [u['user_id'] for u in database.list_users() if u['user_id'] != owner_id]
    return [uid for uid in ids if is_allowed(uid) and habr_store.get_digest_enabled(uid)]


def _can_receive(user_id: int, is_allowed) -> bool:
    """Send-time re-check: a broken check never sends (fail closed)."""
    try:
        return bool(is_allowed(user_id)) and habr_store.get_digest_enabled(user_id)
    except Exception as e:  # noqa: BLE001 — a broken check never sends
        logger.warning('Recipient check failed: user_id=%s, %s', user_id, type(e).__name__)
        return False


def _housekeeping(now: datetime) -> None:
    """Once per MSK day: delete ranked lists older than 30 days."""
    global _cleanup_done_for
    today = now.date()
    if _cleanup_done_for == today:
        return
    _cleanup_done_for = today
    try:
        habr_store.cleanup_ranked_lists(now)
    except Exception:  # noqa: BLE001
        logger.exception('Ranked lists cleanup failed')


def _skip_run(run_id: str, now: datetime, reason: str, send: Send, owner_id: int) -> None:
    habr_store.update_run(run_id, status='skipped', finished_at=now)
    logger.info('Digest run skipped: %s, %s', run_id, reason)
    _notify_owner(send, owner_id, TEXT_OWNER_SKIPPED.format(
        date=feed.format_date_ru(date.fromisoformat(run_id)), reason=reason))


def _finish_if_done(run_id: str, now: datetime) -> bool:
    # 'sending' rows count as finished: after a restart they are treated as sent
    if habr_store.list_deliveries(run_id, status='pending'):
        return False
    habr_store.update_run(run_id, status='done', finished_at=now)
    logger.info('Digest run done: %s', run_id)
    return True


def _deliver_one(run_id: str, user_id: int, articles: list[dict], day: date, describe,
                 send: Send, is_allowed) -> None:
    if not _can_receive(user_id, is_allowed):
        habr_store.set_delivery_status(run_id, user_id, 'failed')
        logger.info('Digest not delivered, access revoked or digest off: user_id=%s', user_id)
        return
    # Cheap pre-check: a row that stopped being pending (e.g. /digest_now final) costs
    # no build/LLM work
    if not habr_store.is_delivery_pending(run_id, user_id):
        return
    # The build (LLM calls, tens of seconds) runs while the row is still 'pending': a crash
    # or restart here leaves it pending, so it is retried; a build error is marked 'failed'
    # by _attempt
    with _BUILD_LOCK:
        parts = build_user_digest(user_id, articles, day, describe=describe)['parts']
    # Atomic claim right before sending (pending -> sending): never sent twice
    if not habr_store.claim_delivery(run_id, user_id):
        logger.info('Digest delivery taken while building, not sent: user_id=%s', user_id)
        return
    outcome = _send_parts(send, user_id, parts)
    if outcome == SEND_OK:
        habr_store.set_delivery_status(run_id, user_id, 'sent')
        return
    habr_store.set_delivery_status(run_id, user_id, 'failed')
    if outcome == SEND_BLOCKED:
        habr_store.set_digest_enabled(user_id, False, reason='blocked')
        logger.warning('Digest disabled, bot blocked or chat not found: user_id=%s', user_id)


def _attempt(run: dict, now: datetime, send: Send, is_allowed, describe) -> None:
    """One delivery attempt: the shared fetch, then every pending recipient. HTTP and LLM
    calls run between short store calls, never inside an open transaction."""
    run_id = run['digest_date']
    if _finish_if_done(run_id, now):
        return
    next_at = _parse_ts(run.get('next_attempt_at'))
    if next_at is not None and now < next_at:
        return
    day = digest_day(now)
    try:
        # Background call: default wait_timeout=None (never HANDLER_WAIT_TIMEOUT)
        articles = fetch_day_articles(day)
    except Exception as e:  # noqa: BLE001 — any fetch failure is retried in 15 min
        reason = failure_reason(e)
        habr_store.update_run(run_id, last_error=reason, next_attempt_at=now + RETRY_DELAY)
        if isinstance(e, habr_api.HabrUnavailable):
            logger.info('Digest attempt failed: %s, %s', run_id, reason)
        else:
            logger.exception('Digest attempt failed: %s, %s', run_id, reason)
        return
    for delivery in habr_store.list_deliveries(run_id, status='pending'):
        user_id = delivery['user_id']
        try:
            _deliver_one(run_id, user_id, articles, day, describe, send, is_allowed)
        except Exception:  # noqa: BLE001 — one recipient's error never blocks the others
            logger.exception('Digest delivery failed: user_id=%s', user_id)
            try:
                habr_store.set_delivery_status(run_id, user_id, 'failed')
            except Exception:  # noqa: BLE001
                logger.exception('Could not mark delivery failed: user_id=%s', user_id)
        _sleep(RECIPIENT_PAUSE)
    _finish_if_done(run_id, now)


def tick(now: datetime, *, send: Send, owner_id: int, is_allowed, describe=_USE_DEFAULT) -> None:
    """
    One scheduler step for the aware `now` (converted to MSK).

    send(chat_id, text, rows) -> None: rows are keyboard data (list[list[(text, callback)]])
        or None, passed through unchanged; raises the Telegram exception on failure.
    is_allowed(user_id) -> bool: access gate, re-checked at send time.
    describe: `_USE_DEFAULT` -> `_DEFAULT_DESCRIBE` at call time; None -> lead fallback only.
    """
    if describe is _USE_DEFAULT:
        describe = _DEFAULT_DESCRIBE
    now = now.astimezone(feed.MSK)
    today = now.date()
    _housekeeping(now)
    # A previous-day run still open means no tick landed in its 23:59 minute (a long attempt
    # overran it): skip it now with its own reason; never catch it up
    for stale in habr_store.list_unfinished_runs(before_date=today.isoformat()):
        _skip_run(stale['digest_date'], now, stale.get('last_error') or REASON_BOT_DOWN, send, owner_id)
    start = _at(today, digest_time())
    skip_at = _at(today, SKIP_TIME)
    if now < start:
        return
    run_id = today.isoformat()
    run = habr_store.get_run(run_id)
    if run is None:
        if now >= skip_at:
            return                                   # started too late: no run, no instant skip
        # Snapshot first (INSERT OR IGNORE keeps an owner row already 'sent' by /digest_now
        # final), then the run row: a crash in between only re-snapshots on the next tick
        recipients = _recipients(owner_id, is_allowed)
        habr_store.snapshot_recipients(run_id, recipients)
        habr_store.create_run(run_id)
        logger.info('Digest run created: %s, recipients=%d', run_id, len(recipients))
        run = habr_store.get_run(run_id)
    if run['status'] in habr_store.RUN_FINISHED_STATUSES:
        return
    if now >= skip_at:
        _skip_run(run_id, now, run.get('last_error') or REASON_INTERNAL, send, owner_id)
        return

    _attempt(run, now, send, is_allowed, describe)

    run = habr_store.get_run(run_id)
    if (run['status'] not in habr_store.RUN_FINISHED_STATUSES and not run.get('warned_at')
            and now >= start + WARN_AFTER):
        habr_store.update_run(run_id, warned_at=now)
        _notify_owner(send, owner_id, TEXT_OWNER_WARNING.format(reason=run.get('last_error') or REASON_INTERNAL))


# =============================================================================
# Scheduler loop
# =============================================================================


def close_unfinished_runs(now: datetime, send: Send, owner_id: int) -> int:
    """Startup: every unfinished run of a previous day is skipped (no catch-up)."""
    now = now.astimezone(feed.MSK)
    runs = habr_store.list_unfinished_runs(before_date=now.date().isoformat())
    for run in runs:
        _skip_run(run['digest_date'], now, REASON_BOT_DOWN, send, owner_id)
    return len(runs)


def next_run_time(now: datetime) -> datetime:
    """Next scheduled digest moment (for the startup log)."""
    now = now.astimezone(feed.MSK)
    today = now.date()
    start = _at(today, digest_time())
    if now < start:
        return start
    run = habr_store.get_run(today.isoformat())
    finished = run is not None and run['status'] in habr_store.RUN_FINISHED_STATUSES
    if not finished and now < _at(today, SKIP_TIME):
        return now
    return _at(today + timedelta(days=1), digest_time())


def run_scheduler(send: Send, owner_id: int, is_allowed) -> None:
    """
    Daemon-thread body started by bot.main(): closes previous-day runs, then calls
    tick(_now()) every 60 s forever. Exceptions in a tick are logged; the loop survives.
    describe is omitted, so `_DEFAULT_DESCRIBE` is resolved at every tick.
    """
    try:
        close_unfinished_runs(_now(), send, owner_id)
        logger.info('Digest scheduler started, next run at %s',
                    next_run_time(_now()).strftime('%Y-%m-%d %H:%M MSK'))
    except Exception:  # noqa: BLE001
        logger.exception('Digest scheduler startup step failed')
    while True:
        try:
            tick(_now(), send=send, owner_id=owner_id, is_allowed=is_allowed)
        except Exception:  # noqa: BLE001
            logger.exception('Digest tick failed')
        _sleep(TICK_SECONDS)


# =============================================================================
# /digest_now runner (Decision 12)
# =============================================================================


def _digest_now_work(send: Send, owner_id: int, final: bool) -> None:
    global _digest_now_running
    try:
        now = _now().astimezone(feed.MSK)
        day = digest_day(now)
        try:
            articles = fetch_day_articles(day)
        except Exception as e:  # noqa: BLE001
            reason = failure_reason(e)
            logger.info('/digest_now: fetch failed, %s', reason)
            _notify_owner(send, owner_id, TEXT_DIGEST_NOW_FAILED.format(reason=reason))
            return
        with _BUILD_LOCK:
            parts = build_user_digest(owner_id, articles, day, describe=_DEFAULT_DESCRIBE)['parts']
        if _send_parts(send, owner_id, parts) != SEND_OK:
            logger.warning('/digest_now: send to owner failed')
            return
        if final:
            run_id = now.date().isoformat()
            habr_store.snapshot_recipients(run_id, [owner_id])
            habr_store.set_delivery_status(run_id, owner_id, 'sent')
            logger.info('/digest_now final: owner delivery for %s marked sent', run_id)
    except Exception:  # noqa: BLE001
        logger.exception('/digest_now failed')
        _notify_owner(send, owner_id, TEXT_DIGEST_NOW_FAILED.format(reason=REASON_INTERNAL))
    finally:
        with _digest_now_lock:
            _digest_now_running = False


def start_digest_now(send: Send, owner_id: int, final: bool) -> bool:
    """
    Starts the owner's digest in the background (`_spawn`). Returns False if one is already
    running (the handler then sends TEXT_BUSY). Touches no run/delivery rows, except that
    `final` marks the owner's delivery for today as 'sent' after a successful send.
    """
    global _digest_now_running
    with _digest_now_lock:
        if _digest_now_running:
            return False
        _digest_now_running = True
    try:
        _spawn(lambda: _digest_now_work(send, owner_id, final))
    except Exception:
        with _digest_now_lock:
            _digest_now_running = False
        raise
    return True
