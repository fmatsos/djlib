import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from djlib.config import DjlibConfig
from djlib.db.enums import ScanStatus
from djlib.db.models import FileRecord, ScanRun
from djlib.ids import new_public_id
from djlib.scan.scanner import discover_audio_files

SCANNER_VERSION = '1'


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class ScanSummary:
    public_id: str
    status: ScanStatus
    files_seen: int
    files_new: int
    files_changed: int
    files_unchanged: int
    files_missing: int
    files_failed: int


class ScanService:
    def __init__(self, config: DjlibConfig, session_maker: sessionmaker[Session]) -> None:
        self._config = config
        self._session_maker = session_maker

    def scan(self, full: bool = False) -> ScanSummary:
        # `full` is threaded through per the Task 3 interface; it does not yet change
        # behavior since forced re-extraction is owned by Task 4.
        discovered = list(discover_audio_files(self._config.music_root))
        seen_paths = {item.relative_path for item in discovered}
        now = _now()

        files_new = files_changed = files_unchanged = files_missing = files_failed = 0

        with self._session_maker() as session:
            existing = {
                record.relative_path: record
                for record in session.execute(select(FileRecord)).scalars()
            }

            for item in discovered:
                record = existing.get(item.relative_path)
                if record is None:
                    session.add(
                        FileRecord(
                            public_id=new_public_id('fil'),
                            relative_path=item.relative_path,
                            size_bytes=item.size_bytes,
                            mtime_ns=item.mtime_ns,
                            extension=Path(item.relative_path).suffix.lower(),
                            is_present=True,
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                    )
                    files_new += 1
                elif record.size_bytes == item.size_bytes and record.mtime_ns == item.mtime_ns:
                    record.is_present = True
                    record.last_seen_at = now
                    files_unchanged += 1
                else:
                    record.size_bytes = item.size_bytes
                    record.mtime_ns = item.mtime_ns
                    record.is_present = True
                    record.last_seen_at = now
                    files_changed += 1

            for relative_path, record in existing.items():
                if relative_path not in seen_paths:
                    record.is_present = False
                    files_missing += 1

            status = ScanStatus.SUCCESS_WITH_ERRORS if files_failed else ScanStatus.SUCCESS
            run = ScanRun(
                public_id=new_public_id('scan'),
                started_at=now,
                ended_at=_now(),
                status=status,
                files_seen=len(discovered),
                files_new=files_new,
                files_changed=files_changed,
                files_unchanged=files_unchanged,
                files_missing=files_missing,
                files_failed=files_failed,
                scanner_version=SCANNER_VERSION,
            )
            session.add(run)
            session.commit()

            return ScanSummary(
                public_id=run.public_id,
                status=run.status,
                files_seen=run.files_seen,
                files_new=run.files_new,
                files_changed=run.files_changed,
                files_unchanged=run.files_unchanged,
                files_missing=run.files_missing,
                files_failed=run.files_failed,
            )
