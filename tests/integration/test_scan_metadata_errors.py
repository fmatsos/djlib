import wave
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from djlib.config import DjlibConfig
from djlib.db.enums import ScanStatus
from djlib.db.models import FileRecord
from djlib.db.session import session_factory
from djlib.scan.service import ScanService


def _write_valid_wav(path: Path) -> None:
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(b'\x00\x00\x00\x00' * 44100)


def _file_record(session_maker: sessionmaker[Session], relative_path: str) -> FileRecord:
    with session_maker() as session:
        return session.execute(
            select(FileRecord).where(FileRecord.relative_path == relative_path)
        ).scalar_one()


def test_corrupt_file_is_isolated_and_valid_file_still_extracted(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    _write_valid_wav(config.music_root / 'valid.wav')
    (config.music_root / 'corrupt.mp3').write_bytes(b'not a real mp3 file' * 5)

    session_maker = session_factory(engine)
    service = ScanService(config, session_maker)

    summary = service.scan(full=True)

    assert summary.status == ScanStatus.SUCCESS_WITH_ERRORS
    assert summary.files_failed == 1
    assert summary.files_seen == 2

    valid_record = _file_record(session_maker, 'valid.wav')
    assert valid_record.metadata_updated_at is not None
    assert valid_record.sample_rate == 44100
    assert valid_record.channels == 2
    assert valid_record.duration_ms is not None
    assert valid_record.duration_ms > 0

    corrupt_record = _file_record(session_maker, 'corrupt.mp3')
    assert corrupt_record.metadata_updated_at is None
    assert corrupt_record.duration_ms is None
    assert corrupt_record.is_present is True
