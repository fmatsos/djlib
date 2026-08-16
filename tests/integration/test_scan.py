import wave
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker, Session

from djlib.config import DjlibConfig
from djlib.db.models import FileRecord
from djlib.db.session import session_factory
from djlib.scan.service import ScanService


def _file_record(session_maker: sessionmaker[Session], relative_path: str) -> FileRecord:
    with session_maker() as session:
        return session.execute(
            select(FileRecord).where(FileRecord.relative_path == relative_path)
        ).scalar_one()


def _write_valid_wav(path: Path, num_frames: int) -> None:
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b'\x00\x00' * num_frames)


def test_new_unchanged_changed_missing_lifecycle(config: DjlibConfig, engine: Engine) -> None:
    config.music_root.mkdir(parents=True)
    fixture = config.music_root / 'track.mp3'
    _write_valid_wav(fixture, num_frames=5)

    session_maker = session_factory(engine)
    service = ScanService(config, session_maker)

    new_run = service.scan()
    assert new_run.files_seen == 1
    assert new_run.files_new == 1
    assert new_run.files_changed == 0
    assert new_run.files_unchanged == 0
    assert new_run.files_missing == 0
    assert new_run.files_failed == 0
    assert _file_record(session_maker, 'track.mp3').is_present is True

    unchanged_run = service.scan()
    assert unchanged_run.files_seen == 1
    assert unchanged_run.files_new == 0
    assert unchanged_run.files_changed == 0
    assert unchanged_run.files_unchanged == 1
    assert unchanged_run.files_missing == 0
    assert _file_record(session_maker, 'track.mp3').is_present is True

    _write_valid_wav(fixture, num_frames=10)
    changed_run = service.scan()
    assert changed_run.files_seen == 1
    assert changed_run.files_new == 0
    assert changed_run.files_changed == 1
    assert changed_run.files_unchanged == 0
    assert changed_run.files_missing == 0
    changed_record = _file_record(session_maker, 'track.mp3')
    assert changed_record.is_present is True
    assert changed_record.size_bytes == fixture.stat().st_size

    fixture.unlink()
    missing_run = service.scan()
    assert missing_run.files_seen == 0
    assert missing_run.files_new == 0
    assert missing_run.files_changed == 0
    assert missing_run.files_unchanged == 0
    assert missing_run.files_missing == 1
    assert _file_record(session_maker, 'track.mp3').is_present is False

    assert new_run.public_id != unchanged_run.public_id != changed_run.public_id != missing_run.public_id


def test_scan_reports_progress_per_discovered_file(config: DjlibConfig, engine: Engine) -> None:
    config.music_root.mkdir(parents=True)
    _write_valid_wav(config.music_root / 'a.mp3', num_frames=5)
    _write_valid_wav(config.music_root / 'b.mp3', num_frames=5)

    session_maker = session_factory(engine)
    service = ScanService(config, session_maker)

    calls: list[tuple[str, int, int]] = []
    service.scan(progress=lambda stage, current, total: calls.append((stage, current, total)))

    assert calls == [('scanning', 0, 2), ('scanning', 1, 2), ('scanning', 2, 2)]
