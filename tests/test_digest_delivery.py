"""Tests for digest.py delivery part (Task 10): scheduler tick, delivery state machine,
Telegram error policy, startup close, scheduler loop and the /digest_now runner.

Integration style: tmp_db + fake Habr at the requests level (habr_api runs for real),
fake describe, fake Telegram sender and a fake clock. No real network, no real LLM.
"""

import copy
import json
import logging
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from telebot.apihelper import ApiTelegramException

import access
import database
import digest
import feed
import habr_api
import habr_store
import summarizer
from conftest import kek_article_id, rss_body

UTC = timezone.utc
TODAY = date(2026, 11, 4)
DAY = date(2026, 11, 3)                     # digest day of TODAY
TOMORROW = date(2026, 11, 5)
OWNER = 1
USER = 501
OTHER = 502
FOOTER = 'Настроить: /subscribe, выключить: /digest off'
LEAD = '<p>Короткий анонс статьи про Python и всё такое.</p>'
CALLBACK_RE = re.compile(r'hl:([sm]):([0-9]+):([0-9]+)')


def at(hour, minute=0, day=TODAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=feed.MSK)


# =============================================================================
# Fakes
# =============================================================================


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.headers = {}
        self._body = (body if isinstance(body, str) else json.dumps(body)).encode('utf-8')

    def iter_content(self, chunk_size=1):
        yield self._body

    def close(self):
        pass


class FakeHabr:
    """Serves the Habr RSS feeds and kek article pages (built from the trimmed real fixture);
    habr_api parses them. `log` holds one entry per list fetch (the first top feed request),
    `requests` every request."""

    def __init__(self, template):
        self.template = template
        self.articles = []          # (habr_id, published, score, hubs, lead, title)
        self.status = 200
        self.partial = False        # every kek article fetch fails -> 'partial'
        self.log = []
        self.requests = []

    def add(self, habr_id, score=0, hubs=('other',), day=DAY, minute=0, lead=LEAD, title=None):
        published = datetime(day.year, day.month, day.day, 12, 0, tzinfo=feed.MSK) - timedelta(minutes=minute)
        self.articles.append((habr_id, published, score, hubs, lead, title or f'Статья {habr_id}'))

    def _ref(self, habr_id, published, score, hubs, lead, title):
        ref = copy.deepcopy(self.template)
        ref['id'] = str(habr_id)
        ref['titleHtml'] = title
        ref['timePublished'] = published.astimezone(UTC).isoformat()
        ref['statistics']['score'] = score
        ref['statistics']['commentsCount'] = 0
        ref['hubs'] = [{'alias': a, 'type': 'collective'} for a in hubs]
        ref['leadData']['textHtml'] = lead
        return ref

    def rss(self):
        rows = sorted(self.articles, key=lambda a: a[1], reverse=True)
        # One old item outside the digest day
        rows.append((999, datetime(2026, 10, 1, 12, 0, tzinfo=feed.MSK), 0, ('other',), LEAD, 'Старая'))
        return rss_body([(r[0], r[1], r[5]) for r in rows])

    def get(self, url, params=None, **kw):
        self.requests.append((url, dict(params or {})))
        if url == habr_api.RSS_BASE + habr_api.RSS_TOP_FEEDS[0]:
            self.log.append((url, dict(params or {})))
        if self.status != 200:
            return _Resp(self.status, {'error': 'x'})
        if url.startswith(habr_api.RSS_BASE):
            return _Resp(200, self.rss())
        habr_id = kek_article_id(url)
        row = next((r for r in self.articles if r[0] == habr_id), None)
        if row is None or self.partial:
            return _Resp(503, {'error': 'x'})
        return _Resp(200, self._ref(*row))


class FakeSend:
    """send(chat_id, text, rows): records successes; raises queued exceptions per chat id."""

    def __init__(self):
        self.sent = []
        self.attempts = []
        self.errors = {}

    def __call__(self, chat_id, text, rows):
        self.attempts.append((chat_id, text, rows))
        queue = self.errors.get(chat_id)
        if queue:
            raise queue.pop(0)
        self.sent.append((chat_id, text, rows))

    def to(self, chat_id):
        return [(text, rows) for c, text, rows in self.sent if c == chat_id]


class Describe:
    def __init__(self, result=lambda title: f'О чём: {title}', error=None):
        self.calls = []
        self.result = result
        self.error = error

    def __call__(self, title, lead_text):
        self.calls.append(title)
        if self.error is not None:
            raise self.error
        return self.result(title) if callable(self.result) else self.result


class _StopLoop(Exception):
    pass


def tg_error(code, description, retry_after=None):
    result_json = {'ok': False, 'error_code': code, 'description': description}
    if retry_after is not None:
        result_json['parameters'] = {'retry_after': retry_after}
    return ApiTelegramException('sendMessage', None, result_json)


# =============================================================================
# Fixtures and helpers
# =============================================================================


@pytest.fixture
def habr(monkeypatch, fixture_text):
    page = json.loads(fixture_text('habr_list_page.json'))
    fake = FakeHabr(page['publicationRefs'][page['publicationIds'][0]])
    monkeypatch.setattr(habr_api.requests, 'get', fake.get)
    return fake


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(digest, '_sleep', calls.append)
    return calls


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """The real summarizer must never reach a provider; calls are recorded for asserts."""
    calls = []

    def guard(*a, **kw):
        calls.append(a)
        raise AssertionError('real LLM call in a test')

    monkeypatch.setattr(summarizer, '_generate', guard)
    monkeypatch.delenv('DIGEST_TIME', raising=False)
    return calls


@pytest.fixture
def send():
    return FakeSend()


def approve(uid):
    database.create_access_request(uid, f'u{uid}', 'U')
    database.set_user_status(uid, 'approved')


def allowed(uid):
    return access.is_allowed(uid, OWNER)        # the production rule (bot.is_allowed delegates)


def tick(now, send, describe=None):
    digest.tick(now, send=send, owner_id=OWNER, is_allowed=allowed, describe=describe)


def statuses(run_id=TODAY.isoformat()):
    return {d['user_id']: d['status'] for d in habr_store.list_deliveries(run_id)}


