import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from newsbot.config import Settings, load_assets, load_sources  # noqa: E402
from newsbot.storage.db import Database  # noqa: E402

T0 = datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc)  # a Monday


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def db():
    return Database("sqlite:///:memory:")


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.paths.kill_file = str(tmp_path / "KILL")
    s.paths.paper_state = str(tmp_path / "paper.json")
    return s


@pytest.fixture
def assets():
    return load_assets(ROOT / "config" / "assets.yaml")


@pytest.fixture
def sources_cfg():
    return load_sources(ROOT / "config" / "sources.yaml")
