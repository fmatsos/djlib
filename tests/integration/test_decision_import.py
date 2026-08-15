import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from djlib.config import DjlibConfig
from djlib.curation.decisions import DecisionImporter, DecisionImportError
from djlib.db.enums import DuplicateStatus, TrackStatus
from djlib.db.models import (
    CurationEvent,
    DuplicateGroup,
    DuplicateGroupMember,
    FileRecord,
    Track,
    TrackFile,
)
from djlib.db.session import session_factory
from djlib.duplicates.service import DuplicateService
from djlib.report.generator import ReportArtifact, ReportGenerator
from djlib.scan.service import ScanService


def _make_noise_wav(path: Path, seed: int, duration: float = 3.0) -> None:
    """A real, deterministic-per-seed PCM source via the system ffmpeg -- no
    mocks (same pattern as tests/integration/test_duplicate_pipeline.py).
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


def _seed_review_required_groups(config: DjlibConfig, engine: Engine, count: int = 1) -> list[str]:
    """Builds `count` independent version-conflict fixtures (same scenario as
    Task 10's `test_version_conflict_pair_is_review_required_and_never_auto_merged`)
    so each becomes its own real, persisted `REVIEW_REQUIRED` group with real
    evidence -- never a hand-built mock row.
    """
    config.music_root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        original_mix = config.music_root / f'grp{i}' / f'Artist{i} - Track{i} (Original Mix).wav'
        _make_noise_wav(original_mix, seed=1000 + i, duration=3.0)
        extended_mix = config.music_root / f'grp{i}' / f'Artist{i} - Track{i} (Extended Mix).flac'
        _reencode(original_mix, extended_mix, '-codec:a', 'flac')

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()
    with session_maker() as session:
        service = DuplicateService(config, session)
        service.detect()
        service.analyze()

    with session_maker() as session:
        groups = list(
            session.execute(select(DuplicateGroup).order_by(DuplicateGroup.id)).scalars()
        )
        assert len(groups) == count
        assert all(g.status == DuplicateStatus.REVIEW_REQUIRED for g in groups)
        return [g.public_id for g in groups]


def _member_file_public_ids(engine: Engine, group_public_id: str) -> list[str]:
    session_maker = session_factory(engine)
    with session_maker() as session:
        group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == group_public_id)
        ).scalar_one()
        file_ids = list(
            session.execute(
                select(DuplicateGroupMember.file_id).where(
                    DuplicateGroupMember.group_id == group.id
                )
            ).scalars()
        )
        files = session.execute(select(FileRecord).where(FileRecord.id.in_(file_ids))).scalars()
        return [f.public_id for f in files]


def _generate_report(config: DjlibConfig, engine: Engine) -> ReportArtifact:
    session_maker = session_factory(engine)
    with session_maker() as session:
        return ReportGenerator(config, session).generate()


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _envelope(report_id: str, catalog_revision: str, *decisions: dict) -> dict:
    return {
        'schema_version': 1,
        'report_id': report_id,
        'catalog_revision': catalog_revision,
        'generated_at': _now_iso(),
        'decisions': list(decisions),
    }


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding='utf-8')


def _snapshot(engine: Engine) -> tuple:
    session_maker = session_factory(engine)
    with session_maker() as session:
        groups = sorted(
            (g.public_id, g.status.value, g.confidence, g.resolved_at is not None)
            for g in session.execute(select(DuplicateGroup)).scalars()
        )
        tracks = sorted(
            (t.public_id, t.status.value, t.merged_into_track_id, t.preferred_file_id)
            for t in session.execute(select(Track)).scalars()
        )
        track_files = sorted(
            (tf.track_id, tf.file_id, tf.is_active, tf.relationship.value)
            for tf in session.execute(select(TrackFile)).scalars()
        )
        events = sorted(
            (e.sequence, e.event_uuid) for e in session.execute(select(CurationEvent)).scalars()
        )
        return groups, tracks, track_files, events


# -- accepted-import semantics ------------------------------------------------


def test_confirm_decision_consolidates_group_and_records_curation_event(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    (group_id,) = _seed_review_required_groups(config, engine, count=1)
    artifact = _generate_report(config, engine)

    decisions_path = tmp_path / 'decisions.json'
    _write(
        decisions_path,
        _envelope(
            artifact.report_id,
            artifact.catalog_revision,
            {'group_id': group_id, 'decision': 'CONFIRM', 'reviewed_at': _now_iso()},
        ),
    )

    session_maker = session_factory(engine)
    with session_maker() as session:
        summary = DecisionImporter(config, session).import_file(decisions_path)

    assert summary.accepted == 1
    assert summary.group_ids == (group_id,)

    with session_maker() as session:
        group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == group_id)
        ).scalar_one()
        assert group.status == DuplicateStatus.CONFIRMED
        assert group.resolved_at is not None

        tracks = list(session.execute(select(Track)).scalars())
        active = [t for t in tracks if t.status == TrackStatus.ACTIVE]
        merged = [t for t in tracks if t.status == TrackStatus.MERGED]
        assert len(active) == 1
        assert len(merged) == 1
        assert active[0].preferred_file_id is not None

        # Two events land in the same transaction (Task 15 retrofit):
        # `consolidate_group`'s internal `merge_track_into` call records its
        # own TRACK_MERGE event first, then `_record_curation_event` records
        # the DUPLICATE_GROUP_CONFIRMED decision itself -- both are real,
        # independently replayable state changes.
        events = sorted(
            session.execute(select(CurationEvent)).scalars(), key=lambda e: e.sequence
        )
        assert len(events) == 2
        assert [e.event_type for e in events] == ['TRACK_MERGE', 'DUPLICATE_GROUP_CONFIRMED']
        assert events[1].payload_json['group_id'] == group_id
        assert events[1].payload_json['decision'] == 'CONFIRM'


def test_change_preferred_consolidates_using_the_human_selected_file(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    (group_id,) = _seed_review_required_groups(config, engine, count=1)
    session_maker = session_factory(engine)

    with session_maker() as session:
        group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == group_id)
        ).scalar_one()
        member_file_ids = list(
            session.execute(
                select(DuplicateGroupMember.file_id).where(
                    DuplicateGroupMember.group_id == group.id
                )
            ).scalars()
        )
        files = {
            f.id: f
            for f in session.execute(
                select(FileRecord).where(FileRecord.id.in_(member_file_ids))
            ).scalars()
        }
        proposed_id = group.proposed_preferred_file_id
        human_choice_id = next(fid for fid in member_file_ids if fid != proposed_id)
        human_choice_public_id = files[human_choice_id].public_id

    artifact = _generate_report(config, engine)
    decisions_path = tmp_path / 'decisions.json'
    _write(
        decisions_path,
        _envelope(
            artifact.report_id,
            artifact.catalog_revision,
            {
                'group_id': group_id,
                'decision': 'CHANGE_PREFERRED',
                'preferred_file_id': human_choice_public_id,
                'reviewed_at': _now_iso(),
            },
        ),
    )

    with session_maker() as session:
        DecisionImporter(config, session).import_file(decisions_path)

    with session_maker() as session:
        group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == group_id)
        ).scalar_one()
        assert group.status == DuplicateStatus.CONFIRMED
        survivor = session.execute(
            select(Track).where(Track.status == TrackStatus.ACTIVE)
        ).scalar_one()
        assert survivor.preferred_file_id == human_choice_id
        assert survivor.preferred_file_id != proposed_id


def test_reject_decision_is_durable_against_a_later_automatic_run(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    """design's own claim: REJECTED groups are already excluded from
    `DuplicateService._ANALYZABLE_STATUSES`, so this should hold "for free" --
    confirmed here rather than assumed.
    """
    (group_id,) = _seed_review_required_groups(config, engine, count=1)
    artifact = _generate_report(config, engine)

    decisions_path = tmp_path / 'decisions.json'
    _write(
        decisions_path,
        _envelope(
            artifact.report_id,
            artifact.catalog_revision,
            {'group_id': group_id, 'decision': 'REJECT', 'reviewed_at': _now_iso()},
        ),
    )

    session_maker = session_factory(engine)
    with session_maker() as session:
        DecisionImporter(config, session).import_file(decisions_path)

    with session_maker() as session:
        group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == group_id)
        ).scalar_one()
        assert group.status == DuplicateStatus.REJECTED
        tracks_before = {
            (t.public_id, t.status) for t in session.execute(select(Track)).scalars()
        }

    with session_maker() as session:
        DuplicateService(config, session).run()

    with session_maker() as session:
        group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == group_id)
        ).scalar_one()
        assert group.status == DuplicateStatus.REJECTED
        tracks_after = {
            (t.public_id, t.status) for t in session.execute(select(Track)).scalars()
        }
        assert tracks_after == tracks_before


def test_defer_decision_leaves_identities_separate(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    (group_id,) = _seed_review_required_groups(config, engine, count=1)
    artifact = _generate_report(config, engine)

    decisions_path = tmp_path / 'decisions.json'
    _write(
        decisions_path,
        _envelope(
            artifact.report_id,
            artifact.catalog_revision,
            {'group_id': group_id, 'decision': 'DEFER', 'reviewed_at': _now_iso()},
        ),
    )

    session_maker = session_factory(engine)
    with session_maker() as session:
        DecisionImporter(config, session).import_file(decisions_path)

    with session_maker() as session:
        group = session.execute(
            select(DuplicateGroup).where(DuplicateGroup.public_id == group_id)
        ).scalar_one()
        assert group.status == DuplicateStatus.DEFERRED
        tracks = list(session.execute(select(Track)).scalars())
        assert len(tracks) == 2
        assert all(t.status == TrackStatus.PROVISIONAL for t in tracks)


# -- atomic rejection ----------------------------------------------------


def test_rejects_unsupported_schema_version_with_zero_writes(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    (group_id,) = _seed_review_required_groups(config, engine, count=1)
    artifact = _generate_report(config, engine)

    envelope = _envelope(
        artifact.report_id,
        artifact.catalog_revision,
        {'group_id': group_id, 'decision': 'CONFIRM', 'reviewed_at': _now_iso()},
    )
    envelope['schema_version'] = 2
    decisions_path = tmp_path / 'decisions.json'
    _write(decisions_path, envelope)

    before = _snapshot(engine)
    session_maker = session_factory(engine)
    with session_maker() as session:
        with pytest.raises(DecisionImportError):
            DecisionImporter(config, session).import_file(decisions_path)
    after = _snapshot(engine)
    assert after == before


def test_rejects_stale_catalog_revision_with_zero_writes(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    (group_id,) = _seed_review_required_groups(config, engine, count=1)
    artifact = _generate_report(config, engine)

    decisions_path = tmp_path / 'decisions.json'
    _write(
        decisions_path,
        _envelope(
            artifact.report_id,
            'rev_0000000000000000000000',
            {'group_id': group_id, 'decision': 'CONFIRM', 'reviewed_at': _now_iso()},
        ),
    )

    before = _snapshot(engine)
    session_maker = session_factory(engine)
    with session_maker() as session:
        with pytest.raises(DecisionImportError, match='catalog_revision'):
            DecisionImporter(config, session).import_file(decisions_path)
    after = _snapshot(engine)
    assert after == before


def test_rejects_when_catalog_changed_since_report_generation_with_zero_writes(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    """Simulates the exact staleness scenario design §24 describes: the
    catalog changes (here, a rescan -- even a no-op one still inserts a new
    `ScanRun` row and therefore changes `catalog_revision`, see
    `report/generator.py::compute_catalog_revision`) between report
    generation and import.
    """
    (group_id,) = _seed_review_required_groups(config, engine, count=1)
    artifact = _generate_report(config, engine)

    session_maker = session_factory(engine)
    ScanService(config, session_maker).scan()

    decisions_path = tmp_path / 'decisions.json'
    _write(
        decisions_path,
        _envelope(
            artifact.report_id,
            artifact.catalog_revision,
            {'group_id': group_id, 'decision': 'CONFIRM', 'reviewed_at': _now_iso()},
        ),
    )

    before = _snapshot(engine)
    with session_maker() as session:
        with pytest.raises(DecisionImportError, match='catalog_revision'):
            DecisionImporter(config, session).import_file(decisions_path)
    after = _snapshot(engine)
    assert after == before


def test_rejects_unknown_group_id_with_zero_writes(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    _seed_review_required_groups(config, engine, count=1)
    artifact = _generate_report(config, engine)

    decisions_path = tmp_path / 'decisions.json'
    _write(
        decisions_path,
        _envelope(
            artifact.report_id,
            artifact.catalog_revision,
            {'group_id': 'dup_doesnotexist00000000000000000', 'decision': 'CONFIRM', 'reviewed_at': _now_iso()},
        ),
    )

    before = _snapshot(engine)
    session_maker = session_factory(engine)
    with session_maker() as session:
        with pytest.raises(DecisionImportError):
            DecisionImporter(config, session).import_file(decisions_path)
    after = _snapshot(engine)
    assert after == before


def test_rejects_decision_for_a_group_no_longer_review_required(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    """A group already resolved by an earlier import is stale for a second
    import attempt -- even with an otherwise fresh `catalog_revision`.
    """
    (group_id,) = _seed_review_required_groups(config, engine, count=1)
    session_maker = session_factory(engine)

    first_artifact = _generate_report(config, engine)
    first_path = tmp_path / 'first.json'
    _write(
        first_path,
        _envelope(
            first_artifact.report_id,
            first_artifact.catalog_revision,
            {'group_id': group_id, 'decision': 'REJECT', 'reviewed_at': _now_iso()},
        ),
    )
    with session_maker() as session:
        DecisionImporter(config, session).import_file(first_path)

    second_artifact = _generate_report(config, engine)
    second_path = tmp_path / 'second.json'
    _write(
        second_path,
        _envelope(
            second_artifact.report_id,
            second_artifact.catalog_revision,
            {'group_id': group_id, 'decision': 'CONFIRM', 'reviewed_at': _now_iso()},
        ),
    )

    before = _snapshot(engine)
    with session_maker() as session:
        with pytest.raises(DecisionImportError, match='REVIEW_REQUIRED'):
            DecisionImporter(config, session).import_file(second_path)
    after = _snapshot(engine)
    assert after == before


def test_rejects_change_preferred_with_file_not_in_group(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    (group_id,) = _seed_review_required_groups(config, engine, count=1)
    session_maker = session_factory(engine)

    with session_maker() as session:
        outsider = FileRecord(
            public_id='fil_outsider0000000000000000000000',
            relative_path='not-in-group.wav',
            size_bytes=1,
            mtime_ns=1,
            extension='.wav',
            is_present=True,
        )
        session.add(outsider)
        session.commit()

    artifact = _generate_report(config, engine)
    decisions_path = tmp_path / 'decisions.json'
    _write(
        decisions_path,
        _envelope(
            artifact.report_id,
            artifact.catalog_revision,
            {
                'group_id': group_id,
                'decision': 'CHANGE_PREFERRED',
                'preferred_file_id': 'fil_outsider0000000000000000000000',
                'reviewed_at': _now_iso(),
            },
        ),
    )

    before = _snapshot(engine)
    with session_maker() as session:
        with pytest.raises(DecisionImportError):
            DecisionImporter(config, session).import_file(decisions_path)
    after = _snapshot(engine)
    assert after == before


def test_batch_with_one_bad_decision_rejects_the_whole_batch(
    config: DjlibConfig, engine: Engine, tmp_path: Path
) -> None:
    """One bad decision among several good ones must reject the WHOLE batch --
    proved via a direct before/after DB snapshot, not merely 'no exception
    leaked'.
    """
    group_ids = _seed_review_required_groups(config, engine, count=3)
    artifact = _generate_report(config, engine)

    decisions = [
        {'group_id': group_ids[0], 'decision': 'CONFIRM', 'reviewed_at': _now_iso()},
        {'group_id': group_ids[1], 'decision': 'REJECT', 'reviewed_at': _now_iso()},
        {'group_id': group_ids[2], 'decision': 'DEFER', 'reviewed_at': _now_iso()},
        {'group_id': 'dup_totallybogus000000000000000000', 'decision': 'CONFIRM', 'reviewed_at': _now_iso()},
    ]
    decisions_path = tmp_path / 'decisions.json'
    _write(decisions_path, _envelope(artifact.report_id, artifact.catalog_revision, *decisions))

    before = _snapshot(engine)
    session_maker = session_factory(engine)
    with session_maker() as session:
        with pytest.raises(DecisionImportError):
            DecisionImporter(config, session).import_file(decisions_path)
    after = _snapshot(engine)
    assert after == before

    # Explicitly: none of the three otherwise-good groups moved, and no
    # CurationEvent was recorded for any of them.
    with session_maker() as session:
        groups = {
            g.public_id: g.status
            for g in session.execute(select(DuplicateGroup)).scalars()
        }
        for gid in group_ids:
            assert groups[gid] == DuplicateStatus.REVIEW_REQUIRED
        assert list(session.execute(select(CurationEvent)).scalars()) == []
