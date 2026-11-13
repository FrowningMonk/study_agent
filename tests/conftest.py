import itertools
import json
import os
import re
import sys
import threading
from datetime import timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from telebot import types

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'tests' / 'fixtures'

# Modules live in the repo root (no package), make them importable
sys.path.insert(0, str(ROOT))

# bot.py fails at import without a token and OWNER_ID; a .env (if any) overrides them
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:TEST')
os.environ.setdefault('OWNER_ID', '1000')


class KekResponse:
    """Streamed kek response as habr_api._request reads it (JSON-encodes dict/list bodies)."""

    def __init__(self, status: int, body=b'', headers: dict | None = None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def iter_content(self, chunk_size=1):
        yield self._body

    def close(self):
        pass


# Live kek envelope for an article hidden into drafts (id 1103492, probed 2026-11-05;
# author data trimmed)
IN_DRAFTS_ENVELOPE = {
    'httpCode': 403, 'message': 'post drafted',
    'data': {'author': {'id': '3172623', 'alias': 'sokolovps'}}, 'errorCode': 'IN_DRAFTS',
}


class SignallingLock:
    """Lock proxy for deterministic contention tests.

    `contended` is set when acquire() is called while the lock is already held, i.e.
    right before a second thread blocks on it. Install it with monkeypatch over a
    module-level lock that the code under test looks up at call time.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        if self._lock.locked():
            self.contended.set()
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()


def rss_body(entries) -> str:
    """A Habr RSS channel (same markup as the live feed) for (habr_id, aware published) entries.

    An entry may also be (habr_id, published, title)."""
    items = []
    for entry in entries:
        habr_id, published = entry[0], entry[1]
        title = entry[2] if len(entry) > 2 else f'Статья {habr_id}'
        items.append(
            '<item>\n'
            f'<title><![CDATA[{title}]]></title>\n'
            f'<guid isPermaLink="true">https://habr.com/ru/articles/{habr_id}/</guid>\n'
            f'<link>https://habr.com/ru/articles/{habr_id}/?utm_campaign={habr_id}&amp;utm_source=habrahabr</link>\n'
            '<description><![CDATA[<p>Анонс.</p>]]></description>\n'
            f'<pubDate>{format_datetime(published.astimezone(timezone.utc), usegmt=True)}</pubDate>\n'
            '<category><![CDATA[python]]></category>\n'
            '</item>'
        )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0">\n<channel>\n'
            '<title><![CDATA[Хабр]]></title>\n' + '\n'.join(items) + '\n</channel>\n</rss>\n')


def kek_article_id(url: str) -> int | None:
    """habr_id of a kek `articles/{id}/` URL, else None."""
    match = re.fullmatch(r'https://habr\.com/kek/v2/articles/(\d+)/', url)
    return int(match.group(1)) if match else None


@pytest.fixture
def fixture_text():
    def _read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding='utf-8')
    return _read


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Isolated SQLite file per test: database.py reads DATA_DIR/DB_PATH at call time."""
    import database
    monkeypatch.setattr(database, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(database, 'DB_PATH', str(tmp_path / 'test.db'))
    database.init_db()
    return database


# =============================================================================
# Habr network guard + throttle reset (applies to every test file)
# =============================================================================


class RealHabrRequestBlocked(RuntimeError):
    """Raised when a test reaches the real network for habr.com."""


def _is_habr_host(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    return host == 'habr.com' or host.endswith('.habr.com')


@pytest.fixture(autouse=True)
def _habr_network_guard(monkeypatch):
    """Block real HTTP to habr.com and reset `habr_api` throttle state.

    The guard sits on the transport adapter, below `requests.get` and
    `Session.request`, so tests that monkeypatch `requests` themselves keep
    working. The throttle interval is set to 0 so fake-network tests never sleep;
    a test that needs real spacing patches `MIN_REQUEST_INTERVAL` back up.
    """
    real_send = requests.adapters.HTTPAdapter.send

    def guarded_send(self, request, *args, **kwargs):
        if _is_habr_host(request.url):
            raise RealHabrRequestBlocked(f'real request to Habr in a test: {request.url}')
        return real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', guarded_send)

    import habr_api
    monkeypatch.setattr(habr_api, '_lock', threading.Lock())
    monkeypatch.setattr(habr_api, '_last_request_at', None)
    monkeypatch.setattr(habr_api, 'MIN_REQUEST_INTERVAL', 0)

    # Module state shared across tests: feed fetch cache + REGISTRY, hubs refresh lock
    import digest
    import feed
    import hubs
    feed._reset_for_tests()
    hubs._reset_for_tests()
    digest._reset_for_tests()
    yield
    feed._reset_for_tests()
    hubs._reset_for_tests()
    digest._reset_for_tests()


# =============================================================================
# Telegram bot harness: real dispatch, no network, no threads
# =============================================================================

STRANGER = 2000
FRIEND = 3000

# Per-user session dicts of bot.py, replaced with fresh ones for every test
_SESSION_STATES = ('pending_model_selection', 'pending_article_links', 'pending_reassign',
                   'pending_assign_list', 'pending_md_generation', 'pending_cache_urls')

_RECORDED_METHODS = ('send_message', 'answer_callback_query', 'edit_message_text', 'reply_to',
                     'set_my_commands', 'send_chat_action', 'delete_message', 'edit_message_reply_markup')


class _Sent:
    """Return value of a recorded bot call; only message_id is used by bot code."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


@pytest.fixture
def tg(tmp_db, monkeypatch):
    """Bot without network or threads. Returns the list of (name, args, kwargs) calls it made."""
    import bot as bot_module
    sent: list[tuple[str, tuple, dict]] = []
    ids = itertools.count(1000)
    b = bot_module.bot

    def recorder(name):
        def record(*a, **kw):
            sent.append((name, a, kw))
            return _Sent(next(ids))
        return record

    for name in _RECORDED_METHODS:
        monkeypatch.setattr(b, name, recorder(name))
    monkeypatch.setattr(b, 'threaded', False)
    monkeypatch.setattr(b, 'owner_menu_set', True)
    for state in _SESSION_STATES:
        monkeypatch.setattr(bot_module, state, {})
    tmp_db.create_access_request(FRIEND, 'friend', 'Друг')
    tmp_db.set_user_status(FRIEND, 'approved')
    yield sent
    for chat_id in (STRANGER, FRIEND, bot_module.OWNER_ID):
        b.clear_step_handler_by_chat_id(chat_id)


def _buttons(markup) -> list[str]:
    return [btn.callback_data for row in markup.keyboard for btn in row]


def _sent_to(sent: list, chat_id: int) -> list:
    return [s for s in sent if s[0] == 'send_message' and s[1][0] == chat_id]


def _message_update(user_id: int, text: str, chat_type: str = 'private', update_id: int = 1) -> types.Update:
    return types.Update.de_json({
        'update_id': update_id,
        'message': {
            'message_id': 10,
            'date': 0,
            'chat': {'id': user_id, 'type': chat_type},
            'from': {'id': user_id, 'is_bot': False, 'first_name': 'U'},
            'text': text,
        },
    })


def _callback_update(user_id: int, data: str, update_id: int = 2) -> types.Update:
    return types.Update.de_json({
        'update_id': update_id,
        'callback_query': {
            'id': 'cb1',
            'from': {'id': user_id, 'is_bot': False, 'first_name': 'U', 'username': 'u'},
            'chat_instance': 'x',
            'data': data,
            'message': {
                'message_id': 11,
                'date': 0,
                'chat': {'id': user_id, 'type': 'private'},
                'text': 'old',
            },
        },
    })
