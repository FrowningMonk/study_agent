"""
SQLite storage for Habr data: hub catalog, subscriptions, article metadata,
"seen" events, ranked-list snapshots and digest state.

Tables live in the same DB file as `articles` and are created by `init_schema`,
called from `database.init_db()`. None of them has a foreign key to `articles`
or `users`, so `/wipe` and `delete_article` never touch them (Decision 17).

Every connection is opened through `database._get_connection()` at call time:
tests patch `database.DB_PATH`, and the connection enables `PRAGMA foreign_keys`
needed for the `ranked_list_items` cascade.

Timestamps written here are aware ISO strings in UTC with fixed-width
microseconds, so string comparison is chronological; `list_date` and
`digest_date` are MSK dates `YYYY-MM-DD` supplied by callers.

`habr_articles` rows created by `mark_unavailable` may have NULL metadata
(title, score, published_at...): consumers filter on `unavailable_at IS NULL`.
"""

import logging
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

import database

logger = logging.getLogger(__name__)

# Single source of the hub alias rule: catalog import, callback data, user input.
# Always apply it with .fullmatch(): with .match() the `$` accepts a trailing "\n".
HUB_ALIAS_RE = re.compile(r'^[A-Za-z0-9_-]{1,50}$')

VIEW_SOURCES = ('link', 'digest', 'feed', 'legacy')
LIST_KINDS = ('digest_site', 'digest_hubs', 'feed')
RUN_STATUSES = ('pending', 'done', 'skipped')
RUN_FINISHED_STATUSES = ('done', 'skipped')
DELIVERY_STATUSES = ('pending', 'sending', 'sent', 'failed')
RANKED_LIST_TTL_DAYS = 30

# Fixed whitelist: the only column names update_run may put into SQL
_RUN_UPDATABLE_COLUMNS = ('status', 'last_error', 'next_attempt_at', 'warned_at', 'finished_at')

# SQLite limits bound parameters per statement; stay well below it
_IN_CHUNK = 500

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS habr_hubs (
    alias TEXT PRIMARY KEY,
    title TEXT,
    rating REAL,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS hub_subscriptions (
    user_id INTEGER NOT NULL,
    hub_alias TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, hub_alias)
);

