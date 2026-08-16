import subprocess
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, select

from djlib.config import DjlibConfig
from djlib.db.enums import DuplicateStatus
from djlib.db.models import DuplicateGroup, FileRecord
from djlib.db.session import session_factory
from djlib.duplicates import export as duplicates_export
from djlib.duplicates.export import collect_duplicate_export_rows
from djlib.duplicates.service import DuplicateService
from djlib.report.generator import ReportGenerator
from djlib.scan.service import ScanService


def _make_noise_wav(path: Path, seed: int, duration: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            'ffmpeg', '-y', '-v', 'error',
            '-f', 'lavfi', '-i', f'anoisesrc=duration={duration}:color=white:seed={seed}',
            '-ar', '44100', '-sample_fmt', 's16', str(path),
        ],
        check=True,
    )


def _reencode(src: Path, dst: Path, *codec_args: str) -> None:
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(src), *codec_args, str(dst)], check=True)


def _seed_review_required_group(config: DjlibConfig, engine: Engine) -> str:
    config.music_root.mkdir(parents=True)
    original_mix = config.music_root / 'Artist - Track (Original Mix).wav'
    _make_noise_wav(original_mix, seed=11, duration=3.0)
    extended_mix = config.music_root / 'Artist - Track (Extended Mix).flac'
    _reencode(original_mix, extended_mix, '-codec:a', 'flac')

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    with session_maker() as session:
        service = DuplicateService(config, session)
        service.detect()
        service.analyze()

    with session_maker() as session:
        group = session.execute(select(DuplicateGroup)).scalar_one()
        assert group.status == DuplicateStatus.REVIEW_REQUIRED
        return group.public_id


def test_collect_duplicate_export_rows_matches_report_reasons_and_preferred_file(
    config: DjlibConfig, engine: Engine
) -> None:
    _seed_review_required_group(config, engine)
    session_maker = session_factory(engine)

    with session_maker() as session:
        artifact = ReportGenerator(config, session).generate()
    manifest_group = __import__('json').loads(
        artifact.manifest_path.read_text(encoding='utf-8')
    )['groups'][0]

    with session_maker() as session:
        rows = collect_duplicate_export_rows(session)
        files = {f.id: f for f in session.execute(select(FileRecord)).scalars()}

    assert len(rows) == 1
    row = rows[0]
    assert row.group_public_id == manifest_group['group_id']
    assert row.status == manifest_group['status']
    assert row.file_count == manifest_group['file_count']
    assert row.reasons == '; '.join(manifest_group['reasons'])
    assert row.proposed_preferred_path == next(
        f.relative_path for f in files.values() if f.public_id == manifest_group['proposed_preferred_file_id']
    )
    expected_paths = ' | '.join(sorted(f.relative_path for f in files.values()))
    assert row.member_paths == expected_paths


def test_collecting_rows_streams_groups_instead_of_loading_them_all_up_front(
    config: DjlibConfig, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same memory concern as the catalog export: exporting every duplicate
    group must not load them all into memory before producing the first
    output row."""
    config.music_root.mkdir(parents=True)
    for name, seed in (('Pair One', 21), ('Pair Two', 22)):
        original = config.music_root / name / 'crate_a' / f'Test Artist - {name}.wav'
        duplicate = config.music_root / name / 'crate_b' / f'Test Artist - {name}.wav'
        _make_noise_wav(original, seed=seed)
        duplicate.parent.mkdir(parents=True)
        duplicate.write_bytes(original.read_bytes())

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()
    with session_maker() as session:
        service = DuplicateService(config, session)
        assert service.detect() == 2
        service.analyze()

    monkeypatch.setattr(duplicates_export, '_EXPORT_YIELD_PER', 1)

    loaded_count = 0

    def _count_loads(target: DuplicateGroup, context: object) -> None:
        nonlocal loaded_count
        loaded_count += 1

    event.listen(DuplicateGroup, 'load', _count_loads)
    try:
        loaded_before_first_row: list[int] = []
        original = duplicates_export.group_reasons

        def _spy(pair_rows: object) -> list[str]:
            loaded_before_first_row.append(loaded_count)
            return original(pair_rows)

        monkeypatch.setattr(duplicates_export, 'group_reasons', _spy)

        with session_maker() as session:
            rows = collect_duplicate_export_rows(session)
    finally:
        event.remove(DuplicateGroup, 'load', _count_loads)

    assert len(rows) == 2
    # Only the first group should have been loaded by the time the first
    # row's data was gathered -- not both.
    assert loaded_before_first_row[0] == 1
