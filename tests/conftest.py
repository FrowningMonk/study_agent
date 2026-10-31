import sys
from pathlib import Path

import pytest

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
