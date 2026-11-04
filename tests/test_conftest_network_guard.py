"""Checks for the autouse Habr network guard in conftest.py (contract C4).

The two throttle tests run in file order: the first leaves a throttle
timestamp behind, the second must see a fresh (reset) state.
"""

import pytest
import requests

import habr_api
from conftest import RealHabrRequestBlocked


@pytest.mark.parametrize('url', [
    'https://habr.com/kek/v2/articles/1/',
    'https://m.habr.com/ru/articles/1/',
    'http://HABR.com/',
])
def test_real_habr_request_is_blocked(url):
    with pytest.raises(RealHabrRequestBlocked):
        requests.get(url, timeout=1)
    with pytest.raises(RealHabrRequestBlocked):
        requests.Session().get(url, timeout=1)


def test_throttle_state_is_dirtied_by_a_request(monkeypatch):
    class _Resp:
        status_code = 200
        headers: dict = {}

        def iter_content(self, chunk_size=1):
            yield b'{"httpCode":404,"errorCode":"NOT_FOUND"}'

        def close(self):
            pass

    monkeypatch.setattr(habr_api.requests, 'get', lambda *a, **kw: _Resp())
    with pytest.raises(habr_api.HabrUnavailable):
        habr_api.fetch_article(1)

    assert habr_api._last_request_at is not None


def test_throttle_state_is_reset_between_tests():
    assert habr_api._last_request_at is None
    assert habr_api.MIN_REQUEST_INTERVAL == 0
    assert not habr_api._lock.locked()
