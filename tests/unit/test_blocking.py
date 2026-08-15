from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from djlib.config import DjlibConfig
from djlib.db import models  # noqa: F401  registers ORM models on Base.metadata
from djlib.db.base import Base
from djlib.db.engine import create_engine_for_config
from djlib.db.enums import DecisionSource, RelationshipType, TrackStatus
from djlib.db.models import FileRecord, Track, TrackFeaturedArtist, TrackFile
from djlib.db.session import session_factory
from djlib.duplicates.blocking import CandidateBlocker
from djlib.duplicates.similarity import duration_tolerance_ms
from djlib.duplicates.types import BlockingTier, VersionCompatibilityState
from djlib.ids import new_public_id

# -- duration_tolerance_ms boundaries (design §14) --


def test_short_track_gets_2000ms_tolerance() -> None:
    assert duration_tolerance_ms(60_000) == 2000


def test_exactly_five_minutes_is_in_the_le_5min_bucket() -> None:
    assert duration_tolerance_ms(300_000) == 2000


def test_just_over_five_minutes_is_in_the_5_to_10min_bucket() -> None:
    assert duration_tolerance_ms(300_001) == 3000


def test_mid_length_track_gets_3000ms_tolerance() -> None:
    assert duration_tolerance_ms(450_000) == 3000


def test_exactly_ten_minutes_is_in_the_5_to_10min_bucket() -> None:
    assert duration_tolerance_ms(600_000) == 3000


def test_just_over_ten_minutes_is_in_the_gt_10min_bucket() -> None:
    assert duration_tolerance_ms(600_001) == 5000


def test_long_track_gets_5000ms_tolerance() -> None:
    assert duration_tolerance_ms(1_200_000) == 5000


# -- CandidateBlocker.find_candidates --


@pytest.fixture
def config(tmp_path: Path) -> DjlibConfig:
    return DjlibConfig(music_root=tmp_path / 'music', data_root=tmp_path / 'data')


@pytest.fixture
def engine(config: DjlibConfig) -> Iterator[Engine]:
    eng = create_engine_for_config(config)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_maker(engine: Engine) -> sessionmaker[Session]:
    return session_factory(engine)


def _add_file_with_track(
    session: Session,
    relative_path: str,
    *,
    artist: str | None,
    title: str | None,
    version: str | None = None,
    edition: str | None = None,
    duration_ms: int | None,
    featured_artists: tuple[str, ...] = (),
) -> FileRecord:
    file = FileRecord(
        public_id=new_public_id('file'),
        relative_path=relative_path,
        size_bytes=1000,
        mtime_ns=1,
        extension='.flac',
        duration_ms=duration_ms,
    )
    session.add(file)
    session.flush()

    def _norm(value: str | None) -> str | None:
        return value.strip().casefold() if value is not None else None

    track = Track(
        public_id=new_public_id('trk'),
        status=TrackStatus.PROVISIONAL,
        artist=artist,
        title=title,
        version=version,
        edition=edition,
        artist_normalized=_norm(artist),
        title_normalized=_norm(title),
        version_normalized=_norm(version),
        edition_normalized=_norm(edition),
        duration_reference_ms=duration_ms,
    )
    session.add(track)
    session.flush()

    for position, name in enumerate(featured_artists):
        session.add(
            TrackFeaturedArtist(
                track_id=track.id,
                position=position,
                name=name,
                normalized_name=_norm(name),
                source='tag',
            )
        )

    session.add(
        TrackFile(
            track_id=track.id,
            file_id=file.id,
            relationship=RelationshipType.PRIMARY,
            decision_source=DecisionSource.AUTOMATIC,
            is_active=True,
        )
    )
    session.flush()
    return file


