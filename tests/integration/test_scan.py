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


def test_new_unchanged_changed_missing_lifecycle(config: DjlibConfig, engine: Engine) -> None:
    config.music_root.mkdir(parents=True)
    fixture = config.music_root / 'track.mp3'
    fixture.write_bytes(b'x' * 10)

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

    fixture.write_bytes(b'y' * 20)
    changed_run = service.scan()
    assert changed_run.files_seen == 1
    assert changed_run.files_new == 0
    assert changed_run.files_changed == 1
    assert changed_run.files_unchanged == 0
    assert changed_run.files_missing == 0
    changed_record = _file_record(session_maker, 'track.mp3')
    assert changed_record.is_present is True
    assert changed_record.size_bytes == 20

    fixture.unlink()
    missing_run = service.scan()
    assert missing_run.files_seen == 0
    assert missing_run.files_new == 0
    assert missing_run.files_changed == 0
    assert missing_run.files_unchanged == 0
    assert missing_run.files_missing == 1
    assert _file_record(session_maker, 'track.mp3').is_present is False

    assert new_run.public_id != unchanged_run.public_id != changed_run.public_id != missing_run.public_id
