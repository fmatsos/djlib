import subprocess
from pathlib import Path

from sqlalchemy import Engine

from djlib.catalog.stats_export import collect_stats_export_rows
from djlib.config import DjlibConfig
from djlib.db.session import session_factory
from djlib.duplicates.service import DuplicateService
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


def test_collect_stats_export_rows_includes_catalog_and_duplicate_side(
    config: DjlibConfig, engine: Engine
) -> None:
    config.music_root.mkdir(parents=True)
    original = config.music_root / 'crate_a' / 'Artist - Track.wav'
    duplicate = config.music_root / 'crate_b' / 'Artist - Track.wav'
    _make_noise_wav(original, seed=1)
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(original.read_bytes())

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()
    with session_maker() as session:
        DuplicateService(config, session).run()

    with session_maker() as session:
        rows = collect_stats_export_rows(session, config)

    by_key = {(row.category, row.metric): row.value for row in rows}

    assert by_key[('files', 'total')] == '2'
    assert by_key[('files', 'present')] == '2'
    assert by_key[('files', 'missing')] == '0'

    assert ('tracks', 'active') in by_key
    assert ('tracks', 'provisional') in by_key
    assert ('tracks', 'merged') in by_key
    assert int(by_key[('tracks', 'active')]) + int(by_key[('tracks', 'merged')]) >= 1

    assert ('scans', 'runs_total') in by_key
    assert int(by_key[('scans', 'runs_total')]) >= 1
    assert by_key[('scans', 'latest_scan_status')] != '-'

    assert ('duplicate_groups', 'auto_confirmed') in by_key
    assert int(by_key[('duplicate_groups', 'auto_confirmed')]) >= 1

    assert ('duplicate_pairs', 'exact') in by_key
    assert int(by_key[('duplicate_pairs', 'exact')]) >= 1