def test_exact_artist_title_close_duration_is_a_strong_candidate(
    session_maker: sessionmaker[Session],
) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session, 'a.flac', artist='Daft Punk', title='One More Time', duration_ms=320_000
        )
        _add_file_with_track(
            session, 'b.mp3', artist='Daft Punk', title='One More Time', duration_ms=321_000
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.tier == BlockingTier.STRONG
    assert candidate.auto_merge_eligible is True
    assert candidate.evidence.artist_similarity == 1.0
    assert candidate.evidence.title_similarity == 1.0
    assert candidate.evidence.duration_delta_ms == 1000


def test_exact_artist_fuzzy_title_close_duration_is_a_candidate(
    session_maker: sessionmaker[Session],
) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session, 'a.flac', artist='The Human League', title="Don't You Want Me", duration_ms=240_000
        )
        _add_file_with_track(
            session, 'b.mp3', artist='The Human League', title='Dont You Want Me', duration_ms=241_000
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert len(candidates) == 1
    assert candidates[0].tier == BlockingTier.CANDIDATE
    assert candidates[0].auto_merge_eligible is True


def test_exact_artist_unrelated_title_close_duration_is_not_a_candidate(
    session_maker: sessionmaker[Session],
) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session, 'a.flac', artist='Artist', title='Totally Different Song', duration_ms=240_000
        )
        _add_file_with_track(
            session, 'b.mp3', artist='Artist', title='Nothing Alike Whatsoever', duration_ms=240_500
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert candidates == []


def test_missing_artist_matching_title_very_close_duration_is_a_weak_candidate(
    session_maker: sessionmaker[Session],
) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session, 'a.flac', artist=None, title='Untitled Track', duration_ms=200_000
        )
        _add_file_with_track(
            session, 'b.mp3', artist='Some Artist', title='Untitled Track', duration_ms=200_400
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert len(candidates) == 1
    assert candidates[0].tier == BlockingTier.WEAK
    assert candidates[0].auto_merge_eligible is True


def test_missing_artist_but_duration_outside_the_tight_window_is_not_a_candidate(
    session_maker: sessionmaker[Session],
) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session, 'a.flac', artist=None, title='Untitled Track', duration_ms=200_000
        )
        _add_file_with_track(
            session, 'b.mp3', artist='Some Artist', title='Untitled Track', duration_ms=201_800
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert candidates == []


def test_both_artists_present_but_different_is_never_a_weak_candidate(
    session_maker: sessionmaker[Session],
) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session, 'a.flac', artist='Artist A', title='Untitled Track', duration_ms=200_000
        )
        _add_file_with_track(
            session, 'b.mp3', artist='Artist B', title='Untitled Track', duration_ms=200_100
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert candidates == []


def test_incompatible_version_pair_is_returned_but_not_auto_merge_eligible(
    session_maker: sessionmaker[Session],
) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session,
            'a.flac',
            artist='Artist',
            title='Track',
            version='Original Mix',
            duration_ms=300_000,
        )
        _add_file_with_track(
            session,
            'b.mp3',
            artist='Artist',
            title='Track',
            version='Extended Mix',
            duration_ms=300_500,
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.tier == BlockingTier.STRONG
    assert candidate.evidence.version_compatibility.state == VersionCompatibilityState.INCOMPATIBLE
    assert candidate.auto_merge_eligible is False


def test_duration_far_outside_tolerance_excludes_an_otherwise_exact_match(
    session_maker: sessionmaker[Session],
) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session, 'a.flac', artist='Artist', title='Track', duration_ms=200_000
        )
        _add_file_with_track(
            session, 'b.mp3', artist='Artist', title='Track', duration_ms=260_000
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert candidates == []


def test_missing_duration_yields_no_candidates(session_maker: sessionmaker[Session]) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session, 'a.flac', artist='Artist', title='Track', duration_ms=None
        )
        _add_file_with_track(
            session, 'b.mp3', artist='Artist', title='Track', duration_ms=300_000
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert candidates == []


def test_file_already_present_is_not_matched_against_itself(
    session_maker: sessionmaker[Session],
) -> None:
    with session_maker() as session:
        left = _add_file_with_track(
            session, 'a.flac', artist='Artist', title='Track', duration_ms=200_000
        )
        session.commit()

        candidates = CandidateBlocker(session).find_candidates(left.id)

    assert candidates == []
