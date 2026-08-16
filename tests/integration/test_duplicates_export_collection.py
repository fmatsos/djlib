import subprocess
from pathlib import Path

from sqlalchemy import Engine, select

from djlib.config import DjlibConfig
from djlib.db.enums import DuplicateStatus
from djlib.db.models import DuplicateGroup, FileRecord
from djlib.db.session import session_factory
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
