"""
Hub catalog and subscriptions logic (Decision 14); no Telegram code.

    - ensure_catalog(): keeps the stored catalog no older than CATALOG_MAX_AGE,
      refreshing it from habr_api.fetch_hub_catalog (handler wait mode, single-flight);
    - resolve(args): `/subscribe` tokens (bare aliases or habr.com hub URLs) ->
      canonical aliases + not-found tokens; format_not_found() builds the echo;
    - subscribe / unsubscribe / list_subscriptions on top of habr_store;
    - top_hubs(): top-30 by rating for the argument-less `/subscribe` keyboard;
    - hub callback data `hs:a:{alias}` / `hs:d:{alias}`: build and parse.

Refresh failure contract (R2): with a stale copy, HabrBusy and HabrUnavailable both
return silently (stale copy used); with no copy, HabrBusy propagates and
HabrUnavailable becomes CatalogUnavailable.

Only collective hubs with aliases matching habr_store.HUB_ALIAS_RE are imported.
Matching is case-insensitive; the canonical case from the catalog is stored and
returned (Habr aliases are case-sensitive: `1C`, `DIY`).
"""

import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import urlsplit

import habr_api
import habr_store
from feed import CALLBACK_MAX_BYTES, strip_control_bidi
from habr_store import HUB_ALIAS_RE

logger = logging.getLogger(__name__)

CATALOG_MAX_AGE = timedelta(days=7)
# After a failed refresh with a stale copy, handlers use the copy without retrying
# Habr for this long (each retry could hold a handler worker for the throttle wait)
REFRESH_RETRY_AFTER = timedelta(minutes=10)
# Upper bound of what habr_api.fetch_hub_catalog can return (pages x per page)
_CATALOG_LOAD_LIMIT = habr_api.HUBS_MAX_PAGES * habr_api.HUBS_PER_PAGE
TOP_HUBS = 30
NOT_FOUND_MAX_TOKENS = 10
NOT_FOUND_TOKEN_MAX_LEN = 40

TEXT_NOT_FOUND = 'Не найдены: {tokens}'

_HUB_HOSTS = frozenset({'habr.com', 'www.habr.com'})
# Optional language prefix, `hubs`, the alias segment, then any trailing path
_HUB_PATH_RE = re.compile(r'(?:/[a-z]{2})?/hubs/([^/]+)(?:/.*)?', re.DOTALL)
# The alias part is checked separately with HUB_ALIAS_RE; `.` excludes "\n"
_HUB_CALLBACK_RE = re.compile(r'hs:([ad]):(.+)')
_TOKEN_SPLIT_RE = re.compile(r'[\s,]+')


class CatalogUnavailable(Exception):
    """Habr is unavailable and there is no stored catalog copy to fall back to."""


def _now() -> datetime:
    """Single clock source (aware UTC); patched in tests."""
    return datetime.now(timezone.utc)


# =============================================================================
# Catalog freshness (single-flight refresh)
# =============================================================================

_refresh_lock = threading.Lock()
_refresh_generation = 0                    # incremented after every refresh attempt
_last_refresh_error: Exception | None = None
_last_failure_at: datetime | None = None   # last failed refresh while a stale copy existed


def _catalog_state(now: datetime) -> tuple[bool, bool]:
    """(has_copy, is_fresh) of the stored catalog."""
    fetched_at = habr_store.catalog_fetched_at()
    if fetched_at is None:
        return False, False
    try:
        age = now - datetime.fromisoformat(fetched_at)
    except (TypeError, ValueError):
        return True, False
    return True, age <= CATALOG_MAX_AGE


def _import_items(items: list[dict]) -> list[dict]:
    """Keeps only collective hubs with a safe alias; the rest is logged and dropped."""
    kept = []
    for item in items:
        alias = item.get('alias')
        if item.get('hub_type') != 'collective':
            logger.warning('Hub catalog: dropped non-collective hub %.60r (%.20r)', alias, item.get('hub_type'))
            continue
        if not isinstance(alias, str) or not HUB_ALIAS_RE.fullmatch(alias):
            logger.warning('Hub catalog: dropped unsafe alias %.60r', alias)
            continue
        kept.append(item)
    return kept


