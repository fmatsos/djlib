from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from djlib.db.enums import DecisionSource, RelationshipType, TrackStatus
from djlib.db.models import FileFeaturedArtist, FileRecord, Track, TrackFeaturedArtist, TrackFile
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
