import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'tests' / 'fixtures'

# Modules live in the repo root (no package), make them importable
sys.path.insert(0, str(ROOT))


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
