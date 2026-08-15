import datetime as dt
import itertools
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from djlib.catalog.queries import active_track_for_file
from djlib.catalog.service import CatalogService
from djlib.config import DjlibConfig
from djlib.db.enums import DecisionSource, DuplicateStatus, PairClassification, RelationshipType
from djlib.db.models import (
    DuplicateGroup,
    DuplicateGroupMember,
    DuplicatePairEvidence,
    FileRecord,
    Track,
    TrackFeaturedArtist,
)
from djlib.duplicates.blocking import CandidateBlocker
from djlib.duplicates.chromaprint import ChromaprintService, fingerprint_similarity
from djlib.duplicates.classifier import PairClassifier, PairDecision
from djlib.duplicates.groups import ClassifiedPair, DuplicateGroupBuilder, connected_components
from djlib.duplicates.hashing import HashService
from djlib.duplicates.preferred import PreferredCandidate, PreferredChoice, PreferredFileSelector
from djlib.duplicates.quality import QualityAnalysisError, QualityAnalyzer
from djlib.duplicates.similarity import metadata_similarity
from djlib.duplicates.types import TrackIdentitySnapshot
from djlib.ids import new_public_id
from djlib.metadata.types import CommandRunner, SubprocessCommandRunner

MATCHER_VERSION = '1'

