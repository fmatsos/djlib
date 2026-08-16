import wave
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from djlib.config import DjlibConfig
from djlib.db.models import FileRecord
from djlib.db.session import session_factory
from djlib.metadata.extractor import MetadataExtractor
from djlib.metadata.types import ExtractedMetadata
from djlib.scan import service as scan_service
from djlib.scan.service import ScanService


def _write_valid_wav(path: Path) -> None:
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(b'\x00\x00\x00\x00' * 44100)


class _FailAfterNCallsExtractor:
    """Delegates to a real extractor, but raises an uncaught error on the
    Nth call -- simulating an infrastructure crash partway through a scan,
    as opposed to the per-file `MetadataExtractionError` the scan loop
    already handles and continues past."""

    def __init__(self, delegate: MetadataExtractor, fail_at_call: int) -> None:
        self._delegate = delegate
        self._fail_at_call = fail_at_call
        self._calls = 0

    def extract(self, path: Path) -> ExtractedMetadata:
        self._calls += 1
        if self._calls == self._fail_at_call:
            raise RuntimeError('synthetic mid-scan crash')
        return self._delegate.extract(path)


def _committed_file_count(session_maker: sessionmaker[Session]) -> int:
    with session_maker() as session:
        return session.execute(select(func.count()).select_from(FileRecord)).scalar_one()


def test_scan_commits_progress_in_batches_so_a_mid_scan_crash_does_not_lose_everything(
    config: DjlibConfig, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-library scan must not hold every touched row uncommitted in
    memory until one final commit at the very end -- on a large library that
    grows memory unboundedly for the entire run. Committing in small batches
    bounds memory *and*, as a side effect, means a crash partway through
    doesn't discard already-processed files."""
    config.music_root.mkdir(parents=True)
    for index in range(4):
        _write_valid_wav(config.music_root / f'track{index}.wav')

    monkeypatch.setattr(scan_service, '_SCAN_COMMIT_BATCH_SIZE', 2)

    session_maker = session_factory(engine)
    extractor = _FailAfterNCallsExtractor(MetadataExtractor.create(), fail_at_call=3)
    service = ScanService(config, session_maker, metadata_extractor=extractor)

    with pytest.raises(RuntimeError, match='synthetic mid-scan crash'):
        service.scan(full=True)

    # Files 1-2 were committed as a batch before the 3rd file's extraction
    # crashed the run; that progress must survive even though the run as a
    # whole failed.
    assert _committed_file_count(session_maker) == 2
