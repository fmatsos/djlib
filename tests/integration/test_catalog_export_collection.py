import wave
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from djlib.catalog import export as catalog_export
from djlib.catalog.export import collect_catalog_export_rows
from djlib.catalog.service import CatalogService
from djlib.catalog.queries import active_track_for_file
from djlib.config import DjlibConfig
from djlib.db.models import FileRecord
from djlib.db.session import session_factory
from djlib.scan.service import ScanService


def _write_valid_wav(path: Path, num_frames: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b'\x00\x00' * num_frames)


def test_collect_catalog_export_rows_matches_effective_identity_and_technical_fields(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    _write_valid_wav(config.music_root / 'Artist One - Track One.wav')
    _write_valid_wav(config.music_root / 'Artist Two - Track Two.wav')

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        rows = collect_catalog_export_rows(session)
        files = {f.relative_path: f for f in session.execute(select(FileRecord)).scalars()}

        assert len(rows) == 2
        rows_by_path = {row.relative_path: row for row in rows}
        assert set(rows_by_path) == set(files)

        for relative_path, row in rows_by_path.items():
            file = files[relative_path]
            track = active_track_for_file(session, file.id)
            assert track is not None
            identity = CatalogService(session).effective_identity(track)
            assert row.artist == identity.artist
            assert row.title == identity.title
            assert row.version == identity.version
            assert row.edition == identity.edition
            assert row.file_public_id == file.public_id
            assert row.track_public_id == track.public_id
            assert row.size_bytes == file.size_bytes
            assert row.duration_ms == file.duration_ms
            assert row.is_present == file.is_present
            assert row.quality_score is None


def test_collecting_rows_streams_files_instead_of_loading_the_whole_catalogue_up_front(
    config: DjlibConfig, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exporting the whole catalogue must not load every `FileRecord` (each
    carrying a `raw_metadata_json` blob) into memory before producing the
    first output row -- that holds the entire library resident for the
    whole export, when only one file at a time is ever needed."""
    config.music_root.mkdir(parents=True)
    for index in range(5):
        _write_valid_wav(config.music_root / f'Artist - Track {index}.wav')

    monkeypatch.setattr(catalog_export, '_EXPORT_YIELD_PER', 1)

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    loaded_count = 0

    def _count_loads(target: FileRecord, context: object) -> None:
        nonlocal loaded_count
        loaded_count += 1

    event.listen(FileRecord, 'load', _count_loads)
    try:
        loaded_before_first_row: list[int] = []
        original = catalog_export._latest_quality_score

        def _spy(session: Session, file_id: int) -> float | None:
            loaded_before_first_row.append(loaded_count)
            return original(session, file_id)

        monkeypatch.setattr(catalog_export, '_latest_quality_score', _spy)

        with session_maker() as session:
            rows = collect_catalog_export_rows(session)
    finally:
        event.remove(FileRecord, 'load', _count_loads)

    assert len(rows) == 5
    # Only the first file should have been loaded by the time the first
    # row's data was gathered -- not all five.
    assert loaded_before_first_row[0] == 1


def test_rows_are_ordered_by_relative_path(config: DjlibConfig, engine: Engine) -> None:
    config.music_root.mkdir(parents=True)
    _write_valid_wav(config.music_root / 'Zebra - Song.wav')
    _write_valid_wav(config.music_root / 'Alpha - Song.wav')

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        rows = collect_catalog_export_rows(session)

    assert [row.relative_path for row in rows] == sorted(row.relative_path for row in rows)
