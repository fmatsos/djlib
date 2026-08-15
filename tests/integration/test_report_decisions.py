import json
import subprocess
import datetime as dt
from pathlib import Path

from sqlalchemy import Engine, select

from djlib.config import DjlibConfig
from djlib.db.enums import DuplicateStatus
from djlib.db.models import DuplicateGroup, DuplicatePairEvidence, FileRecord
from djlib.db.session import session_factory
from djlib.duplicates.service import DuplicateService
from djlib.report.generator import ReportGenerator
from djlib.scan.service import ScanService


def _make_noise_wav(path: Path, seed: int, duration: float = 3.0) -> None:
    """A real, deterministic-per-seed PCM source via the system ffmpeg -- no mocks
    (same pattern as tests/integration/test_duplicate_pipeline.py, Task 10).
    """
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
    """Drives the real scan + duplicate-detection pipeline against a version-
    conflict fixture (same scenario as Task 10's
    `test_version_conflict_pair_is_review_required_and_never_auto_merged`) so
    the report generator is exercised against a real `REVIEW_REQUIRED` group
    with real persisted evidence, not a hand-built mock row.
    """
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


def test_generate_writes_index_and_manifest_files(config: DjlibConfig, engine: Engine) -> None:
    _seed_review_required_group(config, engine)
    session_maker = session_factory(engine)

    with session_maker() as session:
        artifact = ReportGenerator(config, session).generate()

    assert artifact.output_dir.is_dir()
    assert artifact.index_path == artifact.output_dir / 'index.html'
    assert artifact.manifest_path == artifact.output_dir / 'manifest.json'
    assert artifact.index_path.is_file()
    assert artifact.manifest_path.is_file()
    assert artifact.report_id.startswith('rpt_')
    assert artifact.catalog_revision


def test_generate_never_mutates_duplicate_group_or_file_rows(
    config: DjlibConfig, engine: Engine
) -> None:
    _seed_review_required_group(config, engine)
    session_maker = session_factory(engine)

    with session_maker() as before_session:
        groups_before = [
            (g.public_id, g.status, g.confidence, g.proposed_preferred_file_id, g.resolved_at)
            for g in before_session.execute(select(DuplicateGroup)).scalars()
        ]
        files_before = [
            (f.public_id, f.size_bytes, f.mtime_ns)
            for f in before_session.execute(select(FileRecord)).scalars()
        ]

    with session_maker() as session:
        ReportGenerator(config, session).generate()

    with session_maker() as after_session:
        groups_after = [
            (g.public_id, g.status, g.confidence, g.proposed_preferred_file_id, g.resolved_at)
            for g in after_session.execute(select(DuplicateGroup)).scalars()
        ]
        files_after = [
            (f.public_id, f.size_bytes, f.mtime_ns)
            for f in after_session.execute(select(FileRecord)).scalars()
        ]

    assert groups_after == groups_before
    assert files_after == files_before


def test_manifest_contains_required_review_fields(config: DjlibConfig, engine: Engine) -> None:
    _seed_review_required_group(config, engine)
    session_maker = session_factory(engine)

    with session_maker() as session:
        group = session.execute(select(DuplicateGroup)).scalar_one()
        pair = session.execute(select(DuplicatePairEvidence)).scalar_one()
        files = {f.id: f for f in session.execute(select(FileRecord)).scalars()}
        artifact = ReportGenerator(config, session).generate()

    manifest = json.loads(artifact.manifest_path.read_text(encoding='utf-8'))

    # -- report identity / staleness-detection prerequisites (Task 13 needs
    # these; this task only has to make sure they are present) ------------
    assert manifest['report_id'] == artifact.report_id
    assert manifest['report_id'].startswith('rpt_')
    assert manifest['catalog_revision'] == artifact.catalog_revision
    assert manifest['generated_at']
    dt.datetime.fromisoformat(manifest['generated_at'])

    assert len(manifest['groups']) == 1
    group_entry = manifest['groups'][0]
    assert group_entry['group_id'] == group.public_id
    assert group_entry['status'] == DuplicateStatus.REVIEW_REQUIRED.value
    assert group_entry['confidence'] == group.confidence
    assert group_entry['reasons']
    assert isinstance(group_entry['reasons'], list) and len(group_entry['reasons']) > 0

    # proposed preferred file + reasons (design §22)
    assert group_entry['proposed_preferred_file_id'] in {f.public_id for f in files.values()}
    assert group_entry['proposed_preferred_reasons']

    # per-file technical/quality fields + source signature for staleness
    # detection (Task 13) -- size_bytes/mtime_ns must be present per file.
    assert len(group_entry['files']) == 2
    for file_entry in group_entry['files']:
        source_file = next(f for f in files.values() if f.public_id == file_entry['file_id'])
        assert file_entry['relative_path'] == source_file.relative_path
        assert file_entry['size_bytes'] == source_file.size_bytes
        assert file_entry['mtime_ns'] == source_file.mtime_ns
        assert 'codec' in file_entry
        assert 'bitrate' in file_entry
        assert 'sample_rate' in file_entry
        assert 'bit_depth' in file_entry
        assert 'duration_ms' in file_entry
        assert 'metadata_completeness' in file_entry
        assert 'quality' in file_entry
        if file_entry['quality'] is not None:
            assert 'transcode_suspicion' in file_entry['quality']
            assert 'quality_score' in file_entry['quality']

    # pairwise evidence (design §22: "pairwise evidence")
    assert len(group_entry['pairs']) == 1
    pair_entry = group_entry['pairs'][0]
    assert {pair_entry['left_file_id'], pair_entry['right_file_id']} == {
        files[pair.left_file_id].public_id,
        files[pair.right_file_id].public_id,
    }
    assert pair_entry['classification'] == pair.classification.value
    assert pair_entry['reasons']


def test_regenerating_without_any_change_is_byte_identical_manifest(
    config: DjlibConfig, engine: Engine
) -> None:
    """Catalog revision (and every other manifest field) must be deterministic
    given unchanged inputs -- no timestamps/random IDs leaking into the parts
    of the manifest that staleness detection (Task 13) will compare.
    """
    _seed_review_required_group(config, engine)
    session_maker = session_factory(engine)

    with session_maker() as session:
        revision_1 = ReportGenerator(config, session).generate().catalog_revision
    with session_maker() as session:
        revision_2 = ReportGenerator(config, session).generate().catalog_revision

    assert revision_1 == revision_2


def test_cli_duplicates_report_command_writes_report(config: DjlibConfig, engine: Engine) -> None:
    _seed_review_required_group(config, engine)
    session_maker = session_factory(engine)

    with session_maker() as session:
        artifact = ReportGenerator(config, session).generate()

    assert artifact.output_dir.parent == config.data_root / 'reports'
    assert artifact.output_dir.name.startswith('duplicates-review-')
