import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from djlib.db.enums import DecisionSource, IdentityEventType, RelationshipType, TrackStatus
from djlib.db.models import (
    FileFeaturedArtist,
    FileRecord,
    Track,
    TrackFeaturedArtist,
    TrackFile,
    TrackIdentityEvent,
)
from djlib.ids import new_public_id
from djlib.resolve.normalizer import normalize_identity


@dataclass(frozen=True)
class EffectiveFeaturedArtist:
    position: int
    name: str
    source: str


@dataclass(frozen=True)
class EffectiveIdentity:
    artist: str | None
    title: str | None
    version: str | None
    edition: str | None
    featured_artists: tuple[EffectiveFeaturedArtist, ...]


def _normalized_or_none(value: str | None) -> str | None:
    return normalize_identity(value) if value is not None else None


class CatalogService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_provisional_track(self, file: FileRecord) -> Track:
        track = Track(public_id=new_public_id('trk'), status=TrackStatus.PROVISIONAL)
        self._session.add(track)
        self._session.flush()

        self._copy_identity_from_file(track, file)

        self._session.add(
            TrackFile(
                track_id=track.id,
                file_id=file.id,
                relationship=RelationshipType.PRIMARY,
                decision_source=DecisionSource.AUTOMATIC,
                is_active=True,
            )
        )
        self._session.flush()
        return track

    def refresh_track_identity(self, track: Track, file: FileRecord) -> None:
        self._copy_identity_from_file(track, file)
        self._session.flush()

    def effective_identity(self, track: Track) -> EffectiveIdentity:
        featured_artists = self._session.execute(
            select(TrackFeaturedArtist)
            .where(TrackFeaturedArtist.track_id == track.id)
            .order_by(TrackFeaturedArtist.position)
        ).scalars()
        return EffectiveIdentity(
            artist=track.artist,
            title=track.title,
            version=track.version,
            edition=track.edition,
            featured_artists=tuple(
                EffectiveFeaturedArtist(position=fa.position, name=fa.name, source=fa.source)
                for fa in featured_artists
            ),
        )

    def merge_track_into(
        self,
        survivor: Track,
        absorbed: Track,
        relationships: dict[int, RelationshipType],
        decision_source: DecisionSource,
    ) -> TrackIdentityEvent:
        """Fold `absorbed` into `survivor` (design §13): the one merge primitive.

        `absorbed` becomes `MERGED` with `merged_into_track_id` pointing at
        `survivor` -- its `public_id` is never reused or deleted (see
        `.claude/rules/curation-persistence.md`). Every currently-active
        `TrackFile` row owned by `absorbed` is repointed at `survivor` in
        place (same row, `track_id` updated) rather than replaced, so the
        "one active track per file" invariant
        (`uq_track_files_one_active_per_file`) is preserved automatically:
        `relationships` supplies the per-file `RelationshipType` to record
        (e.g. `EXACT_DUPLICATE`/`AUDIO_EQUIVALENT`, never `PRIMARY` for a
        migrated file -- `PRIMARY` stays reserved for `survivor`'s own
        original file(s)). A `TrackIdentityEvent` (`MERGE`) is always
        recorded for auditability, referencing both tracks' public IDs.

        This is deliberately the *only* place in the codebase that mutates
        track/file identity state during a merge: Task 10's automatic safe
        consolidation (`duplicates run`) and Task 11's upcoming human-
        triggered `merge_tracks` both call this same method, so there is
        exactly one code path to reason about for merge correctness.
        """
        absorbed_files = self._session.execute(
            select(TrackFile).where(
                TrackFile.track_id == absorbed.id, TrackFile.is_active.is_(True)
            )
        ).scalars()
        for link in absorbed_files:
            link.track_id = survivor.id
            link.relationship = relationships.get(link.file_id, RelationshipType.AUDIO_EQUIVALENT)
            link.decision_source = decision_source
            link.is_active = True

        absorbed.status = TrackStatus.MERGED
        absorbed.merged_into_track_id = survivor.id

        event = TrackIdentityEvent(
            event_uuid=str(uuid.uuid4()),
            event_type=IdentityEventType.MERGE,
            source_track_public_id=absorbed.public_id,
            target_track_public_id=survivor.public_id,
            payload_json={'decision_source': decision_source.value},
        )
        self._session.add(event)
        self._session.flush()
        return event

    def activate_track(self, track: Track, preferred_file_id: int | None = None) -> None:
        track.status = TrackStatus.ACTIVE
        if preferred_file_id is not None:
            track.preferred_file_id = preferred_file_id
        self._session.flush()

    def _copy_identity_from_file(self, track: Track, file: FileRecord) -> None:
        track.artist = file.resolved_artist
        track.title = file.resolved_title
        track.version = file.resolved_version
        track.edition = file.resolved_edition
        track.artist_normalized = _normalized_or_none(file.resolved_artist)
        track.title_normalized = _normalized_or_none(file.resolved_title)
        track.version_normalized = _normalized_or_none(file.resolved_version)
        track.edition_normalized = _normalized_or_none(file.resolved_edition)
        track.duration_reference_ms = file.duration_ms

        self._session.execute(
            delete(TrackFeaturedArtist).where(TrackFeaturedArtist.track_id == track.id)
        )
        file_featured_artists = self._session.execute(
            select(FileFeaturedArtist)
            .where(FileFeaturedArtist.file_id == file.id)
            .order_by(FileFeaturedArtist.position)
        ).scalars()
        for entry in file_featured_artists:
            self._session.add(
                TrackFeaturedArtist(
                    track_id=track.id,
                    position=entry.position,
                    name=entry.name,
                    normalized_name=entry.normalized_name,
                    source=entry.source,
                )
            )
