import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from djlib.catalog.queries import active_track_for_file
from djlib.catalog.service import CatalogService
from djlib.config import DjlibConfig
from djlib.db.enums import AnalysisStatus, ScanStatus
from djlib.db.models import FileFeaturedArtist, FileRecord, ScanRun
from djlib.ids import new_public_id
from djlib.metadata.extractor import MetadataExtractor, ensure_required_executables
from djlib.metadata.types import ExtractedMetadata, MetadataExtractionError
from djlib.progress import ProgressReporter, null_progress
from djlib.resolve.normalizer import normalize_identity
from djlib.resolve.resolver import MetadataResolver
from djlib.resolve.types import RawIdentity
from djlib.scan.scanner import discover_audio_files

SCANNER_VERSION = '1'
MAX_ERROR_SUMMARY_PATHS = 20

# Committing only once at the very end of a whole-library scan holds every
# touched FileRecord/Track row (and their metadata blobs) live in the
# session's identity map for the run's entire duration -- memory grows
# unboundedly with library size. Committing in batches bounds it instead.
_SCAN_COMMIT_BATCH_SIZE = 200


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
    def __init__(
        self,
        config: DjlibConfig,
        session_maker: sessionmaker[Session],
        metadata_extractor: MetadataExtractor | None = None,
    ) -> None:
        self._config = config
        self._session_maker = session_maker
        self._metadata_extractor = metadata_extractor or MetadataExtractor.create()
        self._resolver = MetadataResolver()

    def scan(self, full: bool = False, progress: ProgressReporter = null_progress) -> ScanSummary:
        ensure_required_executables()

        discovered = list(discover_audio_files(self._config.music_root))
        seen_paths = {item.relative_path for item in discovered}
        now = _now()
        total = len(discovered)
        progress('scanning', 0, total)

        files_new = files_changed = files_unchanged = files_missing = files_failed = 0
        failed_paths: list[str] = []

        with self._session_maker() as session:
            existing = {
                record.relative_path: record
                for record in session.execute(select(FileRecord)).scalars()
            }

            catalog_service = CatalogService(session)

            for index, item in enumerate(discovered, start=1):
                progress('scanning', index, total)
                record = existing.get(item.relative_path)
                is_new = record is None
                needs_extraction: bool
                if record is None:
                    record = FileRecord(
                        public_id=new_public_id('fil'),
                        relative_path=item.relative_path,
                        size_bytes=item.size_bytes,
                        mtime_ns=item.mtime_ns,
                        extension=Path(item.relative_path).suffix.lower(),
                        is_present=True,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                    session.add(record)
                    session.flush()
                    files_new += 1
                    needs_extraction = True
                elif record.size_bytes == item.size_bytes and record.mtime_ns == item.mtime_ns:
                    record.is_present = True
                    record.last_seen_at = now
                    files_unchanged += 1
                    needs_extraction = full
                    if full:
                        # A forced full re-extraction re-derives metadata even though the
                        # source signature did not change -- treat it the same as a real
                        # content change for cached analysis (design §5): the cache keyed
                        # on (size_bytes, mtime_ns) can't tell "unchanged" apart from "we
                        # decided to redo it anyway", so it must be invalidated explicitly.
                        _mark_analysis_stale(record)
                else:
                    record.size_bytes = item.size_bytes
                    record.mtime_ns = item.mtime_ns
                    record.is_present = True
                    record.last_seen_at = now
                    files_changed += 1
                    needs_extraction = True
                    _mark_analysis_stale(record)

                resolved_this_scan = False
                if needs_extraction:
                    try:
                        extracted = self._metadata_extractor.extract(
                            self._config.music_root / item.relative_path
                        )
                    except MetadataExtractionError:
                        files_failed += 1
                        failed_paths.append(item.relative_path)
                    else:
                        _apply_metadata(record, extracted, now)
                        self._apply_resolution(session, record)
                        resolved_this_scan = True

                # A file always owns exactly one provisional track from the moment it is
                # first seen, even if metadata extraction failed for it this scan -- see
                # design §8. Rescans never merge tracks by resemblance; a CHANGED file's
                # existing track only has its copied identity fields refreshed in place.
                if is_new:
                    catalog_service.create_provisional_track(record)
                elif resolved_this_scan:
                    track = active_track_for_file(session, record.id)
                    if track is None:
                        catalog_service.create_provisional_track(record)
                    else:
                        catalog_service.refresh_track_identity(track, record)

                if index % _SCAN_COMMIT_BATCH_SIZE == 0:
                    session.commit()

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
                error_summary=_error_summary(failed_paths),
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

    def _apply_resolution(self, session: Session, record: FileRecord) -> None:
        file_name = Path(record.relative_path).name
        resolved = self._resolver.resolve(
            file_name, RawIdentity(artist=record.artist_raw, title=record.title_raw)
        )

        record.resolved_artist = resolved.artist.value
        record.artist_source = resolved.artist.source
        record.resolved_title = resolved.title.value
        record.title_source = resolved.title.source
        record.resolved_version = resolved.version.value
        record.version_source = resolved.version.source
        record.resolved_edition = resolved.edition.value
        record.edition_source = resolved.edition.source

        if record.id is None:
            session.flush()
        session.execute(delete(FileFeaturedArtist).where(FileFeaturedArtist.file_id == record.id))
        for entry in resolved.featured_artists:
            session.add(
                FileFeaturedArtist(
                    file_id=record.id,
                    position=entry.position,
                    name=entry.name,
                    normalized_name=normalize_identity(entry.name),
                    source=entry.source,
                )
            )


def _mark_analysis_stale(record: FileRecord) -> None:
    """Invalidate cached duplicate-detection evidence for a changed/re-extracted file.

    Per design §5, a changed source signature must invalidate binary hash ->
    Chromaprint -> quality analysis in that order. There is no `analyzer_version`
    column backing `binary_hash_status`/`chromaprint_status` (Task 8 in the
    implementation plan): the entire caching contract for those two fields rests
    on `ScanService` being the sole writer of `size_bytes`/`mtime_ns` and always
    flipping status to STALE in the same transaction it changes them (or forces
    a full re-extraction). `quality_status` has no analyzer yet (Task 9) but is
    set here too so that analyzer inherits a correctly-invalidated cache.
    """
    record.binary_hash_status = AnalysisStatus.STALE
    record.chromaprint_status = AnalysisStatus.STALE
    record.quality_status = AnalysisStatus.STALE


def _apply_metadata(record: FileRecord, extracted: ExtractedMetadata, now: dt.datetime) -> None:
    raw = extracted.raw
    technical = extracted.technical

    record.title_raw = raw.title
    record.artist_raw = raw.artist
    record.album_raw = raw.album
    record.album_artist_raw = raw.album_artist
    record.genre_raw = raw.genre
    record.bpm_raw = raw.bpm
    record.key_raw = raw.key
    record.comment_raw = raw.comment
    record.raw_metadata_json = raw.raw_json

    record.container_format = technical.container_format
    record.codec = technical.codec
    record.bitrate = technical.bitrate
    record.sample_rate = technical.sample_rate
    record.bit_depth = technical.bit_depth
    record.channels = technical.channels
    record.duration_ms = technical.duration_ms

    record.metadata_updated_at = now


def _error_summary(failed_paths: list[str]) -> str | None:
    if not failed_paths:
        return None
    shown = failed_paths[:MAX_ERROR_SUMMARY_PATHS]
    suffix = '' if len(failed_paths) <= MAX_ERROR_SUMMARY_PATHS else ', ...'
    return f'metadata extraction failed for {len(failed_paths)} file(s): {", ".join(shown)}{suffix}'