def ensure_catalog(now: datetime | None = None) -> None:
    """
    Makes sure a usable catalog is stored. Refreshes it when older than
    CATALOG_MAX_AGE; concurrent callers wait for the running refresh and reuse its
    outcome (one Habr request). One handler deadline (habr_api.HANDLER_DEADLINE) bounds
    the whole call: the refresh-lock wait (also capped by HANDLER_WAIT_TIMEOUT) plus
    every catalog page. Raises habr_api.HabrBusy or CatalogUnavailable only when there
    is no stored copy.
    """
    global _refresh_generation, _last_refresh_error, _last_failure_at
    now = now or _now()
    has_copy, fresh = _catalog_state(now)
    if fresh:
        return
    failed_at = _last_failure_at
    if has_copy and failed_at is not None and timedelta(0) <= now - failed_at < REFRESH_RETRY_AFTER:
        return
    generation = _refresh_generation
    deadline = habr_api.handler_deadline()
    if not _refresh_lock.acquire(timeout=habr_api.wait_budget(habr_api.HANDLER_WAIT_TIMEOUT, deadline)):
        if habr_store.catalog_fetched_at() is not None:
            return
        raise habr_api.HabrBusy('hub catalog refresh is already running')
    try:
        if _refresh_generation != generation:
            # Another caller refreshed while we waited: reuse its outcome
            if habr_store.catalog_fetched_at() is not None:
                return
            _raise_no_copy(_last_refresh_error)
        has_copy, fresh = _catalog_state(now)
        if fresh:
            return
        error = None
        try:
            items = habr_api.fetch_hub_catalog(wait_timeout=habr_api.HANDLER_WAIT_TIMEOUT, deadline=deadline)
            # The HTTP call is over: the DB transaction opens only now
            stored = habr_store.replace_hub_catalog(_import_items(items))
            if stored == 0:
                error = habr_api.HabrUnavailable('invalid_shape', detail='no valid collective hubs')
        except (habr_api.HabrBusy, habr_api.HabrUnavailable) as e:
            error = e
        _last_refresh_error = error
        _refresh_generation += 1
        if error is None:
            _last_failure_at = None
            return
        if has_copy:
            _last_failure_at = now
            logger.warning('Hub catalog refresh failed, stale copy used: %s', error)
            return
        _raise_no_copy(error)
    finally:
        _refresh_lock.release()


def _raise_no_copy(error: Exception | None) -> None:
    if isinstance(error, habr_api.HabrBusy):
        raise habr_api.HabrBusy(str(error)) from error
    raise CatalogUnavailable('hub catalog unavailable and no stored copy') from error


def _reset_for_tests() -> None:
    """Resets the refresh lock and the shared refresh outcome."""
    global _refresh_lock, _refresh_generation, _last_refresh_error, _last_failure_at
    _refresh_lock = threading.Lock()
    _refresh_generation = 0
    _last_refresh_error = None
    _last_failure_at = None


# =============================================================================
# Resolving /subscribe arguments
# =============================================================================


class Resolution(NamedTuple):
    found: list[str]          # canonical aliases, deduplicated, input order
    not_found: list[str]      # raw tokens, deduplicated, input order


def _alias_from_url(token: str) -> str | None:
    """Alias from a habr.com / www.habr.com hub URL (scheme optional); None otherwise."""
    candidate = token
    lowered = token.lower()
    if '://' not in token:
        if not lowered.startswith(('habr.com/', 'www.habr.com/')):
            return None
        candidate = 'https://' + token
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in ('http', 'https'):
        return None
    # Userinfo and ports are rejected: only the exact hosts count
    if '@' in parts.netloc or ':' in parts.netloc or parts.netloc.lower() not in _HUB_HOSTS:
        return None
    match = _HUB_PATH_RE.fullmatch(parts.path)
    return match.group(1) if match else None


def _looks_like_url(token: str) -> bool:
    return '/' in token or '://' in token