# Group statuses an automatic re-analysis is still allowed to touch. CONFIRMED
# / REJECTED / DEFERRED are human decisions (Task 13) and must never be
# silently revisited or reversed by `duplicates analyze`/`duplicates run`
# (see .claude/rules/curation-persistence.md).
_ANALYZABLE_STATUSES = (
    DuplicateStatus.DETECTED,
    DuplicateStatus.AUTO_CONFIRMED,
    DuplicateStatus.REVIEW_REQUIRED,
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class DuplicateRunSummary:
    groups_detected: int
    groups_analyzed: int
    groups_consolidated: int


@dataclass(frozen=True)
class DuplicateStats:
    group_status_counts: dict[str, int]
    pair_classification_counts: dict[str, int]


class DuplicateService:
    """Orchestrates duplicate detection, classification and safe automatic
    consolidation (design §14-21, Task 10 -- the milestone's major review gate).

    `detect()` never computes expensive evidence -- only conservative
    metadata blocking (Task 7). `analyze()` computes BLAKE3/Chromaprint/
    quality evidence *only* for already-detected candidate groups (never
    library-wide, per `.claude/rules/duplicate-detection.md`) and proposes a
    group status + preferred file. `run()` additionally performs automatic
    consolidation, but *only* for groups that reached `AUTO_CONFIRMED` --
    every other status (`REVIEW_REQUIRED` above all) is left completely
    untouched for a human (Task 12/13's job, not this one).
    """

    def __init__(
        self,
        config: DjlibConfig,
        session: Session,
        runner: CommandRunner | None = None,
    ) -> None:
        self._config = config
        self._session = session
        runner = runner or SubprocessCommandRunner()
        self._hash_service = HashService(config.music_root)
        self._chromaprint_service = ChromaprintService(config.music_root, runner)
        self._quality_analyzer = QualityAnalyzer(runner)
        self._classifier = PairClassifier(config.duplicates.chromaprint)
        self._group_builder = DuplicateGroupBuilder()
        self._preferred_selector = PreferredFileSelector()
        self._catalog_service = CatalogService(session)

    # -- detect --------------------------------------------------------

    def detect(self) -> int:
        """Blocking-only candidate detection (design §14). No BLAKE3, no
        Chromaprint, no quality analysis -- `duplicate_groups` rows are
        persisted at `DETECTED` status with no evidence computed yet.
        """
        present_file_ids = list(
            self._session.execute(
                select(FileRecord.id).where(FileRecord.is_present.is_(True))
            ).scalars()
        )
        raw_pairs = self._collect_candidate_pairs(present_file_ids)
        components = connected_components(list(raw_pairs))

        groups: list[DuplicateGroup] = []
        for component in components:
            group = self._find_existing_group(component)
            if group is None:
                group = self._create_group(component)
            groups.append(group)

        self._session.commit()
        return len(groups)

    def _collect_candidate_pairs(self, file_ids: list[int]) -> set[tuple[int, int]]:
        blocker = CandidateBlocker(self._session, self._config.duplicates.duration)
        pairs: set[tuple[int, int]] = set()
        for file_id in file_ids:
            for candidate in blocker.find_candidates(file_id):
                pairs.add(
                    (
                        min(candidate.left_file_id, candidate.right_file_id),
                        max(candidate.left_file_id, candidate.right_file_id),
                    )
                )
        return pairs

    def _find_existing_group(self, file_ids: list[int]) -> DuplicateGroup | None:
        wanted = frozenset(file_ids)
        candidate_group_ids = {
            row
            for row in self._session.execute(
                select(DuplicateGroupMember.group_id).where(
                    DuplicateGroupMember.file_id.in_(file_ids)
                )
            ).scalars()
        }
        for group_id in candidate_group_ids:
            members = frozenset(
                self._session.execute(
                    select(DuplicateGroupMember.file_id).where(
                        DuplicateGroupMember.group_id == group_id
                    )
                ).scalars()
            )
            if members == wanted:
                return self._session.get(DuplicateGroup, group_id)
        return None

    def _create_group(self, file_ids: list[int]) -> DuplicateGroup:
        group = DuplicateGroup(
            public_id=new_public_id('dup'),
            status=DuplicateStatus.DETECTED,
            matcher_version=MATCHER_VERSION,
        )
        self._session.add(group)
        self._session.flush()
        for file_id in file_ids:
            self._session.add(DuplicateGroupMember(group_id=group.id, file_id=file_id))
        self._session.flush()
        return group

    # -- analyze ---------------------------------------------------------

    def analyze(self) -> int:
        """Targeted evidence + classification for already-detected groups
        (design §17-21). Never touches `CONFIRMED`/`REJECTED`/`DEFERRED`
        groups -- those are human decisions.
        """
        groups = list(
            self._session.execute(
                select(DuplicateGroup).where(DuplicateGroup.status.in_(_ANALYZABLE_STATUSES))
            ).scalars()
        )
        for group in groups:
            self._analyze_group(group)
        self._session.commit()
        return len(groups)

    def _member_file_ids(self, group_id: int) -> list[int]:
        return list(
            self._session.execute(
                select(DuplicateGroupMember.file_id).where(
                    DuplicateGroupMember.group_id == group_id
                )
            ).scalars()
        )

    def _analyze_group(self, group: DuplicateGroup) -> None:
        file_ids = self._member_file_ids(group.id)
        files = {
            f.id: f
            for f in self._session.execute(
                select(FileRecord).where(FileRecord.id.in_(file_ids))
            ).scalars()
        }
        if len(files) < 2:
            return

        self._session.execute(
            delete(DuplicatePairEvidence).where(DuplicatePairEvidence.group_id == group.id)
        )

        classified_pairs: list[ClassifiedPair] = [
            ClassifiedPair(left.id, right.id, self._classify_pair(group.id, left, right))
            for left, right in (
                (files[left_id], files[right_id])
                for left_id, right_id in itertools.combinations(sorted(files), 2)
            )
        ]

        drafts = self._group_builder.build(classified_pairs)
        draft = drafts[0]

        group.status = draft.status
        group.confidence = draft.confidence
        if draft.status == DuplicateStatus.AUTO_CONFIRMED:
            group.resolved_at = _now()

        choice = self._propose_preferred_file(files.values())
        group.proposed_preferred_file_id = choice.file_id if choice is not None else None

    def _classify_pair(
        self, group_id: int, left: FileRecord, right: FileRecord
    ) -> PairDecision:
        evidence = metadata_similarity(self._snapshot(left), self._snapshot(right))

        left_hash = self._hash_service.ensure_current(left)
        right_hash = self._hash_service.ensure_current(right)
        binary_hash_equal = left_hash == right_hash

        chromaprint_similarity: float | None = None
        if not binary_hash_equal:
            # design §18: only fingerprint once the binary hash has ruled
            # out an exact copy -- an identical hash already means EXACT.
            left_fp = self._chromaprint_service.ensure_current(left)
            right_fp = self._chromaprint_service.ensure_current(right)
            chromaprint_similarity = fingerprint_similarity(left_fp, right_fp)

        decision = self._classifier.classify(evidence, binary_hash_equal, chromaprint_similarity)

        self._session.add(
            DuplicatePairEvidence(
                group_id=group_id,
                left_file_id=left.id,
                right_file_id=right.id,
                metadata_similarity=evidence.metadata_similarity,
                artist_similarity=evidence.artist_similarity,
                title_similarity=evidence.title_similarity,
                version_compatibility=evidence.version_compatibility.state.value,
                edition_compatibility=evidence.edition_compatibility.state.value,
                featured_artist_similarity=evidence.featured_artist_similarity,
                duration_delta_ms=evidence.duration_delta_ms,
                binary_hash_equal=binary_hash_equal,
                chromaprint_similarity=chromaprint_similarity,
                classification=decision.classification,
                confidence=decision.confidence,
                evidence_json={'reasons': list(decision.reasons)},
            )
        )
        return decision

    def _snapshot(self, file: FileRecord) -> TrackIdentitySnapshot:
        track = active_track_for_file(self._session, file.id)
        if track is None:
            raise RuntimeError(
                f'file {file.public_id} has no active track; cannot analyze duplicates for it'
            )
        featured = tuple(
            self._session.execute(
                select(TrackFeaturedArtist.normalized_name)
                .where(TrackFeaturedArtist.track_id == track.id)
                .order_by(TrackFeaturedArtist.position)
            ).scalars()
        )
        return TrackIdentitySnapshot(
            artist_normalized=track.artist_normalized,
            title_normalized=track.title_normalized,
            version_normalized=track.version_normalized,
            edition_normalized=track.edition_normalized,
            duration_ms=file.duration_ms,
            featured_artist_normalized_names=featured,
        )

    def _propose_preferred_file(self, files: Iterable[FileRecord]) -> PreferredChoice | None:
        candidates: list[PreferredCandidate] = []
        for file in files:
            path = self._config.music_root / file.relative_path
            try:
                quality = self._quality_analyzer.analyze(path, file)
            except QualityAnalysisError:
                continue
            candidates.append(PreferredCandidate(file_id=file.id, quality=quality))
        if not candidates:
            return None
        return self._preferred_selector.choose(candidates)

    # -- run: detect + analyze + safe automatic consolidation -------------

    def run(self) -> DuplicateRunSummary:
        groups_detected = self.detect()
        groups_analyzed = self.analyze()
        groups_consolidated = self._consolidate_auto_confirmed_groups()
        return DuplicateRunSummary(
            groups_detected=groups_detected,
            groups_analyzed=groups_analyzed,
            groups_consolidated=groups_consolidated,
        )

    def _consolidate_auto_confirmed_groups(self) -> int:
        groups = list(
            self._session.execute(
                select(DuplicateGroup).where(
                    DuplicateGroup.status == DuplicateStatus.AUTO_CONFIRMED
                )
            ).scalars()
        )
        consolidated = 0
        for group in groups:
            if self._consolidate_group(group):
                consolidated += 1
        self._session.commit()
        return consolidated

    def _consolidate_group(self, group: DuplicateGroup) -> bool:
        if group.proposed_preferred_file_id is None:
            return False

        file_ids = self._member_file_ids(group.id)
        tracks_by_file: dict[int, Track | None] = {
            file_id: active_track_for_file(self._session, file_id) for file_id in file_ids
        }
        if any(track is None for track in tracks_by_file.values()):
            # A member file with no active track can't be merged safely --
            # defensive, should never happen given catalogue invariants.
            return False

        distinct_track_ids = {track.id for track in tracks_by_file.values()}  # type: ignore[union-attr]
        if len(distinct_track_ids) <= 1:
            return False  # already consolidated into one track -- idempotent no-op

        if group.proposed_preferred_file_id not in tracks_by_file:
            return False
        survivor_track = tracks_by_file[group.proposed_preferred_file_id]
        assert survivor_track is not None

        absorbed: dict[int, dict[int, RelationshipType]] = {}
        for file_id, track in tracks_by_file.items():
            assert track is not None
            if track.id == survivor_track.id:
                continue
            relationship = self._relationship_for(
                group.id, file_id, group.proposed_preferred_file_id
            )
            absorbed.setdefault(track.id, {})[file_id] = relationship

        absorbed_tracks = {
            track.id: track for track in tracks_by_file.values() if track.id != survivor_track.id  # type: ignore[union-attr]
        }
        for track_id, relationships in absorbed.items():
            self._catalog_service.merge_track_into(
                survivor=survivor_track,
                absorbed=absorbed_tracks[track_id],
                relationships=relationships,
                decision_source=DecisionSource.AUTOMATIC,
            )

        self._catalog_service.activate_track(
            survivor_track, preferred_file_id=group.proposed_preferred_file_id
        )
        group.resolved_at = _now()
        return True

    def _relationship_for(
        self, group_id: int, file_id: int, preferred_file_id: int
    ) -> RelationshipType:
        if file_id == preferred_file_id:
            return RelationshipType.PRIMARY
        row = self._session.execute(
            select(DuplicatePairEvidence).where(
                DuplicatePairEvidence.group_id == group_id,
                or_(
                    and_(
                        DuplicatePairEvidence.left_file_id == file_id,
                        DuplicatePairEvidence.right_file_id == preferred_file_id,
                    ),
                    and_(
                        DuplicatePairEvidence.left_file_id == preferred_file_id,
                        DuplicatePairEvidence.right_file_id == file_id,
                    ),
                ),
            )
        ).scalars().first()
        if row is not None and row.classification == PairClassification.EXACT:
            return RelationshipType.EXACT_DUPLICATE
        return RelationshipType.AUDIO_EQUIVALENT

    # -- stats -------------------------------------------------------------

    def stats(self) -> DuplicateStats:
        group_counts = {status.value: 0 for status in DuplicateStatus}
        for status, count in self._session.execute(
            select(DuplicateGroup.status, func.count()).group_by(DuplicateGroup.status)
        ).all():
            group_counts[status.value] = count

        pair_counts = {classification.value: 0 for classification in PairClassification}
        for classification, count in self._session.execute(
            select(DuplicatePairEvidence.classification, func.count()).group_by(
                DuplicatePairEvidence.classification
            )
        ).all():
            pair_counts[classification.value] = count

        return DuplicateStats(
            group_status_counts=group_counts, pair_classification_counts=pair_counts
        )