CREATE TABLE IF NOT EXISTS habr_articles (
    habr_id INTEGER PRIMARY KEY,
    title TEXT,
    url TEXT,
    published_at TEXT,
    score INTEGER,
    comments_count INTEGER,
    lead_text TEXT,
    short_description TEXT,
    unavailable_at TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS habr_article_hubs (
    habr_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    hub_type TEXT,
    PRIMARY KEY (habr_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_habr_article_hubs_alias ON habr_article_hubs(alias);

CREATE TABLE IF NOT EXISTS habr_article_tags (
    habr_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (habr_id, tag)
);

CREATE TABLE IF NOT EXISTS article_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    habr_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('link', 'digest', 'feed', 'legacy')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_article_views_user_habr ON article_views(user_id, habr_id);

CREATE TABLE IF NOT EXISTS ranked_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('digest_site', 'digest_hubs', 'feed')),
    list_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ranked_lists_created_at ON ranked_lists(created_at);

CREATE TABLE IF NOT EXISTS ranked_list_items (
    list_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    habr_id INTEGER NOT NULL,
    PRIMARY KEY (list_id, position),
    FOREIGN KEY (list_id) REFERENCES ranked_lists(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS digest_settings (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL,
    disabled_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digest_runs (
    digest_date TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    last_error TEXT,
    next_attempt_at TEXT,
    warned_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS digest_deliveries (
    digest_date TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    sent_at TEXT,
    PRIMARY KEY (digest_date, user_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _to_text(value):
    """Aware datetimes -> ISO string in UTC; other values pass through."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('naive datetime is not allowed, pass an aware one')
        return value.astimezone(timezone.utc).isoformat(timespec='microseconds')
    return value


def _chunks(values: list, size: int = _IN_CHUNK):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _placeholders(n: int) -> str:
    # Only "?" markers are built here, never values
    return ', '.join('?' * n)


# ========================
# Schema and migration
# ========================


def init_schema(conn: sqlite3.Connection) -> None:
    """Creates all Habr tables and indexes (idempotent). Connection comes from init_db."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def migrate_articles(conn: sqlite3.Connection) -> None:
    """
    Network-free migration (Decision 18), idempotent:
    fills articles.habr_id from strictly parsed Habr URLs, then adds one 'legacy'
    view per (user_id, habr_id) that has no view row yet.
    """
    # Function-level import: habr_api is the single strict Habr URL parser, and a
    # module-level import here would put habr_api on the database -> habr_store path.
    import habr_api

    candidates = conn.execute('SELECT id, url FROM articles WHERE habr_id IS NULL').fetchall()
    updates = []
    for row in candidates:
        habr_id = habr_api.parse_article_id(row[1])
        if habr_id is not None:
            updates.append((habr_id, row[0]))
    with conn:
        if updates:
            conn.executemany('UPDATE articles SET habr_id = ? WHERE id = ?', updates)
        inserted = conn.execute(
            """
            INSERT INTO article_views (user_id, habr_id, source, created_at)
            SELECT DISTINCT a.user_id, a.habr_id, 'legacy', ?
            FROM articles a
            WHERE a.habr_id IS NOT NULL
              AND a.user_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM article_views v
                  WHERE v.user_id = a.user_id AND v.habr_id = a.habr_id
              )
            """,
            (_now(),),
        ).rowcount
    if updates or inserted:
        logger.info("Migration: habr_id set for %d articles, legacy views added: %d", len(updates), inserted)


# ========================
# Views ("seen" events)
# ========================


def record_view(user_id: int, habr_id: int, source: str) -> None:
    """
    Records that the user has seen the article (any row excludes it from lists).
    A repeated (user_id, habr_id, source) is not stored again; the first time is kept.
    """
    if source not in VIEW_SOURCES:
        raise ValueError(f'unknown view source: {source!r}')
    conn = database._get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO article_views (user_id, habr_id, source, created_at)
                SELECT ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM article_views WHERE user_id = ? AND habr_id = ? AND source = ?
                )
                """,
                (user_id, habr_id, source, _now(), user_id, habr_id, source),
            )
    finally:
        conn.close()


def get_viewed_ids(user_id: int) -> set[int]:
    """habr_ids the user has any view row for."""
    conn = database._get_connection()
    try:
        rows = conn.execute('SELECT DISTINCT habr_id FROM article_views WHERE user_id = ?', (user_id,))
        return {r[0] for r in rows}
    finally:
        conn.close()


# ========================
# Habr article metadata
# ========================


def _write_article(conn: sqlite3.Connection, item: dict, fetched_at: str) -> None:
    habr_id = item['habr_id']
    # short_description and unavailable_at are deliberately not in the UPDATE set
    conn.execute(
        """
        INSERT INTO habr_articles (
            habr_id, title, url, published_at, score, comments_count, lead_text, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(habr_id) DO UPDATE SET
            title = excluded.title,
            url = excluded.url,
            published_at = excluded.published_at,
            score = excluded.score,
            comments_count = excluded.comments_count,
            lead_text = excluded.lead_text,
            fetched_at = excluded.fetched_at
        """,
        (
            habr_id,
            item.get('title'),
            item.get('url'),
            _to_text(item.get('published_at')),
            item.get('score'),
            item.get('comments'),
            item.get('lead_text'),
            fetched_at,
        ),
    )
    conn.execute('DELETE FROM habr_article_hubs WHERE habr_id = ?', (habr_id,))
    conn.executemany(
        'INSERT OR IGNORE INTO habr_article_hubs (habr_id, alias, hub_type) VALUES (?, ?, ?)',
        [(habr_id, alias, hub_type) for alias, hub_type in item.get('hubs') or ()],
    )
    conn.execute('DELETE FROM habr_article_tags WHERE habr_id = ?', (habr_id,))
    conn.executemany(
        'INSERT OR IGNORE INTO habr_article_tags (habr_id, tag) VALUES (?, ?)',
        [(habr_id, tag) for tag in item.get('tags') or ()],
    )


def upsert_habr_articles(items: Iterable[dict]) -> None:
    """
    Upserts normalized habr_api items (habr_id, title, url, published_at, score,
    comments, hubs [(alias, type)], tags, lead_text) in one transaction.
    Hub and tag sets of each habr_id are replaced; short_description and
    unavailable_at are kept.
    """
    fetched_at = _now()
    conn = database._get_connection()
    try:
        with conn:
            for item in items:
                _write_article(conn, item, fetched_at)
    finally:
        conn.close()


def upsert_habr_article(item: dict) -> None:
    upsert_habr_articles([item])


def get_habr_articles(habr_ids: Iterable[int]) -> dict[int, dict]:
    """Metadata by habr_id with `hubs` ([{alias, hub_type}]) and `tags` ([str]); unknown ids omitted."""
    ids = list(dict.fromkeys(habr_ids))
    result: dict[int, dict] = {}
    if not ids:
        return result
    conn = database._get_connection()
    try:
        for chunk in _chunks(ids):
            marks = _placeholders(len(chunk))
            for row in conn.execute(f'SELECT * FROM habr_articles WHERE habr_id IN ({marks})', chunk):
                result[row['habr_id']] = {**dict(row), 'hubs': [], 'tags': []}
            for row in conn.execute(
                f'SELECT habr_id, alias, hub_type FROM habr_article_hubs WHERE habr_id IN ({marks}) '
                'ORDER BY habr_id, alias',
                chunk,
            ):
                if row['habr_id'] in result:
                    result[row['habr_id']]['hubs'].append({'alias': row['alias'], 'hub_type': row['hub_type']})
            for row in conn.execute(
                f'SELECT habr_id, tag FROM habr_article_tags WHERE habr_id IN ({marks}) ORDER BY habr_id, tag',
                chunk,
            ):
                if row['habr_id'] in result:
                    result[row['habr_id']]['tags'].append(row['tag'])
        return result
    finally:
        conn.close()


def get_habr_article(habr_id: int) -> dict | None:
    return get_habr_articles([habr_id]).get(habr_id)


def mark_unavailable(habr_id: int) -> None:
    """
    Marks an article as deleted/hidden on Habr. If there is no metadata yet, creates
    a bare row with only habr_id and unavailable_at (other columns NULL, fetched_at NULL).
    """
    conn = database._get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO habr_articles (habr_id, unavailable_at) VALUES (?, ?)
                ON CONFLICT(habr_id) DO UPDATE SET unavailable_at = excluded.unavailable_at
                """,
                (habr_id, _now()),
            )
    finally:
        conn.close()


def set_short_description(habr_id: int, text: str) -> bool:
    """Stores the cached short description; False if there is no metadata row."""
    conn = database._get_connection()
    try:
        with conn:
            cur = conn.execute(
                'UPDATE habr_articles SET short_description = ? WHERE habr_id = ?', (text, habr_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


# ========================
# Hub catalog
# ========================


def replace_hub_catalog(hubs: Iterable[dict]) -> int:
    """
    Replaces the whole catalog with `hubs` ({alias, title, rating}) in one transaction.
    Aliases not matching HUB_ALIAS_RE are skipped and logged. Returns the stored count.
    If no valid hub remains, the existing catalog is kept untouched and 0 is returned:
    a broken response must not destroy the stale copy (Decision 14).
    """
    fetched_at = _now()
    rows = []
    skipped = 0
    for hub in hubs:
        alias = hub.get('alias')
        if not isinstance(alias, str) or not HUB_ALIAS_RE.fullmatch(alias):
            skipped += 1
            logger.warning("Hub catalog: skipped unsafe alias %.60r", alias)
            continue
        rows.append((alias, hub.get('title'), hub.get('rating'), fetched_at))
    if not rows:
        logger.warning("Hub catalog: no valid hubs in import (%d skipped), existing catalog kept", skipped)
        return 0
    conn = database._get_connection()
    try:
        with conn:
            conn.execute('DELETE FROM habr_hubs')
            conn.executemany(
                'INSERT OR REPLACE INTO habr_hubs (alias, title, rating, fetched_at) VALUES (?, ?, ?, ?)', rows,
            )
    finally:
        conn.close()
    logger.info("Hub catalog replaced: %d stored, %d skipped", len(rows), skipped)
    return len(rows)


def find_hub(alias: str) -> dict | None:
    """Case-insensitive catalog lookup; returns the row with the canonical alias case."""
    if not isinstance(alias, str) or not HUB_ALIAS_RE.fullmatch(alias):
        return None
    conn = database._get_connection()
    try:
        row = conn.execute(
            'SELECT * FROM habr_hubs WHERE lower(alias) = lower(?) ORDER BY alias LIMIT 1', (alias,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def top_hubs(n: int) -> list[dict]:
    conn = database._get_connection()
    try:
        rows = conn.execute('SELECT * FROM habr_hubs ORDER BY rating DESC, alias LIMIT ?', (n,))
        return [dict(r) for r in rows]
    finally:
        conn.close()


def catalog_fetched_at() -> str | None:
    """Last catalog refresh time (ISO UTC) or None if the catalog is empty."""
    conn = database._get_connection()
    try:
        return conn.execute('SELECT MAX(fetched_at) FROM habr_hubs').fetchone()[0]
    finally:
        conn.close()


# ========================
# Subscriptions
# ========================


def add_subscription(user_id: int, hub_alias: str) -> bool:
    """True if added, False if it already existed. Unsafe aliases raise ValueError."""
    if not isinstance(hub_alias, str) or not HUB_ALIAS_RE.fullmatch(hub_alias):
        raise ValueError(f'unsafe hub alias: {hub_alias!r:.60}')
    conn = database._get_connection()
    try:
        with conn:
            cur = conn.execute(
                'INSERT OR IGNORE INTO hub_subscriptions (user_id, hub_alias, created_at) VALUES (?, ?, ?)',
                (user_id, hub_alias, _now()),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_subscription(user_id: int, hub_alias: str) -> bool:
    conn = database._get_connection()
    try:
        with conn:
            cur = conn.execute(
                'DELETE FROM hub_subscriptions WHERE user_id = ? AND hub_alias = ?', (user_id, hub_alias),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def list_subscriptions(user_id: int) -> list[str]:
    conn = database._get_connection()
    try:
        rows = conn.execute(
            'SELECT hub_alias FROM hub_subscriptions WHERE user_id = ? ORDER BY hub_alias', (user_id,),
        )
        return [r[0] for r in rows]
    finally:
        conn.close()


# ========================
# Ranked lists
# ========================


def create_ranked_list(user_id: int, kind: str, list_date: str, habr_ids: Iterable[int]) -> int:
    """Stores a list snapshot with items at positions 1..N atomically; returns the list id."""
    if kind not in LIST_KINDS:
        raise ValueError(f'unknown ranked list kind: {kind!r}')
    conn = database._get_connection()
    try:
        with conn:
            list_id = conn.execute(
                'INSERT INTO ranked_lists (user_id, kind, list_date, created_at) VALUES (?, ?, ?, ?)',
                (user_id, kind, list_date, _now()),
            ).lastrowid
            conn.executemany(
                'INSERT INTO ranked_list_items (list_id, position, habr_id) VALUES (?, ?, ?)',
                [(list_id, pos, habr_id) for pos, habr_id in enumerate(habr_ids, start=1)],
            )
        return list_id
    finally:
        conn.close()


def get_ranked_list(list_id: int, offset: int = 0, limit: int | None = None) -> dict | None:
    """
    List header plus `total` item count and `items` ([{position, habr_id}]) ordered
    by position, sliced by offset/limit (offset 0 = position 1, limit None = all).
    None if missing. Negative offset/limit raise ValueError.
    """
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError('offset and limit must be non-negative')
    conn = database._get_connection()
    try:
        row = conn.execute('SELECT * FROM ranked_lists WHERE id = ?', (list_id,)).fetchone()
        if row is None:
            return None
        total = conn.execute('SELECT COUNT(*) FROM ranked_list_items WHERE list_id = ?', (list_id,)).fetchone()[0]
        items = conn.execute(
            'SELECT position, habr_id FROM ranked_list_items WHERE list_id = ? ORDER BY position LIMIT ? OFFSET ?',
            (list_id, -1 if limit is None else limit, offset),
        )
        return {**dict(row), 'total': total, 'items': [dict(i) for i in items]}
    finally:
        conn.close()


def cleanup_ranked_lists(now: datetime | None = None, days: int = RANKED_LIST_TTL_DAYS) -> int:
    """Deletes lists older than `days` (items cascade). Returns the number of lists deleted."""
    now = now or datetime.now(timezone.utc)
    cutoff = _to_text(now - timedelta(days=days))
    conn = database._get_connection()
    try:
        with conn:
            deleted = conn.execute('DELETE FROM ranked_lists WHERE created_at < ?', (cutoff,)).rowcount
        if deleted:
            logger.info("Ranked lists cleanup: %d deleted", deleted)
        return deleted
    finally:
        conn.close()


# ========================
# Digest settings
# ========================


def get_digest_enabled(user_id: int) -> bool:
    """Missing row means enabled."""
    conn = database._get_connection()
    try:
        row = conn.execute('SELECT enabled FROM digest_settings WHERE user_id = ?', (user_id,)).fetchone()
        return True if row is None else bool(row[0])
    finally:
        conn.close()


def set_digest_enabled(user_id: int, enabled: bool, reason: str | None = None) -> None:
    """Upserts the setting; `reason` is stored only when disabling."""
    conn = database._get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO digest_settings (user_id, enabled, disabled_reason, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    disabled_reason = excluded.disabled_reason,
                    updated_at = excluded.updated_at
                """,
                (user_id, 1 if enabled else 0, None if enabled else reason, _now()),
            )
    finally:
        conn.close()


# ========================
# Digest runs (run_id == digest_date)
# ========================


def create_run(digest_date: str) -> bool:
    """Creates the run for the date in status 'pending'; False if it already exists."""
    conn = database._get_connection()
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO digest_runs (digest_date, status) VALUES (?, 'pending')", (digest_date,),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def get_run(digest_date: str) -> dict | None:
    conn = database._get_connection()
    try:
        row = conn.execute('SELECT * FROM digest_runs WHERE digest_date = ?', (digest_date,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_unfinished_runs(before_date: str) -> list[dict]:
    """Runs with digest_date < before_date that are neither done nor skipped, oldest first."""
    conn = database._get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM digest_runs
            WHERE digest_date < ? AND status NOT IN ({_placeholders(len(RUN_FINISHED_STATUSES))})
            ORDER BY digest_date
            """,
            (before_date, *RUN_FINISHED_STATUSES),
        )
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_run(run_id: str, **fields) -> bool:
    """
    Updates whitelisted run columns (status, last_error, next_attempt_at, warned_at,
    finished_at). Unknown fields raise ValueError. Column names in the SQL come only
    from the fixed whitelist; values are bound parameters. Returns True if the run exists.
    """
    unknown = set(fields) - set(_RUN_UPDATABLE_COLUMNS)
    if unknown:
        raise ValueError(f'update_run: unknown fields {sorted(map(repr, unknown))}')
    if not fields:
        raise ValueError('update_run: no fields to update')
    if 'status' in fields and fields['status'] not in RUN_STATUSES:
        raise ValueError(f'update_run: unknown status {fields["status"]!r}')
    columns = [c for c in _RUN_UPDATABLE_COLUMNS if c in fields]
    assignments = ', '.join(f'{c} = ?' for c in columns)
    params = [_to_text(fields[c]) for c in columns] + [run_id]
    conn = database._get_connection()
    try:
        with conn:
            cur = conn.execute(f'UPDATE digest_runs SET {assignments} WHERE digest_date = ?', params)
        return cur.rowcount > 0
    finally:
        conn.close()


# ========================
# Digest deliveries
# ========================


def snapshot_recipients(digest_date: str, user_ids: Iterable[int]) -> int:
    """Adds 'pending' deliveries; existing rows (e.g. already 'sent') are kept. Returns rows added."""
    conn = database._get_connection()
    try:
        with conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO digest_deliveries (digest_date, user_id, status) VALUES (?, ?, 'pending')",
                [(digest_date, uid) for uid in user_ids],
            )
        return cur.rowcount
    finally:
        conn.close()


def list_deliveries(run_id: str, status: str | None = None) -> list[dict]:
    """Deliveries of the run (run_id == digest_date), optionally filtered by status, by user_id."""
    conn = database._get_connection()
    try:
        if status is None:
            rows = conn.execute(
                'SELECT * FROM digest_deliveries WHERE digest_date = ? ORDER BY user_id', (run_id,),
            )
        else:
            rows = conn.execute(
                'SELECT * FROM digest_deliveries WHERE digest_date = ? AND status = ? ORDER BY user_id',
                (run_id, status),
            )
        return [dict(r) for r in rows]
    finally:
        conn.close()


def claim_delivery(digest_date: str, user_id: int) -> bool:
    """Atomic pending -> sending; True only for the single caller that won."""
    conn = database._get_connection()
    try:
        with conn:
            cur = conn.execute(
                """
                UPDATE digest_deliveries SET status = 'sending'
                WHERE digest_date = ? AND user_id = ? AND status = 'pending'
                """,
                (digest_date, user_id),
            )
        return cur.rowcount == 1
    finally:
        conn.close()


def set_delivery_status(digest_date: str, user_id: int, status: str) -> bool:
    """
    Sets the delivery status ('sent' also stamps sent_at). 'sent' is terminal: a later
    non-'sent' status never overwrites it. False if the row is missing or already sent.
    """
    if status not in DELIVERY_STATUSES:
        raise ValueError(f'unknown delivery status: {status!r}')
    conn = database._get_connection()
    try:
        with conn:
            if status == 'sent':
                cur = conn.execute(
                    'UPDATE digest_deliveries SET status = ?, sent_at = ? WHERE digest_date = ? AND user_id = ?',
                    (status, _now(), digest_date, user_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE digest_deliveries SET status = ? "
                    "WHERE digest_date = ? AND user_id = ? AND status != 'sent'",
                    (status, digest_date, user_id),
                )
        return cur.rowcount > 0
    finally:
        conn.close()