def callbacks(rows):
    return [cb for row in (rows or []) for _, cb in row]


def fill_day(habr, site=8, hubs=0, day=DAY):
    """Site items 100+n and python-hub items 200+n on DAY; other days get distinct ids."""
    base = (day - DAY).days * 1000
    for n in range(site):
        habr.add(base + 100 + n, score=100 - n, hubs=('other',), day=day, minute=n)
    for n in range(hubs):
        habr.add(base + 200 + n, score=50 - n, hubs=('python',), day=day, minute=100 + n)


def sync_spawn(monkeypatch):
    monkeypatch.setattr(digest, '_spawn', lambda target: target())


# =============================================================================
# Time transitions (fake clock)
# =============================================================================


def test_digest_time_parsing(monkeypatch, caplog):
    monkeypatch.delenv('DIGEST_TIME', raising=False)
    assert digest.digest_time().strftime('%H:%M') == '18:00'
    monkeypatch.setenv('DIGEST_TIME', '09:30')
    assert digest.digest_time().strftime('%H:%M') == '09:30'
    for bad in ('25:00', '9:3', 'abc', '18:00x', '12:60'):
        caplog.clear()
        monkeypatch.setenv('DIGEST_TIME', bad)
        with caplog.at_level(logging.WARNING, logger='digest'):
            assert digest.digest_time().strftime('%H:%M') == '18:00'
        assert any('DIGEST_TIME' in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING), bad


