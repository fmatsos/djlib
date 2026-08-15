from pathlib import Path
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from djlib.config import DjlibConfig
from djlib.db.engine import create_engine_for_config


@pytest.fixture
def config(tmp_path: Path) -> DjlibConfig:
    return DjlibConfig(music_root=tmp_path / 'music', data_root=tmp_path / 'data')


@pytest.fixture
def engine(config: DjlibConfig) -> Iterator[Engine]:
    eng = create_engine_for_config(config)
    yield eng
    eng.dispose()
