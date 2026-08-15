from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from djlib.catalog.queries import active_track_for_file
from djlib.config import DurationToleranceThresholds
from djlib.db.models import FileRecord, Track, TrackFeaturedArtist, TrackFile
from djlib.duplicates.similarity import (
    FUZZY_TITLE_THRESHOLD,
    duration_tolerance_ms,
    metadata_similarity,
)
from djlib.duplicates.types import (
    BlockingTier,
    CandidatePair,
    TrackIdentitySnapshot,
    VersionCompatibilityState,
)

# "Very close" duration tolerance for the missing-artist (weak) tier: tighter
# than the normal duration bucket since artist is not corroborating the match,
# floored so it never collapses to an unusably narrow window for short tracks.
_WEAK_TIER_MIN_TOLERANCE_MS = 1000

_TIER_RANK = {BlockingTier.WEAK: 0, BlockingTier.CANDIDATE: 1, BlockingTier.STRONG: 2}


def _weak_tolerance_ms(tolerance_ms: int) -> int:
    return max(_WEAK_TIER_MIN_TOLERANCE_MS, tolerance_ms // 2)


@dataclass(frozen=True)
class _Match:
    file: FileRecord
    track: Track
    tier: BlockingTier


def _featured_artist_names(session: Session, track_id: int) -> tuple[str, ...]:
    rows = session.execute(
        select(TrackFeaturedArtist.normalized_name)
        .where(TrackFeaturedArtist.track_id == track_id)
        .order_by(TrackFeaturedArtist.position)
    ).scalars()
    return tuple(rows)


def _snapshot(session: Session, track: Track, file: FileRecord) -> TrackIdentitySnapshot:
    return TrackIdentitySnapshot(
        artist_normalized=track.artist_normalized,
        title_normalized=track.title_normalized,
        version_normalized=track.version_normalized,
        edition_normalized=track.edition_normalized,
        duration_ms=file.duration_ms,
        featured_artist_normalized_names=_featured_artist_names(session, track.id),
    )


class CandidateBlocker:
    """Conservative duplicate-candidate blocking at FILE granularity (design §14).

    Blocking is never all-vs-all: each tier issues one indexed-column SQL
    query filtered on the source file's already-resolved, already-normalized
    identity (Task 6 output) plus its own duration bucket. Only the small
    result set a query returns is ever compared in Python (RapidFuzz title
    fuzzy match) -- there is no O(n^2) double loop over the file table.

    A pair whose `version_compatibility` turns out INCOMPATIBLE is still
    returned (with `auto_merge_eligible=False`) rather than dropped, so a
    genuine metadata conflict remains visible to the rest of the pipeline for
    human review (Task 10) instead of disappearing silently.
    """

    def __init__(
        self, session: Session, duration_thresholds: DurationToleranceThresholds | None = None
    ) -> None:
        self._session = session
        self._duration_thresholds = duration_thresholds

    def find_candidates(self, file_id: int) -> list[CandidatePair]:
        source_file = self._session.get(FileRecord, file_id)
        if source_file is None or source_file.duration_ms is None:
            return []
        source_track = active_track_for_file(self._session, file_id)
        if source_track is None:
            return []

        tolerance_ms = duration_tolerance_ms(source_file.duration_ms, self._duration_thresholds)
        low = source_file.duration_ms - tolerance_ms
        high = source_file.duration_ms + tolerance_ms

        matches: dict[int, _Match] = {}

        if source_track.artist_normalized and source_track.title_normalized:
            self._collect_exact_artist_tier(source_track, low, high, file_id, matches)

        if source_track.title_normalized:
            self._collect_missing_artist_tier(
                source_track, source_file.duration_ms, tolerance_ms, file_id, matches
            )

        return [
            self._build_candidate_pair(file_id, source_track, source_file, match)
            for match in matches.values()
        ]

    def _build_candidate_pair(
        self, file_id: int, source_track: Track, source_file: FileRecord, match: '_Match'
    ) -> CandidatePair:
        left_snapshot = _snapshot(self._session, source_track, source_file)
        right_snapshot = _snapshot(self._session, match.track, match.file)
        evidence = metadata_similarity(left_snapshot, right_snapshot)
        auto_merge_eligible = (
            evidence.version_compatibility.state != VersionCompatibilityState.INCOMPATIBLE
        )
        return CandidatePair(
            left_file_id=file_id,
            right_file_id=match.file.id,
            tier=match.tier,
            evidence=evidence,
            auto_merge_eligible=auto_merge_eligible,
        )

    def _candidate_query(self, low: int, high: int, file_id: int):
        return (
            select(FileRecord, Track)
            .join(TrackFile, TrackFile.file_id == FileRecord.id)
            .join(Track, Track.id == TrackFile.track_id)
            .where(
                TrackFile.is_active.is_(True),
                FileRecord.is_present.is_(True),
                FileRecord.id != file_id,
                FileRecord.duration_ms.is_not(None),
                FileRecord.duration_ms.between(low, high),
            )
        )

    def _collect_exact_artist_tier(
        self, source_track: Track, low: int, high: int, file_id: int, matches: dict[int, '_Match']
    ) -> None:
        query = self._candidate_query(low, high, file_id).where(
            Track.artist_normalized == source_track.artist_normalized
        )
        for candidate_file, candidate_track in self._session.execute(query).all():
            if candidate_track.title_normalized == source_track.title_normalized:
                tier = BlockingTier.STRONG
            elif candidate_track.title_normalized and (
                fuzz.ratio(source_track.title_normalized, candidate_track.title_normalized)
                >= FUZZY_TITLE_THRESHOLD
            ):
                tier = BlockingTier.CANDIDATE
            else:
                continue
            self._record(matches, candidate_file, candidate_track, tier)

    def _collect_missing_artist_tier(
        self,
        source_track: Track,
        duration_ms: int,
        tolerance_ms: int,
        file_id: int,
        matches: dict[int, '_Match'],
    ) -> None:
        weak_tolerance = _weak_tolerance_ms(tolerance_ms)
        low = duration_ms - weak_tolerance
        high = duration_ms + weak_tolerance

        query = self._candidate_query(low, high, file_id).where(
            Track.title_normalized == source_track.title_normalized
        )
        if source_track.artist_normalized:
            # Source side has an artist: this tier only fires for candidates
            # that are themselves missing one (design's "missing artist on
            # either side"). Two present-but-different artists are never a
            # candidate via this path.
            query = query.where(Track.artist_normalized.is_(None))

        for candidate_file, candidate_track in self._session.execute(query).all():
            self._record(matches, candidate_file, candidate_track, BlockingTier.WEAK)

    def _record(
        self,
        matches: dict[int, '_Match'],
        candidate_file: FileRecord,
        candidate_track: Track,
        tier: BlockingTier,
    ) -> None:
        existing = matches.get(candidate_file.id)
        if existing is not None and _TIER_RANK[existing.tier] >= _TIER_RANK[tier]:
            return
        matches[candidate_file.id] = _Match(file=candidate_file, track=candidate_track, tier=tier)