def resolve(args: str) -> Resolution:
    """
    Splits `/subscribe` arguments on whitespace/commas and resolves each token
    (hub URL or bare alias) case-insensitively against the stored catalog.
    The caller runs ensure_catalog() first. One catalog read, matching in memory,
    so the cost does not grow with DB round-trips per token. No network access.
    """
    found: list[str] = []
    not_found: list[str] = []
    tokens = [t for t in _TOKEN_SPLIT_RE.split(args or '') if t]
    if not tokens:
        return Resolution(found, not_found)
    catalog = _catalog_by_lower()
    for token in dict.fromkeys(tokens):
        alias = _alias_from_url(token) if _looks_like_url(token) else token
        canonical = catalog.get(alias.lower()) if alias and HUB_ALIAS_RE.fullmatch(alias) else None
        if canonical is None:
            not_found.append(token)
        elif canonical not in found:
            found.append(canonical)
    return Resolution(found, not_found)


def _catalog_by_lower() -> dict[str, str]:
    """lower(alias) -> canonical alias; on a case-only clash the smallest alias wins (as find_hub)."""
    result: dict[str, str] = {}
    for hub in habr_store.top_hubs(_CATALOG_LOAD_LIMIT):
        alias = hub['alias']
        key = alias.lower()
        if key not in result or alias < result[key]:
            result[key] = alias
    return result


def format_not_found(tokens: list[str]) -> str:
    """'Не найдены: a, b' with at most 10 sanitized tokens of <= 40 chars; '' if none."""
    shown = []
    for token in tokens[:NOT_FOUND_MAX_TOKENS]:
        clean = ' '.join(strip_control_bidi(token).split())
        if len(clean) > NOT_FOUND_TOKEN_MAX_LEN:
            clean = clean[:NOT_FOUND_TOKEN_MAX_LEN - 1] + '…'
        if clean:
            shown.append(clean)
    if not shown:
        return ''
    text = ', '.join(shown)
    if len(tokens) > NOT_FOUND_MAX_TOKENS:
        text += ', …'
    return TEXT_NOT_FOUND.format(tokens=text)


# =============================================================================
# Subscriptions
# =============================================================================


def subscribe(user_id: int, aliases: list[str]) -> list[str]:
    """Subscribes to catalog-resolved aliases (idempotent); returns the newly added ones."""
    return [alias for alias in aliases if habr_store.add_subscription(user_id, alias)]


def unsubscribe(user_id: int, alias: str) -> bool:
    return habr_store.remove_subscription(user_id, alias)


def list_subscriptions(user_id: int) -> list[str]:
    return habr_store.list_subscriptions(user_id)


def top_hubs(n: int = TOP_HUBS) -> list[dict]:
    """Top-n catalog hubs by rating DESC, ties by alias (stable)."""
    return habr_store.top_hubs(n)


# =============================================================================
# Hub callback data
# =============================================================================


def _hub_callback(action: str, alias: str) -> str:
    if not isinstance(alias, str) or not HUB_ALIAS_RE.fullmatch(alias):
        raise ValueError(f'unsafe hub alias: {alias!r:.60}')
    data = f'hs:{action}:{alias}'
    if len(data.encode('utf-8')) > CALLBACK_MAX_BYTES:
        raise ValueError('hub callback data longer than 64 bytes')
    return data


def subscribe_callback(alias: str) -> str:
    return _hub_callback('a', alias)


def unsubscribe_callback(alias: str) -> str:
    return _hub_callback('d', alias)


def parse_hub_callback(data, user_id: int) -> tuple[str, str] | None:
    """
    `hs:a:{alias}` -> ('a', canonical alias) if the alias is in the catalog;
    `hs:d:{alias}` -> ('d', alias) if the alias is in the user's own subscriptions
    (so a hub dropped from the catalog can still be removed). Anything else -> None,
    logged at WARNING with the user id.
    """
    match = _HUB_CALLBACK_RE.fullmatch(data) if isinstance(data, str) else None
    if match is None or not HUB_ALIAS_RE.fullmatch(match.group(2)):
        return _reject(user_id, data, 'format')
    action, alias = match.groups()
    if action == 'a':
        hub = habr_store.find_hub(alias)
        if hub is None:
            return _reject(user_id, data, 'not in catalog')
        return 'a', hub['alias']
    if alias not in habr_store.list_subscriptions(user_id):
        return _reject(user_id, data, 'not subscribed')
    return 'd', alias


