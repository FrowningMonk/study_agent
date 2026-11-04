"""
Hubs/tags backfill for Habr articles already in the DB (Decision 18, stage C).

The network-free part (articles.habr_id from URLs, legacy views) is done by the
`init_db` migration. This module fetches hubs/tags through the kek API for every
habr_id in `articles` that has neither `fetched_at` nor `unavailable_at`:
    - success -> metadata, hubs and tags upserted (each article committed alone,
      so a restart mid-run keeps the progress);
    - HabrNotFound -> `unavailable_at` set, the id is never requested again;
    - HabrUnavailable / HabrBusy -> the run stops, the id stays unfilled and is
      retried on the next bot start.
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
import time

import habr_api
import habr_store

logger = logging.getLogger(__name__)

PAUSE_BETWEEN_ARTICLES: float = 2
PROGRESS_LOG_EVERY: int = 25


def _sleep(seconds: float) -> None:
    """The pause between articles; patched in tests."""
    time.sleep(seconds)


def run_once() -> dict:
    """Backfills all current candidates once; returns stats (requests, filled, unavailable, stopped_on_error)."""
    stats = {'candidates': 0, 'requests': 0, 'filled': 0, 'unavailable': 0, 'stopped_on_error': False}
    candidates = habr_store.list_backfill_candidates()
    stats['candidates'] = len(candidates)
    started = time.monotonic()
    if candidates:
        logger.info('Backfill: %d articles to fetch', len(candidates))

    for index, habr_id in enumerate(candidates):
        if index:
            _sleep(PAUSE_BETWEEN_ARTICLES)
        stats['requests'] += 1
        try:
            item = habr_api.fetch_article(habr_id, wait_timeout=None)
        except habr_api.HabrNotFound:
            habr_store.mark_unavailable(habr_id)
            stats['unavailable'] += 1
        except (habr_api.HabrUnavailable, habr_api.HabrBusy) as e:
            logger.warning('Backfill: stopped on id=%d (%s), will resume on next start', habr_id, e)
            stats['stopped_on_error'] = True
            break
        else:
            habr_store.upsert_habr_article(item)
            stats['filled'] += 1
        if (index + 1) % PROGRESS_LOG_EVERY == 0:
            logger.info('Backfill progress: %d/%d (filled=%d, unavailable=%d)',
                        index + 1, len(candidates), stats['filled'], stats['unavailable'])

    logger.info('Backfill finished: candidates=%d, requests=%d, filled=%d, unavailable=%d, '
                'stopped_on_error=%s, duration=%.1fs',
                stats['candidates'], stats['requests'], stats['filled'], stats['unavailable'],
                stats['stopped_on_error'], time.monotonic() - started)
    return stats


def run_safely() -> None:
    """Thread body: runs the backfill; an unexpected exception is logged and ends only this thread."""
    try:
        run_once()
    except Exception:
        logger.exception('Backfill: unexpected error, thread stopped')