def test_digest_time_at_or_after_skip_time_falls_back(monkeypatch, caplog):
    """DIGEST_TIME=23:59 would start at SKIP_TIME and never create a run (CA-6)."""
    assert digest.SKIP_TIME.strftime('%H:%M') == '23:59'
    monkeypatch.setenv('DIGEST_TIME', '23:59')
    with caplog.at_level(logging.WARNING, logger='digest'):
        assert digest.digest_time().strftime('%H:%M') == '18:00'
    assert any('DIGEST_TIME' in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    # The last minute before the skip time is still accepted
    caplog.clear()
    monkeypatch.setenv('DIGEST_TIME', '23:58')
    with caplog.at_level(logging.WARNING, logger='digest'):
        assert digest.digest_time().strftime('%H:%M') == '23:58'
    assert not caplog.records


def test_digest_time_2359_still_creates_run_at_default(tmp_db, habr, send, sleeps, monkeypatch):
    monkeypatch.setenv('DIGEST_TIME', '23:59')
    fill_day(habr)
    tick(at(18, 0), send)
    assert habr_store.get_run(TODAY.isoformat()) is not None
    assert len(send.to(OWNER)) == 1


def test_digest_time_from_env_moves_the_run(tmp_db, habr, send, sleeps, monkeypatch):
    fill_day(habr, site=2)
    monkeypatch.setenv('DIGEST_TIME', '09:30')
    tick(at(9, 29), send)
    assert habr_store.get_run(TODAY.isoformat()) is None
    tick(at(9, 30), send)
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'


def test_tick_before_digest_time_does_nothing(tmp_db, habr, send, sleeps):
    approve(USER)
    tick(at(17, 59), send)
    assert habr_store.get_run(TODAY.isoformat()) is None
    assert statuses() == {}
    assert send.attempts == [] and habr.log == []


def test_tick_at_digest_time_creates_run_and_snapshots_recipients(tmp_db, habr, send, sleeps):
    habr.status = 503                                   # keep deliveries pending
    approve(USER)
    database.create_access_request(601, 'p', 'P')       # pending
    approve(602)
    database.set_user_status(602, 'rejected')
    approve(603)
    habr_store.set_digest_enabled(603, False)
    tick(at(18, 0), send)
    run = habr_store.get_run(TODAY.isoformat())
    assert run['status'] == 'pending'
    assert statuses() == {OWNER: 'pending', USER: 'pending'}


def test_recipients_follow_the_injected_access_rule(tmp_db):
    """The snapshot uses the same is_allowed as the send-time re-check (CA-3), not its own copy."""
    approve(USER)
    approve(OTHER)
    database.create_access_request(601, 'p', 'P')       # pending
    assert digest._recipients(OWNER, allowed) == [OWNER, USER, OTHER]
    # A different rule changes the snapshot: nothing in digest re-implements the status check
    assert digest._recipients(OWNER, lambda uid: uid in (OWNER, 601)) == [OWNER, 601]
    habr_store.set_digest_enabled(OTHER, False)
    assert digest._recipients(OWNER, allowed) == [OWNER, USER]


@pytest.mark.parametrize('broken', ['is_allowed', 'digest_setting'])
def test_snapshot_db_error_aborts_tick_and_is_retried(tmp_db, habr, send, sleeps, monkeypatch, broken):
    """A DB error while building the recipient list never silently drops a user (CAR-3):
    no run is created, and the next tick snapshots everybody."""
    approve(USER)
    fill_day(habr)
    real_enabled = habr_store.get_digest_enabled

    def locked(uid):
        if uid == USER:
            raise sqlite3.OperationalError('database is locked')
        return allowed(uid) if broken == 'is_allowed' else real_enabled(uid)

    if broken == 'is_allowed':
        with pytest.raises(sqlite3.OperationalError):
            digest.tick(at(18, 0), send=send, owner_id=OWNER, is_allowed=locked, describe=None)
    else:
        monkeypatch.setattr(habr_store, 'get_digest_enabled', locked)
        with pytest.raises(sqlite3.OperationalError):
            tick(at(18, 0), send)
        monkeypatch.setattr(habr_store, 'get_digest_enabled', real_enabled)
    assert habr_store.get_run(TODAY.isoformat()) is None and statuses() == {}

    tick(at(18, 1), send)
    assert statuses() == {OWNER: 'sent', USER: 'sent'}


def test_recipients_match_access_rule_for_every_status(tmp_db):
    """Ties digest recipients to access.is_allowed / gate for each user status."""
    ids = {'approved': 701, 'pending': 702, 'rejected': 703}
    for status, uid in ids.items():
        database.create_access_request(uid, f'u{uid}', 'U')
        if status != 'pending':
            database.set_user_status(uid, status)
    recipients = set(digest._recipients(OWNER, allowed))
    for uid in [OWNER, *ids.values()]:
        passes_gate = access.gate(uid, 'private', None, OWNER) == access.Gate.PASS
        assert (uid in recipients) is access.is_allowed(uid, OWNER) is passes_gate, uid


def test_failed_attempt_schedules_retry_in_15_minutes(tmp_db, habr, send, sleeps):
    habr.status = 503
    tick(at(18, 0), send)
    run = habr_store.get_run(TODAY.isoformat())
    assert run['last_error'] == 'Хабр не ответил (HTTP 503)'
    assert datetime.fromisoformat(run['next_attempt_at']) == at(18, 15)
    requests_made = len(habr.log)
    assert requests_made == 1
    tick(at(18, 14), send)
    assert len(habr.log) == requests_made
    tick(at(18, 15), send)
    assert len(habr.log) == requests_made + 1
    assert send.attempts == []


def test_owner_warning_sent_once_at_plus_3h(tmp_db, habr, send, sleeps):
    habr.status = 503
    approve(USER)
    for now in (at(18, 0), at(20, 59), at(21, 0), at(21, 1), at(21, 15)):
        tick(now, send)
    assert send.sent == [(OWNER, 'Дайджест за сегодня ещё не разослан: Хабр не ответил (HTTP 503)', None)]
    assert datetime.fromisoformat(habr_store.get_run(TODAY.isoformat())['warned_at']) == at(21, 0)


def test_no_warning_when_plus_3h_is_past_skip_time(tmp_db, habr, send, sleeps, monkeypatch):
    monkeypatch.setenv('DIGEST_TIME', '21:30')
    habr.status = 503
    for now in (at(21, 30), at(23, 58), at(23, 59)):
        tick(now, send)
    assert [t for _, t, _ in send.sent] == ['Дайджест за 4 ноября пропущен: Хабр не ответил (HTTP 503)']


def test_day_skipped_at_2359_with_reason(tmp_db, habr, send, sleeps):
    habr.status = 503
    approve(USER)
    tick(at(18, 0), send)
    tick(at(23, 59), send)
    run = habr_store.get_run(TODAY.isoformat())
    assert run['status'] == 'skipped' and run['finished_at']
    expected = f'Дайджест за {feed.format_date_ru(TODAY)} пропущен: Хабр не ответил (HTTP 503)'
    assert send.sent == [(OWNER, expected, None)]
    habr.status = 200
    fill_day(habr)
    requests_made = len(habr.log)
    tick(at(23, 59) + timedelta(seconds=30), send)
    assert len(send.sent) == 1 and len(habr.log) == requests_made


def test_start_after_skip_time_creates_no_run(tmp_db, habr, send, sleeps):
    tick(at(23, 59), send)
    assert habr_store.get_run(TODAY.isoformat()) is None
    assert send.attempts == [] and habr.log == []


def test_startup_closes_previous_day_run(tmp_db, habr, send, monkeypatch):
    habr_store.snapshot_recipients('2026-11-03', [OWNER, USER])
    habr_store.create_run('2026-11-03')
    monkeypatch.setattr(digest, '_now', lambda: at(10, 0))

    def stop(seconds):
        raise _StopLoop

    monkeypatch.setattr(digest, '_sleep', stop)
    with pytest.raises(_StopLoop):
        digest.run_scheduler(send, OWNER, allowed)
    assert habr_store.get_run('2026-11-03')['status'] == 'skipped'
    assert send.sent == [(OWNER, 'Дайджест за 3 ноября пропущен: бот не работал', None)]
    assert habr.log == []


def test_scheduler_loop_survives_tick_exception(tmp_db, send, monkeypatch, caplog):
    calls = []

    def fake_tick(now, **kw):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError('boom')

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise _StopLoop

    monkeypatch.setattr(digest, 'tick', fake_tick)
    monkeypatch.setattr(digest, '_sleep', fake_sleep)
    monkeypatch.setattr(digest, '_now', lambda: at(10, 0))
    with caplog.at_level(logging.INFO, logger='digest'), pytest.raises(_StopLoop):
        digest.run_scheduler(send, OWNER, allowed)
    assert len(calls) == 3
    assert sleeps == [60, 60, 60]
    assert any(r.levelno == logging.ERROR and 'tick failed' in r.getMessage() and r.exc_info
               for r in caplog.records)
    assert any('scheduler started' in r.getMessage() and '2026-11-04 18:00' in r.getMessage()
               for r in caplog.records)


# =============================================================================
# Full runs
# =============================================================================


def _check_part_rows(user_id, rows):
    for cb in callbacks(rows):
        match = CALLBACK_RE.fullmatch(cb)
        assert match and len(cb.encode('utf-8')) <= 64, cb
        ranked = habr_store.get_ranked_list(int(match.group(2)))
        assert ranked['user_id'] == user_id


def test_full_run_owner_and_approved_user(tmp_db, habr, send, sleeps):
    approve(USER)
    for uid in (OWNER, USER):
        habr_store.add_subscription(uid, 'python')
    fill_day(habr, site=8, hubs=7)
    describe = Describe()

    tick(at(18, 0), send, describe=describe)

    for uid in (OWNER, USER):
        parts = send.to(uid)
        assert len(parts) == 2
        (site_text, site_rows), (hubs_text, hubs_rows) = parts
        assert site_text.startswith('Лучшее на Хабре') and hubs_text.startswith('Твои хабы')
        assert 'О чём: Статья 100' in site_text and 'О чём: Статья 200' in hubs_text
        assert FOOTER not in site_text and hubs_text.endswith(FOOTER)
        for rows in (site_rows, hubs_rows):
            _check_part_rows(uid, rows)
            labels = [t for row in rows for t, _ in row]
            assert labels == [f'Саммари {n}' for n in range(1, 6)] + ['Ещё 10 ▶']
        site_list = int(CALLBACK_RE.fullmatch(callbacks(site_rows)[0]).group(2))
        assert callbacks(site_rows)[-1] == f'hl:m:{site_list}:0'
        assert habr_store.get_ranked_list(site_list)['kind'] == 'digest_site'
        assert habr_store.get_ranked_list(site_list)['total'] == 15      # all day articles
    assert statuses() == {OWNER: 'sent', USER: 'sent'}
    assert all(d['sent_at'] for d in habr_store.list_deliveries(TODAY.isoformat()))
    run = habr_store.get_run(TODAY.isoformat())
    assert run['status'] == 'done' and run['finished_at']
    assert len(habr.log) == 1                               # one shared fetch
    assert sleeps.count(0.1) == 2                         # pause between recipients


def test_same_day_rerun_sends_nothing(tmp_db, habr, send, sleeps):
    fill_day(habr)
    tick(at(18, 0), send)
    sent, requests_made = len(send.sent), len(habr.log)
    tick(at(18, 5), send)
    tick(at(22, 0), send)
    assert len(send.sent) == sent and len(habr.log) == requests_made


def test_restart_after_digest_time_delivers_only_pending(tmp_db, habr, send, sleeps):
    approve(USER)
    fill_day(habr)
    habr_store.snapshot_recipients(TODAY.isoformat(), [OWNER, USER])
    habr_store.create_run(TODAY.isoformat())
    habr_store.set_delivery_status(TODAY.isoformat(), OWNER, 'sent')
    tick(at(19, 0), send)
    assert send.to(OWNER) == [] and len(send.to(USER)) == 1
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'


def test_sending_row_treated_as_sent(tmp_db, habr, send, sleeps):
    approve(USER)
    fill_day(habr)
    habr_store.snapshot_recipients(TODAY.isoformat(), [OWNER, USER])
    habr_store.create_run(TODAY.isoformat())
    assert habr_store.claim_delivery(TODAY.isoformat(), USER)
    tick(at(19, 0), send)
    assert send.to(USER) == [] and len(send.to(OWNER)) == 1
    assert statuses()[USER] == 'sending'
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'


def test_run_without_recipients_is_done_without_fetch(tmp_db, habr, send, sleeps):
    habr_store.set_digest_enabled(OWNER, False)
    tick(at(18, 0), send)
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'
    assert habr.log == [] and send.attempts == []


def test_next_day_no_catch_up(tmp_db, habr, send, sleeps):
    approve(USER)
    habr.status = 503
    tick(at(18, 0), send)                               # today's run stays unfinished
    habr.status = 200
    fill_day(habr)
    requests_made = len(habr.log)
    tick(at(10, 0, day=TOMORROW), send)
    tick(at(17, 59, day=TOMORROW), send)
    # Nothing is sent for the previous day: only the owner learns that it was skipped
    assert send.sent == [(OWNER, 'Дайджест за 4 ноября пропущен: Хабр не ответил (HTTP 503)', None)]
    assert len(habr.log) == requests_made
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'skipped'
    assert habr_store.get_run(TOMORROW.isoformat()) is None
    assert set(statuses().values()) == {'pending'}


def test_tick_skips_previous_day_run_when_2359_was_missed(tmp_db, habr, send, sleeps):
    habr.status = 503
    tick(at(18, 0), send)
    tick(at(23, 58), send)                              # the 23:59 minute had no tick
    tick(at(0, 2, day=TOMORROW), send)
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'skipped'
    assert [t for _, t, _ in send.sent] == [
        'Дайджест за сегодня ещё не разослан: Хабр не ответил (HTTP 503)',
        'Дайджест за 4 ноября пропущен: Хабр не ответил (HTTP 503)',
    ]
    tick(at(0, 3, day=TOMORROW), send)
    assert len(send.sent) == 2


def test_empty_day_sends_nothing_new(tmp_db, habr, send, sleeps):
    approve(USER)
    tick(at(18, 0), send, describe=Describe())
    expected = [(f'За вчера ничего нового\n\n{FOOTER}', None)]
    assert send.to(OWNER) == expected and send.to(USER) == expected
    assert statuses() == {OWNER: 'sent', USER: 'sent'}


# =============================================================================
# Telegram error policy
# =============================================================================


def _settings_row(uid):
    conn = sqlite3.connect(database.DB_PATH)
    try:
        return conn.execute('SELECT enabled, disabled_reason FROM digest_settings WHERE user_id = ?',
                            (uid,)).fetchone()
    finally:
        conn.close()


def _assert_blocked(send, caplog):
    assert statuses() == {OWNER: 'sent', USER: 'failed', OTHER: 'sent'}
    assert _settings_row(USER) == (0, 'blocked')
    assert habr_store.get_digest_enabled(USER) is False
    assert any(r.levelno == logging.WARNING and str(USER) in r.getMessage() for r in caplog.records)
    assert len(send.to(OTHER)) == 1 and send.to(USER) == []


@pytest.mark.parametrize('description', ['Forbidden: bot was blocked by the user',
                                         'Forbidden: user is deactivated'])
def test_blocked_user_403_disables_digest(tmp_db, habr, send, sleeps, caplog, description):
    approve(USER)
    approve(OTHER)
    fill_day(habr)
    send.errors[USER] = [tg_error(403, description)]
    with caplog.at_level(logging.WARNING, logger='digest'):
        tick(at(18, 0), send)
    _assert_blocked(send, caplog)


def test_chat_not_found_400_disables_digest(tmp_db, habr, send, sleeps, caplog):
    approve(USER)
    approve(OTHER)
    fill_day(habr)
    send.errors[USER] = [tg_error(400, 'Bad Request: chat not found')]
    with caplog.at_level(logging.WARNING, logger='digest'):
        tick(at(18, 0), send)
    _assert_blocked(send, caplog)


def test_other_400_does_not_disable(tmp_db, habr, send, sleeps):
    approve(USER)
    fill_day(habr)
    send.errors[USER] = [tg_error(400, 'Bad Request: message is too long')]
    tick(at(18, 0), send)
    assert statuses()[USER] == 'failed'
    assert habr_store.get_digest_enabled(USER) is True


def test_429_retried_once_after_retry_after(tmp_db, habr, send, sleeps):
    approve(USER)
    fill_day(habr)
    send.errors[USER] = [tg_error(429, 'Too Many Requests: retry after 3', retry_after=3)]
    tick(at(18, 0), send)
    assert 3 in sleeps
    assert statuses()[USER] == 'sent'
    assert len(send.to(USER)) == 1
    assert len([a for a in send.attempts if a[0] == USER]) == 2


def test_429_without_retry_after_uses_default(tmp_db, habr, send, sleeps):
    approve(USER)
    fill_day(habr)
    send.errors[USER] = [tg_error(429, 'Too Many Requests')]
    tick(at(18, 0), send)
    assert digest.RETRY_AFTER_DEFAULT in sleeps
    assert statuses()[USER] == 'sent'


def test_two_consecutive_429_mark_failed_not_resent(tmp_db, habr, send, sleeps):
    approve(USER)
    fill_day(habr)
    send.errors[USER] = [tg_error(429, 'Too Many Requests', retry_after=1),
                         tg_error(429, 'Too Many Requests', retry_after=1)]
    tick(at(18, 0), send)
    assert statuses()[USER] == 'failed'
    attempts = len(send.attempts)
    tick(at(18, 30), send)
    tick(at(19, 0), send)
    assert len(send.attempts) == attempts
    assert habr_store.get_digest_enabled(USER) is True


def test_429_with_long_retry_after_fails_without_waiting(tmp_db, habr, send, sleeps):
    approve(USER)
    fill_day(habr)
    send.errors[USER] = [tg_error(429, 'Too Many Requests', retry_after=3600)]
    tick(at(18, 0), send)
    assert statuses()[USER] == 'failed'
    assert 3600 not in sleeps and len([a for a in send.attempts if a[0] == USER]) == 1


def test_one_recipient_build_error_does_not_block_others(tmp_db, habr, send, sleeps, monkeypatch, caplog):
    approve(USER)
    approve(OTHER)
    fill_day(habr)
    real_build = digest.build_user_digest

    def build(user_id, *a, **kw):
        if user_id == USER:
            raise sqlite3.OperationalError('disk I/O error')
        return real_build(user_id, *a, **kw)

    monkeypatch.setattr(digest, 'build_user_digest', build)
    with caplog.at_level(logging.ERROR, logger='digest'):
        tick(at(18, 0), send)
    assert statuses() == {OWNER: 'sent', USER: 'failed', OTHER: 'sent'}
    run = habr_store.get_run(TODAY.isoformat())
    assert run['status'] == 'done' and run['last_error'] is None
    assert any(str(USER) in r.getMessage() for r in caplog.records)


def test_no_build_for_delivery_no_longer_pending(tmp_db, habr, send, sleeps, monkeypatch):
    """A row that stopped being pending (e.g. /digest_now final) costs no build/LLM work (CA-5)."""
    approve(USER)
    fill_day(habr)
    built = []
    real_build = digest.build_user_digest
    real_can_receive = digest._can_receive

    def build(user_id, *a, **kw):
        built.append(user_id)
        return real_build(user_id, *a, **kw)

    def can_receive(user_id, is_allowed):
        if user_id == OWNER:          # /digest_now final took the owner's row after the listing
            habr_store.set_delivery_status(TODAY.isoformat(), OWNER, 'sent')
        return real_can_receive(user_id, is_allowed)

    monkeypatch.setattr(digest, 'build_user_digest', build)
    monkeypatch.setattr(digest, '_can_receive', can_receive)
    tick(at(18, 0), send, describe=Describe())

    assert built == [USER]                                       # owner: no build at all
    assert send.to(OWNER) == [] and len(send.to(USER)) == 1
    assert statuses() == {OWNER: 'sent', USER: 'sent'}


def test_crash_during_build_leaves_delivery_pending_and_retried(tmp_db, habr, send, sleeps, monkeypatch):
    """The row is claimed only after the build: a crash/restart mid-build (CAR-2) never
    leaves it 'sending' (= treated as sent), so the user still gets today's digest."""
    fill_day(habr)
    seen = []
    real_build = digest.build_user_digest

    def crashing_build(user_id, *a, **kw):
        seen.append(statuses()[user_id])
        raise KeyboardInterrupt                  # process dies mid-build (not an Exception)

    monkeypatch.setattr(digest, 'build_user_digest', crashing_build)
    with pytest.raises(KeyboardInterrupt):
        tick(at(18, 0), send)
    assert seen == ['pending']
    assert statuses() == {OWNER: 'pending'} and send.attempts == []

    monkeypatch.setattr(digest, 'build_user_digest', real_build)     # "restart"
    tick(at(18, 1), send)
    assert statuses() == {OWNER: 'sent'} and len(send.to(OWNER)) == 1
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'


def test_claim_lost_after_build_sends_nothing(tmp_db, habr, send, sleeps, monkeypatch, caplog):
    """/digest_now final finishes while the scheduled build runs: the late claim fails and the
    built digest is dropped, never sent twice."""
    fill_day(habr)
    real_build = digest.build_user_digest

    def build(user_id, *a, **kw):
        result = real_build(user_id, *a, **kw)
        habr_store.set_delivery_status(TODAY.isoformat(), user_id, 'sent')
        return result

    monkeypatch.setattr(digest, 'build_user_digest', build)
    with caplog.at_level(logging.INFO, logger='digest'):
        tick(at(18, 0), send)
    assert send.attempts == []
    assert statuses() == {OWNER: 'sent'}
    assert any('taken while building' in r.getMessage() for r in caplog.records)


def test_build_error_marks_failed(tmp_db, habr, send, sleeps, monkeypatch):
    """A build failure (row still pending) ends as 'failed', not stuck."""
    fill_day(habr)
    seen = []

    def build(user_id, *a, **kw):
        seen.append(statuses()[user_id])     # recorded, not asserted: _attempt swallows errors
        raise RuntimeError('build broke')

    monkeypatch.setattr(digest, 'build_user_digest', build)
    tick(at(18, 0), send)
    assert seen == ['pending']
    assert statuses() == {OWNER: 'failed'}
    assert send.attempts == []
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'


@pytest.mark.parametrize('error', [tg_error(500, 'Internal Server Error'), RuntimeError('network down')],
                         ids=['telegram-500', 'generic'])
def test_other_telegram_error_marks_failed_others_delivered(tmp_db, habr, send, sleeps, error):
    approve(USER)
    approve(OTHER)
    fill_day(habr)
    send.errors[USER] = [error]
    tick(at(18, 0), send)
    assert statuses() == {OWNER: 'sent', USER: 'failed', OTHER: 'sent'}
    assert habr_store.get_digest_enabled(USER) is True
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'


def test_partial_send_marks_failed_and_not_resent(tmp_db, habr, send, sleeps):
    approve(USER)
    habr_store.add_subscription(USER, 'python')
    fill_day(habr, site=6, hubs=3)

    class SecondPartFails(FakeSend):
        def __call__(self, chat_id, text, rows):
            if chat_id == USER and text.startswith('Твои хабы'):
                self.attempts.append((chat_id, text, rows))
                raise RuntimeError('boom')
            super().__call__(chat_id, text, rows)

    sender = SecondPartFails()
    tick(at(18, 0), sender)
    assert statuses()[USER] == 'failed'
    assert len(sender.to(USER)) == 1
    tick(at(18, 30), sender)
    assert len(sender.to(USER)) == 1


def test_owner_alert_failure_does_not_break_tick(tmp_db, habr, send, sleeps, caplog):
    habr.status = 503
    send.errors[OWNER] = [tg_error(403, 'Forbidden: bot was blocked by the user')]
    tick(at(18, 0), send)
    with caplog.at_level(logging.WARNING, logger='digest'):
        tick(at(21, 0), send)                           # warning send fails
    run = habr_store.get_run(TODAY.isoformat())
    assert run['warned_at'] and run['status'] == 'pending'
    assert any('Owner alert failed' in r.getMessage() for r in caplog.records)


# =============================================================================
# Habr failures and recipients at send time
# =============================================================================


def test_partial_habr_response_sends_nothing(tmp_db, habr, send, sleeps):
    approve(USER)
    fill_day(habr)
    habr.partial = True
    tick(at(18, 0), send)
    assert send.attempts == []
    run = habr_store.get_run(TODAY.isoformat())
    assert run['last_error'] == 'Хабр вернул неполный список'
    assert datetime.fromisoformat(run['next_attempt_at']) == at(18, 15)
    assert statuses() == {OWNER: 'pending', USER: 'pending'}
    assert habr_store.get_habr_articles([100]) == {}


@pytest.mark.parametrize('error, reason', [
    (habr_api.HabrUnavailable('timeout'), 'Хабр не ответил вовремя'),
    (habr_api.HabrUnavailable('network', detail='ConnectionError https://x?token=SECRET'),
     'Хабр недоступен (ошибка сети)'),
    (habr_api.HabrUnavailable('invalid_shape', 200, 'missing id'), 'Хабр вернул некорректный ответ'),
    (habr_api.HabrBusy('lock'), 'внутренняя ошибка бота'),
    (RuntimeError('SECRET token in text'), 'внутренняя ошибка бота'),
])
def test_failure_reason_categories(tmp_db, send, sleeps, monkeypatch, error, reason):
    def fail(day, wait_timeout=None):
        raise error

    monkeypatch.setattr(digest, 'fetch_day_articles', fail)
    tick(at(18, 0), send)
    run = habr_store.get_run(TODAY.isoformat())
    assert run['last_error'] == reason
    assert 'SECRET' not in run['last_error']
    assert datetime.fromisoformat(run['next_attempt_at']) == at(18, 15)


def test_revoked_user_skipped_at_send_time(tmp_db, habr, send, sleeps):
    approve(USER)
    habr.status = 503
    tick(at(18, 0), send)                               # snapshot includes USER
    database.set_user_status(USER, 'rejected')
    habr.status = 200
    fill_day(habr)
    tick(at(18, 15), send)
    assert send.to(USER) == [] and len(send.to(OWNER)) == 1
    assert statuses() == {OWNER: 'sent', USER: 'failed'}
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'


def test_digest_off_after_snapshot_not_sent(tmp_db, habr, send, sleeps):
    approve(USER)
    habr.status = 503
    tick(at(18, 0), send)
    habr_store.set_digest_enabled(USER, False)
    habr.status = 200
    fill_day(habr)
    tick(at(18, 15), send)
    assert send.to(USER) == [] and statuses()[USER] == 'failed'


def test_user_approved_after_snapshot_gets_digest_next_day(tmp_db, habr, send, sleeps):
    fill_day(habr)
    fill_day(habr, site=3, day=TODAY)
    tick(at(18, 0), send)
    approve(USER)
    tick(at(18, 30), send)
    assert send.to(USER) == [] and USER not in statuses()
    tick(at(18, 0, day=TOMORROW), send)
    assert len(send.to(USER)) == 1
    assert statuses(TOMORROW.isoformat())[USER] == 'sent'


def test_digest_on_after_snapshot_delivered_next_day(tmp_db, habr, send, sleeps):
    approve(USER)
    habr_store.set_digest_enabled(USER, False)
    fill_day(habr)
    fill_day(habr, site=3, day=TODAY)
    tick(at(18, 0), send)
    habr_store.set_digest_enabled(USER, True)
    tick(at(18, 30), send)
    assert send.to(USER) == [] and USER not in statuses()
    tick(at(18, 0, day=TOMORROW), send)
    assert len(send.to(USER)) == 1


# =============================================================================
# Descriptions and content
# =============================================================================


def test_one_description_per_shared_article(tmp_db, habr, send, sleeps):
    approve(USER)
    fill_day(habr, site=5)
    describe = Describe()
    tick(at(18, 0), send, describe=describe)
    assert sorted(describe.calls) == sorted(f'Статья {100 + n}' for n in range(5))
    owner_text, user_text = send.to(OWNER)[0][0], send.to(USER)[0][0]
    assert 'О чём: Статья 100' in user_text
    assert owner_text == user_text
    assert habr_store.get_habr_article(100)['short_description'] == 'О чём: Статья 100'


def test_llm_failure_uses_lead_fallback_and_regenerates_next_run(tmp_db, habr, send, sleeps, monkeypatch):
    fill_day(habr, site=2)

    def bad(title, lead):
        if title == 'Статья 100':
            return None
        raise RuntimeError('LLM down')

    tick(at(18, 0), send, describe=bad)
    text = send.to(OWNER)[0][0]
    assert text.count(feed.fallback_description(LEAD, limit=digest.DESCRIPTION_LIMIT)) == 2
    assert habr_store.get_habr_article(100)['short_description'] is None
    assert habr_store.get_habr_article(101)['short_description'] is None

    # The next run (here the owner's /digest_now over the same day) asks again
    later = Describe()
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', later)
    monkeypatch.setattr(digest, '_now', lambda: at(19, 0))
    sync_spawn(monkeypatch)
    assert digest.start_digest_now(send, OWNER, False)
    assert sorted(later.calls) == ['Статья 100', 'Статья 101']
    assert 'О чём: Статья 100' in send.to(OWNER)[-1][0]


def test_user_without_subscriptions_gets_site_part_only(tmp_db, habr, send, sleeps):
    approve(USER)
    habr_store.add_subscription(OWNER, 'python')
    fill_day(habr, site=6, hubs=3)
    tick(at(18, 0), send)
    assert len(send.to(OWNER)) == 2
    (text, rows), = send.to(USER)
    assert text.startswith('Лучшее на Хабре') and text.endswith(FOOTER)


def test_feed_lists_and_views_do_not_change_digest(tmp_db, habr, send, sleeps):
    approve(USER)
    approve(OTHER)
    fill_day(habr, site=5)
    # /feed shown to USER (a persisted feed list): no view is recorded by showing a list
    feed.create_list(USER, 'feed', TODAY, [100, 101, 102])
    assert habr_store.get_viewed_ids(USER) == set()
    tick(at(18, 0), send)
    user_text, other_text = send.to(USER)[0][0], send.to(OTHER)[0][0]
    assert user_text == other_text
    for n in range(5):
        assert f'{n + 1}. Статья {100 + n} ' in user_text
    kinds = {habr_store.get_ranked_list(int(CALLBACK_RE.fullmatch(cb).group(2)))['kind']
             for cb in callbacks(send.to(USER)[0][1])}
    assert kinds == {'digest_site'}


def test_feed_summary_view_excludes_article_from_digest(tmp_db, habr, send, sleeps):
    """A "Саммари" click from /feed is a seen event (source 'feed'): excluded like any view."""
    approve(USER)
    fill_day(habr, site=3)
    habr_store.record_view(USER, 100, 'feed')
    tick(at(18, 0), send)
    user_text = send.to(USER)[0][0]
    assert 'Статья 100' not in user_text and '1. Статья 101 ' in user_text
    assert '1. Статья 100 ' in send.to(OWNER)[0][0]


def _make_list(age_days):
    list_id = feed.create_list(USER, 'feed', TODAY, [1])
    created = (at(10, 0) - timedelta(days=age_days)).astimezone(UTC).isoformat(timespec='microseconds')
    conn = sqlite3.connect(database.DB_PATH)
    try:
        with conn:
            conn.execute('UPDATE ranked_lists SET created_at = ? WHERE id = ?', (created, list_id))
    finally:
        conn.close()
    return list_id


def test_housekeeping_deletes_old_lists_once_per_day(tmp_db, habr, send, sleeps):
    old, fresh = _make_list(31), _make_list(29)
    tick(at(10, 0), send)                                # before DIGEST_TIME: cleanup still runs
    assert habr_store.get_ranked_list(old) is None
    assert habr_store.get_ranked_list(fresh) is not None
    old_again = _make_list(40)
    tick(at(10, 5), send)                                # once per day
    assert habr_store.get_ranked_list(old_again) is not None
    tick(at(10, 0, day=TOMORROW), send)
    assert habr_store.get_ranked_list(old_again) is None
    assert habr.log == [] and send.attempts == []


def test_housekeeping_failure_does_not_break_tick(tmp_db, habr, send, sleeps, monkeypatch):
    def broken(now=None, days=30):
        raise sqlite3.OperationalError('locked')

    monkeypatch.setattr(habr_store, 'cleanup_ranked_lists', broken)
    fill_day(habr, site=2)
    tick(at(18, 0), send)
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'


# =============================================================================
# /digest_now runner
# =============================================================================


def test_digest_now_final_before_digest_time_skips_owner(tmp_db, habr, send, sleeps, monkeypatch):
    approve(USER)
    fill_day(habr)
    sync_spawn(monkeypatch)
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', None)
    monkeypatch.setattr(digest, '_now', lambda: at(12, 0))
    assert digest.start_digest_now(send, OWNER, True) is True
    assert len(send.to(OWNER)) == 1
    assert statuses() == {OWNER: 'sent'}
    assert habr_store.get_run(TODAY.isoformat()) is None

    tick(at(18, 0), send)
    assert len(send.to(OWNER)) == 1
    assert len(send.to(USER)) == 1
    assert statuses() == {OWNER: 'sent', USER: 'sent'}
    assert habr_store.get_run(TODAY.isoformat())['status'] == 'done'


def test_digest_now_final_after_run_done_resends_and_keeps_sent(tmp_db, habr, send, sleeps, monkeypatch):
    fill_day(habr)
    tick(at(18, 0), send)
    sync_spawn(monkeypatch)
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', None)
    monkeypatch.setattr(digest, '_now', lambda: at(19, 0))
    assert digest.start_digest_now(send, OWNER, True)
    assert len(send.to(OWNER)) == 2
    assert statuses() == {OWNER: 'sent'}


def _count(table):
    conn = sqlite3.connect(database.DB_PATH)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    finally:
        conn.close()


def test_digest_now_plain_does_not_count(tmp_db, habr, send, sleeps, monkeypatch):
    fill_day(habr)
    sync_spawn(monkeypatch)
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', None)
    monkeypatch.setattr(digest, '_now', lambda: at(12, 0))
    assert digest.start_digest_now(send, OWNER, False) is True
    assert len(send.to(OWNER)) == 1
    assert _count('digest_runs') == 0 and _count('digest_deliveries') == 0
    assert _count('ranked_lists') == 1                   # lists persisted for the buttons
    tick(at(18, 0), send)
    assert len(send.to(OWNER)) == 2
    assert statuses() == {OWNER: 'sent'}


def test_digest_now_single_flight(tmp_db, habr, send, monkeypatch):
    fill_day(habr, site=2)
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', None)
    monkeypatch.setattr(digest, '_now', lambda: at(12, 0))
    entered, release = threading.Event(), threading.Event()
    real_fetch = digest.fetch_day_articles

    def blocking_fetch(day, wait_timeout=None):
        entered.set()
        assert release.wait(10)
        return real_fetch(day, wait_timeout)

    monkeypatch.setattr(digest, 'fetch_day_articles', blocking_fetch)
    threads = []

    def spawn(target):
        t = threading.Thread(target=target, daemon=True)
        threads.append(t)
        t.start()

    monkeypatch.setattr(digest, '_spawn', spawn)
    assert digest.start_digest_now(send, OWNER, False) is True
    assert entered.wait(10)
    assert digest.start_digest_now(send, OWNER, True) is False      # work is running
    assert len(threads) == 1
    release.set()
    threads[0].join(10)
    assert not threads[0].is_alive()
    assert len(send.to(OWNER)) == 1
    assert digest.start_digest_now(send, OWNER, False) is True      # cleared after completion
    threads[1].join(10)
    assert digest.TEXT_BUSY == 'Уже собираю дайджест'


def test_digest_now_flag_cleared_after_exception(tmp_db, habr, send, monkeypatch):
    fill_day(habr)

    def boom(*a, **kw):
        raise RuntimeError('build failed with SECRET')

    monkeypatch.setattr(digest, 'build_user_digest', boom)
    monkeypatch.setattr(digest, '_now', lambda: at(12, 0))
    sync_spawn(monkeypatch)
    assert digest.start_digest_now(send, OWNER, False) is True
    assert send.sent == [(OWNER, 'Дайджест не собран: внутренняя ошибка бота', None)]
    spawned = []
    monkeypatch.setattr(digest, '_spawn', spawned.append)
    assert digest.start_digest_now(send, OWNER, False) is True
    assert len(spawned) == 1


def test_digest_now_spawn_failure_clears_flag(tmp_db, send, monkeypatch):
    def broken(target):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(digest, '_spawn', broken)
    with pytest.raises(RuntimeError):
        digest.start_digest_now(send, OWNER, False)
    monkeypatch.setattr(digest, '_spawn', lambda target: None)
    assert digest.start_digest_now(send, OWNER, False) is True


def test_digest_now_habr_failure_reports_reason_to_owner(tmp_db, habr, send, monkeypatch):
    habr.status = 503
    sync_spawn(monkeypatch)
    monkeypatch.setattr(digest, '_now', lambda: at(12, 0))
    assert digest.start_digest_now(send, OWNER, True) is True
    assert send.sent == [(OWNER, 'Дайджест не собран: Хабр не ответил (HTTP 503)', None)]
    for table in ('digest_runs', 'digest_deliveries', 'ranked_lists', 'habr_articles'):
        assert _count(table) == 0, table
    assert digest.start_digest_now(send, OWNER, False) is True        # flag cleared


def test_digest_now_default_thread_runs_work(tmp_db, habr, send, monkeypatch):
    fill_day(habr, site=2)
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', None)
    monkeypatch.setattr(digest, '_now', lambda: at(12, 0))
    threads = []
    real_thread = digest.threading.Thread

    def capture(*a, **kw):
        t = real_thread(*a, **kw)
        threads.append(t)
        return t

    monkeypatch.setattr(digest.threading, 'Thread', capture)
    assert digest.start_digest_now(send, OWNER, False)
    threads[0].join(timeout=10)
    assert threads[0].daemon
    assert len(send.to(OWNER)) == 1


# =============================================================================
# Default describe (contract R1)
# =============================================================================


def test_default_describe_is_real_function(tmp_db, habr, send, sleeps, monkeypatch, _no_real_llm):
    assert digest._DEFAULT_DESCRIBE is summarizer.generate_short_description
    fake = Describe()
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', fake)
    fill_day(habr, site=1)
    digest.tick(at(18, 0), send=send, owner_id=OWNER, is_allowed=allowed)   # describe omitted
    assert fake.calls == ['Статья 100']
    assert _no_real_llm == []


def test_run_scheduler_uses_default_describe(tmp_db, habr, send, monkeypatch, _no_real_llm):
    fake = Describe()
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', fake)
    fill_day(habr, site=1)
    monkeypatch.setattr(digest, '_now', lambda: at(18, 0))

    def stop(seconds):
        if seconds == digest.TICK_SECONDS:
            raise _StopLoop

    monkeypatch.setattr(digest, '_sleep', stop)
    with pytest.raises(_StopLoop):
        digest.run_scheduler(send, OWNER, allowed)
    assert fake.calls == ['Статья 100']
    assert _no_real_llm == []


def test_start_digest_now_uses_default_describe(tmp_db, habr, send, monkeypatch, _no_real_llm):
    fake = Describe()
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', fake)
    fill_day(habr, site=1)
    monkeypatch.setattr(digest, '_now', lambda: at(12, 0))
    sync_spawn(monkeypatch)
    assert digest.start_digest_now(send, OWNER, False)
    assert fake.calls == ['Статья 100']
    assert _no_real_llm == []


def test_explicit_none_describe_means_fallback(tmp_db, habr, send, sleeps, monkeypatch, capsys):
    fake = Describe()
    monkeypatch.setattr(digest, '_DEFAULT_DESCRIBE', fake)
    monkeypatch.setattr(summarizer, 'generate_short_description', fake)
    fill_day(habr, site=2)
    tick(at(18, 0), send, describe=None)
    fallback = feed.fallback_description(LEAD, limit=digest.DESCRIPTION_LIMIT)
    assert fallback in send.to(OWNER)[0][0]
    result = digest.dry_run(describe=None, now=at(18, 0))
    assert fallback in result['parts'][0][0]
    assert fake.calls == []
