"""Read-only `djlib catalog export` data collection and writers."""

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from djlib.catalog.queries import active_track_for_file
from djlib.catalog.service import CatalogService
from djlib.db.models import FileQualityAnalysis, FileRecord
from djlib.export.html_table import Column, render_table_html

CATALOG_EXPORT_FIELDS = (
    'file_public_id',
    'relative_path',
    'is_present',
    'track_public_id',
    'track_status',
    'artist',
    'title',
    'version',
    'edition',
    'featured_artists',
    'genre',
    'bpm',
    'key',
    'duration_ms',
    'codec',
    'bitrate',
    'sample_rate',
    'size_bytes',
    'quality_score',
)

_HTML_COLUMNS = (
    Column('file_public_id', 'File ID'),
    Column('relative_path', 'Path'),
    Column('is_present', 'Present'),
    Column('track_public_id', 'Track ID'),
    Column('track_status', 'Track status'),
    Column('artist', 'Artist'),
    Column('title', 'Title'),
    Column('version', 'Version'),
    Column('edition', 'Edition'),
    Column('featured_artists', 'Featured artists'),
    Column('genre', 'Genre'),
    Column('bpm', 'BPM'),
    Column('key', 'Key'),
    Column('duration_ms', 'Duration (ms)'),
    Column('codec', 'Codec'),
    Column('bitrate', 'Bitrate'),
    Column('sample_rate', 'Sample rate'),
    Column('size_bytes', 'Size (bytes)'),
    Column('quality_score', 'Quality score'),
)


@dataclass(frozen=True)
class CatalogExportRow:
    file_public_id: str
    relative_path: str
    is_present: bool
    track_public_id: str | None
    track_status: str | None
    artist: str | None
    title: str | None
    version: str | None
    edition: str | None
    featured_artists: str
    genre: str | None
    bpm: str | None
    key: str | None
    duration_ms: int | None
    codec: str | None
    bitrate: int | None
    sample_rate: int | None
    size_bytes: int
    quality_score: float | None


def _latest_quality_score(session: Session, file_id: int) -> float | None:
    row = session.execute(
        select(FileQualityAnalysis)
        .where(FileQualityAnalysis.file_id == file_id)
        .order_by(FileQualityAnalysis.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.quality_score if row is not None else None


_EXPORT_YIELD_PER = 200


def collect_catalog_export_rows(session: Session) -> list[CatalogExportRow]:
    """Streams `FileRecord`s in batches rather than materializing the whole
    catalogue up front -- with each row carrying a `raw_metadata_json` blob,
    a plain unbatched query loads every file before producing the first
    output row, holding the entire library resident in memory for the whole
    export. `execution_options(yield_per=...)` makes SQLAlchemy fetch (and
    instantiate) rows in bounded chunks instead."""
    catalog_service = CatalogService(session)

    rows: list[CatalogExportRow] = []
    files = session.execute(
        select(FileRecord)
        .order_by(FileRecord.relative_path)
        .execution_options(yield_per=_EXPORT_YIELD_PER)
    ).scalars()
    for file in files:
        track = active_track_for_file(session, file.id)
        if track is not None:
            identity = catalog_service.effective_identity(track)
            artist = identity.artist
            title = identity.title
            version = identity.version
            edition = identity.edition
            featured_artists = ', '.join(fa.name for fa in identity.featured_artists)
            track_public_id = track.public_id
            track_status = track.status.value
        else:
            artist = file.artist_raw
            title = file.title_raw
            version = None
            edition = None
            featured_artists = ''
            track_public_id = None
            track_status = None

        rows.append(
            CatalogExportRow(
                file_public_id=file.public_id,
                relative_path=file.relative_path,
                is_present=file.is_present,
                track_public_id=track_public_id,
                track_status=track_status,
                artist=artist,
                title=title,
                version=version,
                edition=edition,
                featured_artists=featured_artists,
                genre=file.genre_raw,
                bpm=file.bpm_raw,
                key=file.key_raw,
                duration_ms=file.duration_ms,
                codec=file.codec,
                bitrate=file.bitrate,
                sample_rate=file.sample_rate,
                size_bytes=file.size_bytes,
                quality_score=_latest_quality_score(session, file.id),
            )
        )
    return rows


def write_catalog_csv(rows: Iterable[CatalogExportRow], stream: TextIO) -> None:
    writer = csv.writer(stream)
    writer.writerow(CATALOG_EXPORT_FIELDS)
    for row in rows:
        writer.writerow([getattr(row, field) for field in CATALOG_EXPORT_FIELDS])


def write_catalog_html(rows: Iterable[CatalogExportRow], generated_at: str, stream: TextIO) -> None:
    table_rows = [
        {field: getattr(row, field) for field in CATALOG_EXPORT_FIELDS} for row in rows
    ]
    stream.write(
        render_table_html(
            title='djlib catalogue',
            generated_at=generated_at,
            columns=list(_HTML_COLUMNS),
            rows=table_rows,
        )
    )
