import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from djlib.db.enums import (
    DecisionSource,
    DuplicateStatus,
    IdentityEventType,
    RelationshipType,
    TrackStatus,
)
from djlib.db.models import (
    CurationEvent,
    DuplicateGroup,
    DuplicateGroupMember,
    FileFeaturedArtist,
    FileRecord,
    Track,
    TrackFeaturedArtist,
    TrackFile,
    TrackIdentityEvent,
    TrackOverride,
)
from djlib.ids import new_public_id
from djlib.resolve.normalizer import normalize_identity

# The only fields Task 11 human overrides cover (design §11): the track-level
# semantic identity fields also copied by `_copy_identity_from_file`.
_OVERRIDABLE_FIELDS = frozenset({'artist', 'title', 'version', 'edition'})

# `CurationEvent.event_type` string constants for the curation-affecting
# actions this service performs (Task 15 retrofit, design §25; the fourth
# added by Task 16 -- see `record_automatic_preferred_file`). Plain string
# literals -- `CurationEvent.event_type` is not a native enum -- but the exact
# spellings here must match `curation/replay.py`'s dispatch table byte-for-byte.
EVENT_TYPE_TRACK_OVERRIDE_SET = 'TRACK_OVERRIDE_SET'
EVENT_TYPE_TRACK_MERGE = 'TRACK_MERGE'
EVENT_TYPE_TRACK_SPLIT = 'TRACK_SPLIT'
EVENT_TYPE_TRACK_PREFERRED_FILE_AUTO_SET = 'TRACK_PREFERRED_FILE_AUTO_SET'


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _next_curation_sequence(session: Session) -> int:
    return (session.execute(select(func.max(CurationEvent.sequence))).scalar_one() or 0) + 1


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
        overrides = self._active_overrides(track.id)
        featured_artists = self._session.execute(
            select(TrackFeaturedArtist)
            .where(TrackFeaturedArtist.track_id == track.id)
            .order_by(TrackFeaturedArtist.position)
        ).scalars()
        return EffectiveIdentity(
            artist=overrides.get('artist', track.artist),
            title=overrides.get('title', track.title),
            version=overrides.get('version', track.version),
            edition=overrides.get('edition', track.edition),
            featured_artists=tuple(
                EffectiveFeaturedArtist(position=fa.position, name=fa.name, source=fa.source)
                for fa in featured_artists
            ),
        )

    def _active_overrides(self, track_id: int) -> dict[str, str | None]:
        rows = self._session.execute(
            select(TrackOverride).where(
                TrackOverride.track_id == track_id, TrackOverride.superseded_at.is_(None)
            )
        ).scalars()
        return {row.field: row.value_json.get('value') for row in rows}

    def set_override(self, track_public_id: str, field: str, value: str | None) -> TrackOverride:
        """Record a human semantic correction (design §11: EFFECTIVE layer).

        Append-only: any existing active override for this `(track, field)`
        is superseded (`superseded_at` set), never deleted or updated in
        place, per `.claude/rules/curation-persistence.md`. `effective_identity`
        prefers the newly-active row over the track's own RESOLVED column;
        `refresh_track_identity` on a later rescan keeps updating that
        RESOLVED column underneath but never touches `track_overrides`, so
        the override survives rescans untouched (design §11).
        """
        if field not in _OVERRIDABLE_FIELDS:
            raise ValueError(f'unsupported override field: {field!r}')
        track = self._require_track(track_public_id)

        existing = self._session.execute(
            select(TrackOverride).where(
                TrackOverride.track_id == track.id,
                TrackOverride.field == field,
                TrackOverride.superseded_at.is_(None),
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.superseded_at = _now()

        override = TrackOverride(track_id=track.id, field=field, value_json={'value': value})
        self._session.add(override)
        self._session.flush()

        # Anchored on the track's own currently-active file(s) rather than its
        # public_id: a fresh rebuild's rescan assigns every track a brand-new
        # random public_id (see `.claude/rules/curation-persistence.md` and
        # `scan/service.py`), so `relative_path` is the only reference that
        # still resolves to "this same track" after replay (design §25).
        self._record_curation_event(
            event_type=EVENT_TYPE_TRACK_OVERRIDE_SET,
            track_public_id=track.public_id,
            file_public_id=None,
            payload={
                'track_public_id': track.public_id,
                'field': field,
                'value': value,
                'track_relative_paths': self._active_file_relative_paths(track.id),
            },
        )
        return override

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
        absorbed_files = list(
            self._session.execute(
                select(TrackFile).where(
                    TrackFile.track_id == absorbed.id, TrackFile.is_active.is_(True)
                )
            ).scalars()
        )
        absorbed_file_ids = [link.file_id for link in absorbed_files]

        # Snapshot both sides' relative paths *before* the reassignment below
        # -- once absorbed's links are repointed at survivor, a post-hoc query
        # for "survivor's active files" would wrongly include the
        # just-migrated files too, and replay needs these two sets kept
        # disjoint to unambiguously re-find each side after a rebuild.
        pre_merge_target_relative_paths = self._active_file_relative_paths(survivor.id)
        pre_merge_source_relative_paths = self._active_file_relative_paths(absorbed.id)
        file_relationships = self._relationships_by_relative_path(absorbed_file_ids, relationships)

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

        self._record_curation_event(
            event_type=EVENT_TYPE_TRACK_MERGE,
            track_public_id=survivor.public_id,
            file_public_id=None,
            payload={
                'source_track_public_id': absorbed.public_id,
                'target_track_public_id': survivor.public_id,
                'source_file_relative_paths': pre_merge_source_relative_paths,
                'target_file_relative_paths': pre_merge_target_relative_paths,
                'file_relationships': file_relationships,
                'decision_source': decision_source.value,
            },
        )
        return event

    def activate_track(self, track: Track, preferred_file_id: int | None = None) -> None:
        track.status = TrackStatus.ACTIVE
        if preferred_file_id is not None:
            track.preferred_file_id = preferred_file_id
        self._session.flush()

    def record_automatic_preferred_file(self, track: Track, preferred_file: FileRecord) -> None:
        """Durably records a fully-automatic preferred-file choice (design
        §21's `AUTO_CONFIRMED` consolidation, `DuplicateService.
        _consolidate_auto_confirmed_groups`) -- the one case where
        `Track.preferred_file_id` gets set with no human decision anywhere,
        and therefore no `curation/decisions.py`-recorded event to replay.
        Without this, `djlib rebuild` restores the merge itself (via the
        survivor's own `TRACK_MERGE` events) but silently drops which file
        was preferred, violating design §32's "same preferred-file
        decisions" rebuild guarantee for the most common duplicate scenario
        of all: exact-copy files.
        """
        self._record_curation_event(
            event_type=EVENT_TYPE_TRACK_PREFERRED_FILE_AUTO_SET,
            track_public_id=track.public_id,
            file_public_id=preferred_file.public_id,
            payload={'preferred_file_relative_path': preferred_file.relative_path},
        )

    def merge_tracks(self, source_public_id: str, target_public_id: str) -> TrackIdentityEvent:
        """Human-triggered MERGE (design §13): thin wrapper over `merge_track_into`.

        Delegates all state mutation to the one shared merge primitive so
        there is exactly one code path to reason about; the only difference
        from Task 10's automatic consolidation is `decision_source=HUMAN`
        and that there is no pairwise evidence to pick a per-file
        relationship from -- `AUDIO_EQUIVALENT` is used as the conservative
        default for every migrated file (a human merge asserts "these are
        the same audio version", not "byte-identical", so `EXACT_DUPLICATE`
        would overclaim and `PROBABLE` would underclaim).
        """
        if source_public_id == target_public_id:
            raise ValueError('cannot merge a track into itself')
        source = self._require_track(source_public_id)
        target = self._require_track(target_public_id)

        if target.status != TrackStatus.ACTIVE:
            self.activate_track(target)

        relationships = {
            link.file_id: RelationshipType.AUDIO_EQUIVALENT
            for link in self._session.execute(
                select(TrackFile).where(
                    TrackFile.track_id == source.id, TrackFile.is_active.is_(True)
                )
            ).scalars()
        }
        return self.merge_track_into(
            survivor=target,
            absorbed=source,
            relationships=relationships,
            decision_source=DecisionSource.HUMAN,
        )

    def split_track(self, source_public_id: str, file_public_ids: list[str]) -> Track:
        """Human-triggered SPLIT (design §13): move `file_public_ids` off
        `source_public_id` onto a brand-new track with a fresh `public_id`
        (never reused). `source_public_id` keeps its original identity for
        whatever files remain active on it.

        The new track's identity is copied from the split-off file's own
        already-resolved metadata (`_copy_identity_from_file`, keyed on the
        first named file when more than one moves together) -- never from
        `source`'s shared identity, since the entire point of a split is
        that the moved file(s) represent a genuinely distinct identity.
        Every moved file becomes `PRIMARY` on the new track (it is now that
        track's own file, exactly like a freshly-scanned file's first
        link). The new track is promoted straight to `ACTIVE` with the
        split-off file as its preferred file -- a human just made an
        explicit, high-confidence identity decision, so there is nothing
        left for the automatic dedup pipeline to review, unlike a plain
        scan's brand-new `PROVISIONAL` track.

        Also rejects any pre-existing `DuplicateGroup` that still spans a
        moved file and a file remaining on `source` (see
        `_reject_groups_spanning_the_split`) -- otherwise a later
        `duplicates run` could reclassify that group from current evidence
        alone, with no memory of this split, and silently re-merge exactly
        what the human just separated.
        """
        if not file_public_ids:
            raise ValueError('split_track requires at least one file_public_id')
        source = self._require_track(source_public_id)
        files = self._require_files(file_public_ids)

        new_track = Track(public_id=new_public_id('trk'), status=TrackStatus.PROVISIONAL)
        self._session.add(new_track)
        self._session.flush()
        self._copy_identity_from_file(new_track, files[0])

        moved_file_ids = {file.id for file in files}
        links = list(
            self._session.execute(
                select(TrackFile).where(
                    TrackFile.track_id == source.id,
                    TrackFile.file_id.in_(moved_file_ids),
                    TrackFile.is_active.is_(True),
                )
            ).scalars()
        )
        if len(links) != len(files):
            raise RuntimeError(
                f'{source_public_id!r} does not actively own every file in {file_public_ids!r}'
            )
        for link in links:
            link.track_id = new_track.id
            link.relationship = RelationshipType.PRIMARY
            link.decision_source = DecisionSource.HUMAN

        self._reject_groups_spanning_the_split(source.id, moved_file_ids)

        self.activate_track(new_track, preferred_file_id=files[0].id)

        event = TrackIdentityEvent(
            event_uuid=str(uuid.uuid4()),
            event_type=IdentityEventType.SPLIT,
            source_track_public_id=source.public_id,
            target_track_public_id=new_track.public_id,
            payload_json={
                'decision_source': DecisionSource.HUMAN.value,
                'moved_file_public_ids': [file.public_id for file in files],
            },
        )
        self._session.add(event)
        self._session.flush()

        # `remaining` is queried *after* the reassignment above so it reflects
        # what actually stayed on `source`; `moved` comes straight from the
        # already-resolved `files` list rather than a DB query, since those
        # rows' `track_id` has already moved to `new_track` by this point.
        self._record_curation_event(
            event_type=EVENT_TYPE_TRACK_SPLIT,
            track_public_id=new_track.public_id,
            file_public_id=None,
            payload={
                'source_track_public_id': source.public_id,
                'new_track_public_id': new_track.public_id,
                'moved_file_relative_paths': sorted(file.relative_path for file in files),
                'remaining_file_relative_paths': self._active_file_relative_paths(source.id),
            },
        )
        return new_track

    def _reject_groups_spanning_the_split(
        self, source_track_id: int, moved_file_ids: set[int]
    ) -> None:
        """Prevent a later automatic `duplicates run` from silently re-merging
        exactly what a human just split apart (design §13: human MERGE/SPLIT
        decisions are stronger than automatic analysis; `curation-persistence.md`:
        a conflicting rescan never clobbers curation).

        `DuplicateGroup`/`DuplicatePairEvidence` carry no memory of a split --
        `Track`/`TrackFile` are the only things this method changed. Without
        this, a moved file and a file still on `source_track_id` that were
        ever blocked together as duplicate candidates could still share a
        `DETECTED`/`AUTO_CONFIRMED`/`REVIEW_REQUIRED` group; a later
        `duplicates analyze`/`run` would reclassify that group from current
        evidence alone (with no idea a human just separated these tracks) and
        could reconfirm + auto-consolidate it right back together.

        The fix reuses `DuplicateService`'s own guard rather than inventing a
        new one: `DuplicateStatus.REJECTED` is already excluded from
        `_ANALYZABLE_STATUSES` (Task 10), so a group spanning the split is
        moved to `REJECTED` here and permanently skipped by future automatic
        analysis. Only a group containing files on BOTH sides of the split is
        touched -- a moved file's unrelated membership in some other group
        (a coincidental duplicate of a third, uninvolved track) is left alone.
        """
        remaining_file_ids = set(
            self._session.execute(
                select(TrackFile.file_id).where(
                    TrackFile.track_id == source_track_id, TrackFile.is_active.is_(True)
                )
            ).scalars()
        )
        if not remaining_file_ids:
            return

        moved_group_ids = set(
            self._session.execute(
                select(DuplicateGroupMember.group_id).where(
                    DuplicateGroupMember.file_id.in_(moved_file_ids)
                )
            ).scalars()
        )
        for group_id in moved_group_ids:
            member_file_ids = set(
                self._session.execute(
                    select(DuplicateGroupMember.file_id).where(
                        DuplicateGroupMember.group_id == group_id
                    )
                ).scalars()
            )
            if member_file_ids & remaining_file_ids:
                group = self._session.get(DuplicateGroup, group_id)
                if group is not None and group.status not in (
                    DuplicateStatus.REJECTED,
                    DuplicateStatus.CONFIRMED,
                    DuplicateStatus.DEFERRED,
                ):
                    group.status = DuplicateStatus.REJECTED
                    group.resolved_at = _now()

    def _require_track(self, public_id: str) -> Track:
        track = self._session.execute(
            select(Track).where(Track.public_id == public_id)
        ).scalar_one_or_none()
        if track is None:
            raise RuntimeError(f'no track with public_id {public_id!r}')
        return track

    def _require_files(self, public_ids: list[str]) -> list[FileRecord]:
        files = list(
            self._session.execute(
                select(FileRecord).where(FileRecord.public_id.in_(public_ids))
            ).scalars()
        )
        by_public_id = {file.public_id: file for file in files}
        missing = [public_id for public_id in public_ids if public_id not in by_public_id]
        if missing:
            raise RuntimeError(f'no file(s) with public_id(s) {missing!r}')
        return [by_public_id[public_id] for public_id in public_ids]

    def _active_file_relative_paths(self, track_id: int) -> list[str]:
        return sorted(
            self._session.execute(
                select(FileRecord.relative_path)
                .join(TrackFile, TrackFile.file_id == FileRecord.id)
                .where(TrackFile.track_id == track_id, TrackFile.is_active.is_(True))
            ).scalars()
        )

    def _relationships_by_relative_path(
        self, file_ids: list[int], relationships: dict[int, RelationshipType]
    ) -> dict[str, str]:
        if not file_ids:
            return {}
        rows = self._session.execute(
            select(FileRecord.id, FileRecord.relative_path).where(FileRecord.id.in_(file_ids))
        ).all()
        return {
            relative_path: relationships.get(file_id, RelationshipType.AUDIO_EQUIVALENT).value
            for file_id, relative_path in rows
        }

    def _record_curation_event(
        self,
        event_type: str,
        track_public_id: str | None,
        file_public_id: str | None,
        payload: dict,
    ) -> CurationEvent:
        """Insert one `CurationEvent` row in the *same* transaction as the
        state change it represents (design §25, `.claude/rules/
        curation-persistence.md`). Never appended to `events.jsonl` here --
        that is `CurationJournal.export_pending()`'s job, as a deliberately
        separate, retriable step.
        """
        event = CurationEvent(
            sequence=_next_curation_sequence(self._session),
            event_uuid=str(uuid.uuid4()),
            event_type=event_type,
            track_public_id=track_public_id,
            file_public_id=file_public_id,
            payload_json=payload,
        )
        self._session.add(event)
        self._session.flush()
        return event

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