def _reject(user_id: int, data, reason: str) -> None:
    logger.warning('Stale hub button: user_id=%s, reason=%s, data=%.40r', user_id, reason, data)
    return None


# =============================================================================
# Presentation for the bot: texts and keyboards as data (rows of (text, data))
# =============================================================================

HUB_BUTTONS_PER_ROW = 2
HUB_BUTTON_TITLE_MAX_LEN = 30
SUBSCRIPTIONS_PER_MESSAGE = 50     # ✖ buttons per /subscriptions message
SUBSCRIBED_PER_PARAGRAPH = 50      # aliases per paragraph of the /subscribe reply

TEXT_TOP_HUBS = 'Выбери хаб для подписки (топ-30 по рейтингу):'
TEXT_SUBSCRIBED = 'Подписка оформлена: {hubs}'
TEXT_SUBSCRIPTIONS = 'Твои подписки — нажми ✖, чтобы отписаться:'
TEXT_SUBSCRIPTIONS_MORE = 'Ещё подписки:'
TEXT_SUBSCRIPTIONS_EMPTY = 'Подписок пока нет — /subscribe'
TEXT_SUBSCRIPTIONS_REFRESHED = 'Список подписок обновлён — /subscriptions'
TEXT_UNSUBSCRIBE_BUTTON = '✖ {alias}'

Rows = list[list[tuple[str, str]]]


def _in_rows(buttons: list[tuple[str, str]], per_row: int) -> Rows:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _hub_button_text(hub: dict) -> str:
    """Catalog title (plain text, one line, short) or the alias when the title is unusable."""
    title = ' '.join(strip_control_bidi(hub.get('title') or '').split())
    if not title:
        return hub['alias']
    if len(title) > HUB_BUTTON_TITLE_MAX_LEN:
        title = title[:HUB_BUTTON_TITLE_MAX_LEN - 1].rstrip() + '…'
    return title


def top_hubs_rows(n: int = TOP_HUBS) -> Rows:
    """Subscribe buttons for the top-n hubs by rating (unsafe aliases skipped)."""
    buttons = []
    for hub in top_hubs(n):
        try:
            buttons.append((_hub_button_text(hub), subscribe_callback(hub['alias'])))
        except ValueError:
            logger.warning('Hub catalog: unsafe alias skipped in keyboard %.60r', hub.get('alias'))
    return _in_rows(buttons, HUB_BUTTONS_PER_ROW)


def format_subscribed(aliases: list[str]) -> str:
    """
    `/subscribe` confirmation ('' for no aliases). Aliases are grouped into paragraphs, so
    bot.send_long_message can split a huge reply into messages under 4096 chars.
    """
    if not aliases:
        return ''
    groups = [', '.join(aliases[i:i + SUBSCRIBED_PER_PARAGRAPH])
              for i in range(0, len(aliases), SUBSCRIBED_PER_PARAGRAPH)]
    return TEXT_SUBSCRIBED.format(hubs='\n\n'.join(groups))


def unsubscribe_rows(aliases: list[str]) -> Rows:
    buttons = [(TEXT_UNSUBSCRIBE_BUTTON.format(alias=a), unsubscribe_callback(a)) for a in aliases]
    return _in_rows(buttons, HUB_BUTTONS_PER_ROW)


def subscription_pages(aliases: list[str]) -> list[tuple[str, Rows]]:
    """
    `/subscriptions` messages: at most SUBSCRIPTIONS_PER_MESSAGE ✖ buttons each
    (there is no subscription limit). No subscriptions -> one empty-state message.
    """
    if not aliases:
        return [(TEXT_SUBSCRIPTIONS_EMPTY, [])]
    pages = []
    for start in range(0, len(aliases), SUBSCRIPTIONS_PER_MESSAGE):
        text = TEXT_SUBSCRIPTIONS if start == 0 else TEXT_SUBSCRIPTIONS_MORE
        pages.append((text, unsubscribe_rows(aliases[start:start + SUBSCRIPTIONS_PER_MESSAGE])))
    return pages
