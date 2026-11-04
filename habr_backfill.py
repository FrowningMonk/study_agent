"""
Hubs/tags backfill for Habr articles already in the DB (Decision 18, stage C).

The network-free part (articles.habr_id from URLs, legacy views) is done by the
`init_db` migration. This module fetches hubs/tags through the kek API for every
habr_id in `articles` that has neither `fetched_at` nor `unavailable_at`:
    - success -> metadata, hubs and tags upserted (each article committed alone,
      so a restart mid-run keeps the progress);
    - HabrNotFound -> `unavailable_at` set, the id is never requested again;
    - a per-article 4xx that habr_api does not map yet (HabrUnavailable kind 'http',
      400-499 except 408/429, e.g. an unknown 403 errorCode) -> the id is skipped
      (`skipped` stat, not marked) and the run continues; it stays a candidate, so a
      later mapping or a recovered article is picked up on the next start. One such
      id must never stall every later id (QA-1 root cause);
    - any other HabrUnavailable (5xx, timeout, network, invalid response) / HabrBusy
      -> the run stops, the id stays unfilled and is retried on the next bot start;
    - sqlite3.Error while listing or storing (e.g. "database is locked" when the
      digest writes longer than the 5 s busy timeout) -> treated as transient too:
      the run stops and resumes on the next start. Skipping the id instead would
      keep fetching from Habr while the results cannot be stored, and the id would
      be retried on the next start anyway; one rule for every non-404 failure keeps
      the stats and the resume point simple.
Duplicate `articles` rows for one habr_id are neither merged nor modified.

`run_once()` returns stats and never raises for these expected conditions.
`run_safely()` is the daemon thread body started by `bot.main()` via `bot._spawn`:
it logs any unexpected exception and ends only this thread.

Requests go through the process-wide `habr_api` throttle in background mode
(`wait_timeout=None`), plus PAUSE_BETWEEN_ARTICLES seconds between articles, so
handlers and the digest are never starved. DB connections are opened per call by
`habr_store` (no network inside a transaction).
"""

import logging
import sqlite3
import time

import habr_api
import habr_store

logger = logging.getLogger(__name__)

PAUSE_BETWEEN_ARTICLES: float = 2
PROGRESS_LOG_EVERY: int = 25


def _sleep(seconds: float) -> None:
    """The pause between articles; patched in tests."""
    time.sleep(seconds)


def _describe(error: Exception) -> str:
    """Log-safe failure description: exception type plus the habr_api category/status, never free text."""
    parts = [type(error).__name__]
    kind = getattr(error, 'kind', None)
    if isinstance(kind, str):
        parts.append(kind)
    status = getattr(error, 'status', None)
    if isinstance(status, int):
        parts.append(f'HTTP {status}')
    return ' '.join(parts)


def _per_article_failure(error: Exception) -> bool:
    """A client error about this one article (skip it), not a Habr-wide problem (stop)."""
    return (isinstance(error, habr_api.HabrUnavailable) and error.kind == 'http'
            and isinstance(error.status, int) and 400 <= error.status < 500
            and error.status not in (408, 429))


def _log_summary(stats: dict, started: float) -> None:
    logger.info('Backfill finished: candidates=%d, requests=%d, filled=%d, unavailable=%d, '
                'skipped=%d, stopped_on_error=%s, duration=%.1fs',
                stats['candidates'], stats['requests'], stats['filled'], stats['unavailable'],
                stats['skipped'], stats['stopped_on_error'], time.monotonic() - started)


def run_once() -> dict:
    """Backfills all current candidates once; returns stats (candidates, requests, filled,
    unavailable, skipped, stopped_on_error)."""
    stats = {'candidates': 0, 'requests': 0, 'filled': 0, 'unavailable': 0, 'skipped': 0,
             'stopped_on_error': False}
    started = time.monotonic()
    try:
        candidates = habr_store.list_backfill_candidates()
    except sqlite3.Error as e:
        logger.warning('Backfill: DB error while listing candidates (%s), will retry on next start', _describe(e))
        stats['stopped_on_error'] = True
        _log_summary(stats, started)
        return stats
    stats['candidates'] = len(candidates)
    if candidates:
        logger.info('Backfill: %d articles to fetch', len(candidates))

    for index, habr_id in enumerate(candidates):
        if index:
            _sleep(PAUSE_BETWEEN_ARTICLES)
        stats['requests'] += 1
        try:
            try:
                item = habr_api.fetch_article(habr_id, wait_timeout=None)
            except habr_api.HabrNotFound:
                habr_store.mark_unavailable(habr_id)
                stats['unavailable'] += 1
            else:
                habr_store.upsert_habr_article(item)
                stats['filled'] += 1
        except habr_api.HabrUnavailable as e:
            if not _per_article_failure(e):
                logger.warning('Backfill: stopped on id=%d (%s), will resume on next start', habr_id, _describe(e))
                stats['stopped_on_error'] = True
                break
            logger.warning('Backfill: skipped id=%d (%s), retried on next start', habr_id, _describe(e))
            stats['skipped'] += 1
        except (habr_api.HabrBusy, sqlite3.Error) as e:
            # Network fetch is done outside any transaction; only the short store call may hit a DB lock
            logger.warning('Backfill: stopped on id=%d (%s), will resume on next start', habr_id, _describe(e))
            stats['stopped_on_error'] = True
            break
        if (index + 1) % PROGRESS_LOG_EVERY == 0:
            logger.info('Backfill progress: %d/%d (filled=%d, unavailable=%d)',
                        index + 1, len(candidates), stats['filled'], stats['unavailable'])

    _log_summary(stats, started)
    return stats


def run_safely() -> None:
    """Thread body: runs the backfill; an unexpected exception is logged and ends only this thread."""
    try:
        run_once()
    except Exception:
        logger.exception('Backfill: unexpected error, thread stopped')
