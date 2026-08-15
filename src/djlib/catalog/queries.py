from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from djlib.db.enums import TrackStatus
from djlib.db.models import FileRecord, ScanRun, Track, TrackFile


@dataclass(frozen=True)
class CatalogStats:
    files_total: int
    files_present: int
    files_missing: int
    track_status_counts: dict[str, int]
    scan_runs_total: int
    scan_files_failed_total: int
    latest_scan_public_id: str | None
    latest_scan_status: str | None


def compute_catalog_stats(session: Session) -> CatalogStats:
    files_total = session.execute(select(func.count()).select_from(FileRecord)).scalar_one()
    files_present = session.execute(
        select(func.count()).select_from(FileRecord).where(FileRecord.is_present.is_(True))
    ).scalar_one()

    track_status_counts = {status.value: 0 for status in TrackStatus}
    for status, count in session.execute(
        select(Track.status, func.count()).group_by(Track.status)
    ).all():
        track_status_counts[status.value] = count

    scan_runs_total = session.execute(select(func.count()).select_from(ScanRun)).scalar_one()
    scan_files_failed_total = session.execute(
        select(func.coalesce(func.sum(ScanRun.files_failed), 0))
    ).scalar_one()

    latest_scan = session.execute(
        select(ScanRun).order_by(ScanRun.started_at.desc(), ScanRun.id.desc()).limit(1)
    ).scalar_one_or_none()

    return CatalogStats(
        files_total=files_total,
        files_present=files_present,
        files_missing=files_total - files_present,
        track_status_counts=track_status_counts,
        scan_runs_total=scan_runs_total,
        scan_files_failed_total=int(scan_files_failed_total),
        latest_scan_public_id=latest_scan.public_id if latest_scan else None,
        latest_scan_status=latest_scan.status.value if latest_scan else None,
    )


def find_file_by_public_id(session: Session, public_id: str) -> FileRecord | None:
    return session.execute(
        select(FileRecord).where(FileRecord.public_id == public_id)
    ).scalar_one_or_none()


def find_track_by_public_id(session: Session, public_id: str) -> Track | None:
    return session.execute(select(Track).where(Track.public_id == public_id)).scalar_one_or_none()


def active_track_for_file(session: Session, file_id: int) -> Track | None:
    return session.execute(
        select(Track)
        .join(TrackFile, TrackFile.track_id == Track.id)
        .where(TrackFile.file_id == file_id, TrackFile.is_active.is_(True))
    ).scalar_one_or_none()


def active_files_for_track(session: Session, track_id: int) -> list[FileRecord]:
    return list(
        session.execute(
            select(FileRecord)
            .join(TrackFile, TrackFile.file_id == FileRecord.id)
            .where(TrackFile.track_id == track_id, TrackFile.is_active.is_(True))
            .order_by(FileRecord.relative_path)
        ).scalars()
    )
